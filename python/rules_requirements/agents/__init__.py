# SPDX-License-Identifier: AGPL-3.0-or-later
"""Agentic workflows over a requirements model.

A *workflow* inspects the model, its evidence and the source tree and returns
:class:`Finding` objects. Each finding names the entity it concerns and may
carry a *proposal* (a new user need / requirement / risk / mitigation). In the
web editor a finding can be elevated to a note on its entity (driving the next
implementation cycle — open notes appear in the gap queue) or turned into the
proposed object.

Deterministic workflows need no LLM; the others use :mod:`.llm`.
"""

from __future__ import annotations

import itertools
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable

from rules_requirements.agents.llm import LLM, LLMError


@dataclass
class Finding:
    workflow: str
    severity: str  # info | warning | error
    category: str
    title: str
    detail: str = ""
    entity: str = ""  # the entity this is about ("" = the model as a whole)
    refs: list[str] = field(default_factory=list)  # other related ids
    proposal: dict[str, Any] | None = None  # {"kind": ..., "data": {...}}
    source: str = "rule"  # rule | llm
    id: str = ""
    status: str = "open"  # open | applied | dismissed

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "workflow": self.workflow,
            "severity": self.severity,
            "category": self.category,
            "title": self.title,
            "detail": self.detail,
            "entity": self.entity,
            "refs": self.refs,
            "proposal": self.proposal,
            "source": self.source,
            "status": self.status,
        }


@dataclass
class Workflow:
    id: str
    title: str
    description: str
    run: Callable[..., list[Finding]]
    needs_llm: bool = False
    params: dict[str, str] = field(default_factory=dict)  # name -> help

    def to_dict(self, llm_available: bool) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "needs_llm": self.needs_llm,
            "available": llm_available or not self.needs_llm,
            "params": self.params,
        }


@dataclass
class Job:
    id: str
    workflow: str
    params: dict[str, Any]
    status: str = "queued"  # queued | running | done | failed
    log: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    error: str = ""
    started: float = 0.0
    finished: float = 0.0
    result: Any = None  # workflow-specific extra output (e.g. assistant operations)

    def say(self, message: str) -> None:
        self.log.append(f"{time.strftime('%H:%M:%S')} {message}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "workflow": self.workflow,
            "params": self.params,
            "status": self.status,
            "log": self.log,
            "findings": [f.to_dict() for f in self.findings],
            "error": self.error,
            "started": self.started,
            "finished": self.finished,
            "result": self.result,
        }


class JobManager:
    """Runs workflows on background threads and keeps their results."""

    def __init__(self, workflows: dict[str, Workflow], context_factory: Callable[[Job], Any], llm: LLM | None):
        self.workflows = workflows
        self.context_factory = context_factory
        self.llm = llm
        self.jobs: dict[str, Job] = {}
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    def start(self, workflow_id: str, params: dict[str, Any] | None = None, wait: bool = False) -> Job:
        wf = self.workflows.get(workflow_id)
        if wf is None:
            raise KeyError(f"unknown workflow {workflow_id!r}")
        if wf.needs_llm and self.llm is None:
            raise LLMError("this workflow needs an LLM, and none is configured")
        with self._lock:
            job = Job(id=f"job-{next(self._ids)}", workflow=workflow_id, params=dict(params or {}))
            self.jobs[job.id] = job
        thread = threading.Thread(target=self._run, args=(wf, job), daemon=True)
        thread.start()
        if wait:
            thread.join()
        return job

    def _run(self, wf: Workflow, job: Job) -> None:
        job.status, job.started = "running", time.time()
        job.say(f"{wf.title}: started")
        try:
            ctx = self.context_factory(job)
            findings = wf.run(ctx, job, self.llm, **job.params)
            for i, f in enumerate(findings, 1):
                f.id = f.id or f"{job.id}-{i}"
            job.findings = findings
            job.status = "done"
            job.say(f"finished: {len(findings)} finding(s)")
        except Exception as exc:
            job.status, job.error = "failed", f"{type(exc).__name__}: {exc}"
            job.say(f"failed: {job.error}")
            job.log.extend(traceback.format_exc().splitlines()[-6:])
        finally:
            job.finished = time.time()

    def finding(self, finding_id: str) -> tuple[Job, Finding]:
        with self._lock:
            jobs = list(self.jobs.values())
        for job in jobs:
            for f in job.findings:
                if f.id == finding_id:
                    return job, f
        raise KeyError(finding_id)


__all__ = ["Finding", "Job", "JobManager", "Workflow"]
