# SPDX-License-Identifier: AGPL-3.0-or-later
"""``rr serve`` — the interactive requirements editor.

A small standard-library HTTP server: a JSON API over a
:class:`~rules_requirements.server.workspace.Workspace` (model, evidence,
annotations, git) and the agent :class:`~rules_requirements.agents.JobManager`, plus a
static single-page UI. It edits files in your checkout, so by default it

* binds to 127.0.0.1 only,
* rejects requests whose ``Host`` header is not a local name (DNS rebinding),
* requires the ``X-RR-Request`` header on every mutating request (a
  cross-site form cannot set it; CORS is never granted), and
* optionally requires ``Authorization: Bearer <token>`` (``--token``).
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import socket
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlparse

from rules_requirements import config as cfg
from rules_requirements import graph as rr_graph
from rules_requirements import report as rr_report
from rules_requirements.agents import JobManager
from rules_requirements.agents.llm import LLM, LLMError, llm_status
from rules_requirements.agents.workflows import WORKFLOWS, Context
from rules_requirements.diff import summarize
from rules_requirements.model import NOTE_KINDS, NOTE_STATUSES
from rules_requirements.server.workspace import Workspace, WorkspaceError, entity_payload, summary_rows

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}

Handler = Callable[["Api", dict[str, str], dict[str, list[str]], Any], Any]


class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class Api:
    """Route table + handlers, independent of the HTTP plumbing (easy to test)."""

    def __init__(self, ws: Workspace, llm: LLM | None = None, llm_enabled: bool = True, author: str = ""):
        self.ws = ws
        self.llm = llm
        self.llm_enabled = llm_enabled
        self.author = author
        self.jobs = JobManager(WORKFLOWS, self._context, llm)
        self.routes: list[tuple[str, re.Pattern[str], Handler]] = []
        r = self._route
        r("GET", r"/api/state", Api.state)
        r("GET", r"/api/entities", Api.list_entities)
        r("POST", r"/api/entities", Api.create_entity)
        r("GET", r"/api/entities/(?P<id>[^/]+)", Api.get_entity)
        r("PUT", r"/api/entities/(?P<id>[^/]+)", Api.update_entity)
        r("DELETE", r"/api/entities/(?P<id>[^/]+)", Api.delete_entity)
        r("POST", r"/api/entities/(?P<id>[^/]+)/rename", Api.rename_entity)
        r("POST", r"/api/entities/(?P<id>[^/]+)/notes", Api.add_note)
        r("PATCH", r"/api/entities/(?P<id>[^/]+)/notes/(?P<note>[^/]+)", Api.update_note)
        r("DELETE", r"/api/entities/(?P<id>[^/]+)/notes/(?P<note>[^/]+)", Api.delete_note)
        r("GET", r"/api/next-id", Api.next_id)
        r("GET", r"/api/graph", Api.graph)
        r("GET", r"/api/report", Api.report)
        r("GET", r"/api/queue", Api.queue)
        r("POST", r"/api/reload", Api.reload)
        r("GET", r"/api/annotations", Api.annotations)
        r("GET", r"/api/source", Api.source)
        r("GET", r"/api/git/status", Api.git_status)
        r("GET", r"/api/git/refs", Api.git_refs)
        r("GET", r"/api/git/log", Api.git_log)
        r("GET", r"/api/diff", Api.diff)
        r("POST", r"/api/git/tag", Api.git_tag)
        r("POST", r"/api/git/commit", Api.git_commit)
        r("GET", r"/api/agents", Api.agents)
        r("POST", r"/api/agents/run", Api.run_agent)
        r("GET", r"/api/agents/jobs", Api.list_jobs)
        r("GET", r"/api/agents/jobs/(?P<job>[^/]+)", Api.get_job)
        r("POST", r"/api/findings/(?P<finding>[^/]+)/apply", Api.apply_finding)
        r("POST", r"/api/findings/(?P<finding>[^/]+)/dismiss", Api.dismiss_finding)

    def _route(self, method: str, pattern: str, fn: Handler) -> None:
        self.routes.append((method, re.compile("^" + pattern + "$"), fn))

    def dispatch(self, method: str, path: str, query: dict[str, list[str]], body: Any, author: str = "") -> Any:
        for m, rx, fn in self.routes:
            match = rx.match(path)
            if match and m == method:
                params = {k: unquote(v) for k, v in match.groupdict().items()}
                if author:
                    params["_author"] = author
                try:
                    return fn(self, params, query, body)
                except WorkspaceError as exc:
                    raise HttpError(exc.status, str(exc)) from exc
                except (KeyError, ValueError) as exc:
                    raise HttpError(400, str(exc).strip("'\"")) from exc
                except LLMError as exc:
                    raise HttpError(503, str(exc)) from exc
        if any(rx.match(path) for _, rx, _ in self.routes):
            raise HttpError(405, f"{method} not allowed on {path}")
        raise HttpError(404, f"no such endpoint: {path}")

    # ------------------------------------------------------------------ helpers

    def _context(self, job: Any) -> Context:
        snap = self.ws.snapshot()
        return Context(
            model=snap.model, matrix=snap.matrix, root=self.ws.root, references=snap.references, issues=snap.issues
        )

    @staticmethod
    def _q(query: dict[str, list[str]], key: str, default: str = "") -> str:
        return (query.get(key) or [default])[0]

    def _author(self, params: dict[str, str]) -> str:
        return params.get("_author") or self.author or self.ws.author

    # ------------------------------------------------------------------ model

    def state(self, params: dict[str, str], query: dict[str, list[str]], body: Any) -> Any:
        snap = self.ws.snapshot()
        c = snap.model.config
        return {
            "project": dict(snap.model.project),
            "root": self.ws.root,
            "files": self.ws.files(),
            "model_paths": self.ws.model_paths,
            "evidence_paths": self.ws.evidence_paths,
            "config": {
                "prefixes": dict(c.prefixes),
                "levels": [{"name": lv.name, "rank": lv.rank, "description": lv.description} for lv in c.levels],
                "severities": list(c.severities),
                "likelihoods": list(c.likelihoods),
                "mitigation_types": list(cfg.MITIGATION_TYPES),
                "statuses": list(cfg.STATUSES),
                "note_kinds": list(NOTE_KINDS),
                "note_statuses": list(NOTE_STATUSES),
                "kinds": {k: cfg.SECTIONS[k] for k in cfg.KINDS},
                "high_severities": list(c.high_severities),
                "default_level": c.default_level,
                "default_provided_level": c.default_provided_level,
                "autonomous_max_level": c.autonomous_max_level,
                "id_pattern": c.id_pattern,
                "acceptable_risk_score": c.acceptable_risk_score,
            },
            "counts": snap.matrix.counts(),
            "issues": [
                {
                    "severity": i.severity,
                    "code": i.code,
                    "message": i.message,
                    "entity": i.entity,
                    "path": i.location.path,
                    "line": i.location.line,
                }
                for i in snap.issues
            ],
            "warnings": snap.warnings,
            "git": self.ws.git_status(),
            "llm": llm_status(self.llm, self.llm_enabled),
            "author": self._author(params),
            "annotations_scanned": snap.references is not None,
        }

    def list_entities(self, params: dict[str, str], query: dict[str, list[str]], body: Any) -> Any:
        kind = self._q(query, "kind")
        kinds = [kind] if kind else list(cfg.KINDS)
        for k in kinds:
            if k not in cfg.KINDS:
                raise HttpError(400, f"unknown kind {k!r}")
        return {"entities": summary_rows(self.ws, kinds)}

    def get_entity(self, params: dict[str, str], query: dict[str, list[str]], body: Any) -> Any:
        return entity_payload(self.ws, params["id"])

    def create_entity(self, params: dict[str, str], query: dict[str, list[str]], body: Any) -> Any:
        body = _obj(body)
        eid = self.ws.create(str(body.get("kind", "")), _obj(body.get("data")), str(body.get("file", "")))
        return entity_payload(self.ws, eid)

    def update_entity(self, params: dict[str, str], query: dict[str, list[str]], body: Any) -> Any:
        body = _obj(body)
        self.ws.update(params["id"], _obj(body.get("data")), version=str(body.get("version") or ""))
        return entity_payload(self.ws, params["id"])

    def delete_entity(self, params: dict[str, str], query: dict[str, list[str]], body: Any) -> Any:
        self.ws.delete(params["id"], force=self._q(query, "force") in ("1", "true"))
        return {"deleted": params["id"]}

    def rename_entity(self, params: dict[str, str], query: dict[str, list[str]], body: Any) -> Any:
        new_id = str(_obj(body).get("new_id", "")).strip()
        touched = self.ws.rename(params["id"], new_id)
        return {"renamed": new_id, "updated_references": touched}

    def add_note(self, params: dict[str, str], query: dict[str, list[str]], body: Any) -> Any:
        body = _obj(body)
        kind = str(body.get("kind", "comment"))
        if kind not in NOTE_KINDS:
            raise HttpError(400, f"note kind must be one of {NOTE_KINDS}")
        text = str(body.get("text", "")).strip()
        if not text:
            raise HttpError(400, "a note needs text")
        nid = self.ws.add_note(params["id"], text, kind, author=str(body.get("author") or self._author(params)))
        return {"note": nid, **entity_payload(self.ws, params["id"])}

    def update_note(self, params: dict[str, str], query: dict[str, list[str]], body: Any) -> Any:
        body = _obj(body)
        changes = {k: str(body[k]) for k in ("status", "text", "kind") if k in body}
        if changes.get("status") and changes["status"] not in NOTE_STATUSES:
            raise HttpError(400, f"status must be one of {NOTE_STATUSES}")
        if changes.get("kind") and changes["kind"] not in NOTE_KINDS:
            raise HttpError(400, f"kind must be one of {NOTE_KINDS}")
        self.ws.update_note(params["id"], params["note"], **changes)
        return entity_payload(self.ws, params["id"])

    def delete_note(self, params: dict[str, str], query: dict[str, list[str]], body: Any) -> Any:
        self.ws.delete_note(params["id"], params["note"])
        return entity_payload(self.ws, params["id"])

    def next_id(self, params: dict[str, str], query: dict[str, list[str]], body: Any) -> Any:
        kind = self._q(query, "kind")
        if kind not in cfg.KINDS:
            raise HttpError(400, "kind is required")
        return {"id": self.ws.next_id(kind), "file": self.ws.file_for_new(kind)}

    # ------------------------------------------------------------------ trace

    def graph(self, params: dict[str, str], query: dict[str, list[str]], body: Any) -> Any:
        snap = self.ws.snapshot()
        statuses = {k: v.status for k, v in snap.matrix.verdicts.items()}
        nodes, edges = rr_graph.build(snap.model, statuses, include_methods=self._q(query, "methods") in ("1", "true"))
        focus = self._q(query, "focus")
        if focus:
            depth = max(1, min(6, int(self._q(query, "depth", "2") or 2)))
            keep = _neighborhood(focus, edges, depth)
            nodes = [n for n in nodes if n.id in keep]
            edges = [e for e in edges if e.source in keep and e.target in keep]
        kinds = [k for k in self._q(query, "kinds").split(",") if k]
        if kinds:
            nodes = [n for n in nodes if n.kind in kinds]
            ids = {n.id for n in nodes}
            edges = [e for e in edges if e.source in ids and e.target in ids]
        return {
            "nodes": [n.__dict__ for n in nodes],
            "edges": [e.__dict__ for e in edges],
            "svg": rr_graph.to_svg(nodes, edges, link_prefix="#/entity/"),
            "mermaid": rr_graph.to_mermaid(nodes, edges),
        }

    def report(self, params: dict[str, str], query: dict[str, list[str]], body: Any) -> Any:
        return rr_report.to_dict(self.ws.snapshot().matrix)

    def queue(self, params: dict[str, str], query: dict[str, list[str]], body: Any) -> Any:
        return {"queue": [g.to_dict() for g in self.ws.snapshot().matrix.gaps]}

    def reload(self, params: dict[str, str], query: dict[str, list[str]], body: Any) -> Any:
        self.ws.snapshot(refresh=True)
        return self.state(params, query, body)

    def annotations(self, params: dict[str, str], query: dict[str, list[str]], body: Any) -> Any:
        snap = self.ws.snapshot()
        refs = snap.references or []
        known = snap.model.ids()
        return {
            "scanned": snap.references is not None,
            "annotations": [dict(r.to_dict(), unknown=[i for i in r.ids if i not in known]) for r in refs],
        }

    def source(self, params: dict[str, str], query: dict[str, list[str]], body: Any) -> Any:
        rel = self._q(query, "path")
        return {"path": rel, "lines": self.ws.source(rel)}

    # ------------------------------------------------------------------ git

    def git_status(self, params: dict[str, str], query: dict[str, list[str]], body: Any) -> Any:
        return self.ws.git_status()

    def git_refs(self, params: dict[str, str], query: dict[str, list[str]], body: Any) -> Any:
        return self.ws.refs()

    def git_log(self, params: dict[str, str], query: dict[str, list[str]], body: Any) -> Any:
        return {"commits": self.ws.log(int(self._q(query, "limit", "30") or 30), self._q(query, "ref", "HEAD"))}

    def diff(self, params: dict[str, str], query: dict[str, list[str]], body: Any) -> Any:
        old = self._q(query, "from", "HEAD")
        new = self._q(query, "to", "WORKTREE")
        changes = self.ws.diff(old, new)
        return {"from": old, "to": new, "summary": summarize(changes), "changes": [c.to_dict() for c in changes]}

    def git_tag(self, params: dict[str, str], query: dict[str, list[str]], body: Any) -> Any:
        body = _obj(body)
        author = str(body.get("author") or self._author(params))
        self.ws.tag(
            str(body.get("name", "")),
            str(body.get("message", "")),
            str(body.get("ref", "HEAD") or "HEAD"),
            author=author,
        )
        return self.ws.refs()

    def git_commit(self, params: dict[str, str], query: dict[str, list[str]], body: Any) -> Any:
        body = _obj(body)
        sha = self.ws.commit(str(body.get("message", "")), str(body.get("author") or self._author(params)))
        return {"commit": sha, **self.ws.git_status()}

    # ------------------------------------------------------------------ agents

    def agents(self, params: dict[str, str], query: dict[str, list[str]], body: Any) -> Any:
        return {
            "llm": llm_status(self.llm, self.llm_enabled),
            "workflows": [w.to_dict(self.llm is not None) for w in WORKFLOWS.values()],
        }

    def run_agent(self, params: dict[str, str], query: dict[str, list[str]], body: Any) -> Any:
        body = _obj(body)
        job = self.jobs.start(str(body.get("workflow", "")), _obj(body.get("params")), wait=bool(body.get("wait")))
        return job.to_dict()

    def list_jobs(self, params: dict[str, str], query: dict[str, list[str]], body: Any) -> Any:
        return {
            "jobs": [
                {k: v for k, v in j.to_dict().items() if k not in ("findings", "log")} | {"findings": len(j.findings)}
                for j in reversed(list(self.jobs.jobs.values()))
            ]
        }

    def get_job(self, params: dict[str, str], query: dict[str, list[str]], body: Any) -> Any:
        job = self.jobs.jobs.get(params["job"])
        if job is None:
            raise HttpError(404, f"no job {params['job']}")
        return job.to_dict()

    def apply_finding(self, params: dict[str, str], query: dict[str, list[str]], body: Any) -> Any:
        """Elevate a finding: a note on its entity, or the proposed object."""
        body = _obj(body)
        action = str(body.get("action", "note"))
        if action not in ("note", "create", "update"):
            raise HttpError(400, f"unknown action {action!r}")
        # Claim it first, so that a double-click applies it once.
        try:
            finding = self.jobs.transition(params["finding"], ("open",), "applying")
        except KeyError as exc:
            raise HttpError(404, f"no finding {params['finding']}") from exc
        except ValueError as exc:
            raise HttpError(409, f"finding {params['finding']} is already {exc}") from exc
        try:
            result = self._apply(finding, action, body, params)
        except BaseException:
            finding.status = "open"
            raise
        finding.status = "applied"
        return {"finding": finding.to_dict(), **result}

    def _apply(self, finding: Any, action: str, body: dict[str, Any], params: dict[str, str]) -> dict[str, Any]:
        author = f"rr-agent/{finding.workflow}" if finding.source == "llm" else (self._author(params) or "rr")
        if action == "note":
            target = str(body.get("entity") or finding.entity)
            if not target:
                raise HttpError(400, "this finding is not about a specific entity; pick one to attach the note to")
            kind = str(body.get("kind", "gap"))
            text = str(body.get("text") or (finding.title + (f"\n\n{finding.detail}" if finding.detail else "")))
            self.ws.add_note(target, text, kind if kind in NOTE_KINDS else "gap", author=author)
            return {"entity": entity_payload(self.ws, target)}
        if action in ("create", "update"):
            proposal = finding.proposal or {}
            data = _obj(body.get("data")) or dict(proposal.get("data", {}))
            kind = str(body.get("kind") or proposal.get("kind", ""))
            if action == "update" or proposal.get("op") == "update":
                target = str(body.get("entity") or finding.entity)
                current = self.ws.model.get(target)
                if current is None:
                    raise HttpError(404, f"{target} does not exist")
                # A proposal only carries the fields it changes: merge it over
                # the entity instead of replacing fields it cannot express.
                merged = {**edit_to_dict(current), **{k: v for k, v in data.items() if k != "id"}, "id": target}
                self.ws.update(target, merged, version=str(body.get("version") or ""))
                eid = target
            else:
                eid = self.ws.create(kind, data, str(body.get("file", "")))
            return {"entity": entity_payload(self.ws, eid)}
        raise HttpError(400, f"unknown action {action!r}")

    def dismiss_finding(self, params: dict[str, str], query: dict[str, list[str]], body: Any) -> Any:
        try:
            finding = self.jobs.transition(params["finding"], ("open", "dismissed"), "dismissed")
        except KeyError as exc:
            raise HttpError(404, f"no finding {params['finding']}") from exc
        except ValueError as exc:
            raise HttpError(409, f"finding {params['finding']} is already {exc}") from exc
        return finding.to_dict()


def edit_to_dict(ent: Any) -> dict[str, Any]:
    from rules_requirements.edit import entity_to_dict

    return entity_to_dict(ent)


def _obj(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise HttpError(400, "expected a JSON object")
    return value


def _neighborhood(focus: str, edges: list[rr_graph.Edge], depth: int) -> set[str]:
    adj: dict[str, set[str]] = {}
    for e in edges:
        adj.setdefault(e.source, set()).add(e.target)
        adj.setdefault(e.target, set()).add(e.source)
    seen, frontier = {focus}, {focus}
    for _ in range(depth):
        frontier = {n for f in frontier for n in adj.get(f, ())} - seen
        seen |= frontier
    return seen


# --------------------------------------------------------------------------- #
# HTTP plumbing                                                               #
# --------------------------------------------------------------------------- #


def make_handler(api: Api, token: str = "", allowed_hosts: set[str] | None = None) -> type[BaseHTTPRequestHandler]:
    hosts = LOCAL_HOSTS | {h.strip().lower() for h in (allowed_hosts or set())}

    class RequestHandler(BaseHTTPRequestHandler):
        server_version = "rules_requirements"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:  # quieter than the default
            if os.environ.get("RR_SERVE_LOG"):
                super().log_message(fmt, *args)

        def _send(self, status: int, body: bytes, ctype: str, extra: dict[str, str] | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, status: int, payload: Any) -> None:
            self._send(status, json.dumps(payload).encode(), "application/json; charset=utf-8")

        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").strip().lower()
            name = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
            return name in hosts

        def _handle(self, method: str) -> None:
            if not self._host_ok():
                return self._json(403, {"error": "unexpected Host header (DNS rebinding protection); see --allow-host"})
            url = urlparse(self.path)
            if url.path.startswith("/api/"):
                if token and self.headers.get("Authorization", "") != f"Bearer {token}":
                    return self._json(401, {"error": "missing or invalid token"})
                if method not in ("GET", "HEAD") and self.headers.get("X-RR-Request") != "1":
                    return self._json(403, {"error": "missing X-RR-Request header"})
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = -1
                if length < 0:
                    return self._json(400, {"error": "invalid Content-Length"})
                if length > 5 * 1024 * 1024:
                    return self._json(413, {"error": "request too large"})
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw) if raw else None
                except json.JSONDecodeError:
                    return self._json(400, {"error": "invalid JSON body"})
                try:
                    # Header values are ASCII; the UI percent-encodes non-ASCII names.
                    author = unquote(self.headers.get("X-RR-Author", "")).strip()
                    result = api.dispatch(method, url.path, parse_qs(url.query), body, author=author)
                except HttpError as exc:
                    return self._json(exc.status, {"error": str(exc)})
                except Exception as exc:
                    return self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
                return self._json(200, result)
            if method not in ("GET", "HEAD"):
                return self._json(405, {"error": "method not allowed"})
            return self._static(url.path)

        def _static(self, path: str) -> None:
            rel = "index.html" if path in ("", "/") else path.lstrip("/")
            full = os.path.realpath(os.path.join(STATIC, rel))
            if not full.startswith(os.path.realpath(STATIC) + os.sep) or not os.path.isfile(full):
                return self._json(404, {"error": "not found"})
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
                ctype += "; charset=utf-8"
            with open(full, "rb") as fh:
                data = fh.read()
            csp = (
                "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
            )
            self._send(200, data, ctype, {"Content-Security-Policy": csp})

        def do_GET(self) -> None:
            self._handle("GET")

        def do_HEAD(self) -> None:
            self._handle("HEAD")

        def do_POST(self) -> None:
            self._handle("POST")

        def do_PUT(self) -> None:
            self._handle("PUT")

        def do_PATCH(self) -> None:
            self._handle("PATCH")

        def do_DELETE(self) -> None:
            self._handle("DELETE")

    return RequestHandler


def serve(
    api: Api,
    host: str = "127.0.0.1",
    port: int = 8080,
    token: str = "",
    allowed_hosts: set[str] | None = None,
    ready: Callable[[str], None] | None = None,
) -> ThreadingHTTPServer:
    """Start the server on a background thread and return it (``.shutdown()`` to stop).

    Off loopback the Host check is no protection (any client can send
    ``Host: localhost``), so a token is then mandatory: pass ``token`` or one is
    generated (see :func:`needs_token`).
    """
    extra = {h.lower() for h in (allowed_hosts or ())}
    if host not in ("127.0.0.1", "localhost", "::1", "0.0.0.0", "::"):  # noqa: S104 — only a comparison
        extra.add(host.lower())
    if needs_token(host) and not token:
        raise ValueError("a --token is required when binding to a non-loopback address")
    server_cls: type[ThreadingHTTPServer] = _IPv6Server if ":" in host else ThreadingHTTPServer
    httpd = server_cls((host, port), make_handler(api, token, extra))
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    if ready:
        shown = "localhost" if host in ("127.0.0.1", "0.0.0.0", "::") else host  # noqa: S104
        ready(f"http://{f'[{shown}]' if ':' in shown else shown}:{httpd.server_address[1]}/")
    return httpd


def needs_token(host: str) -> bool:
    """Whether binding to ``host`` exposes the server beyond this machine."""
    return host not in ("127.0.0.1", "localhost", "::1")


class _IPv6Server(ThreadingHTTPServer):
    address_family = socket.AF_INET6


__all__ = ["Api", "HTTPStatus", "HttpError", "needs_token", "serve"]
