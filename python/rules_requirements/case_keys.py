# SPDX-License-Identifier: AGPL-3.0-or-later
"""Case identity: the key every per-case result is filed under.

A test case is identified by :class:`CaseKey` — the build target that ran it
and its *path* within that target, ``<classname>::<name>``::

    //web:clocksync_test#clocksync::bestSample keeps the min-RTT sample
    //pi/server/tests:server_test#pi.server.tests.test_handler::test_configure[a]

The key is what a requirement claims and what attribution maps to exactly one
owner, so it must not depend on how a case is tagged or executed:

* **Re-tagging never renames a case.** ``[rr:ID]`` tags inside a case name
  are stripped from the path (:func:`case_path`); :func:`name_tags` reads them.
* **Retries, repeats, shards and evidence roots are not identity.** They are
  execution dimensions (:func:`run_dims_from_path`), folded into one
  :class:`CaseRow` per key by :func:`index_cases`.
* **A target with no per-case output has one case**, ``[target]``
  (:data:`SYNTHETIC_PATH`): Bazel's generated ``test.xml``, or a result one of
  our writers marks ``rr.synthetic=true``.

Evidence that cannot be pinned to a build target gets a pseudo-target —
``record:<stem>`` for records without a ``target:``, ``suite:<testsuite name>``
for JUnit outside a ``bazel-testlogs`` tree (see :func:`target_of`).
"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable, NamedTuple

from rules_requirements.ingest import (
    FILE_PROPERTY,
    NAME_TAG,
    STATUS_ORDER,
    Evidence,
    TestCase,
    name_tags,
    workspace_relative,
)
from rules_requirements.ingest.junit import ATTEMPT, SHARD_RUN
from rules_requirements.util import dedupe, natural_key

__all__ = [
    "FILE_PROPERTY",
    "RECORD_PREFIX",
    "SUITE_PREFIX",
    "SYNTHETIC_PATH",
    "UNNAMED_PATH",
    "CaseKey",
    "CaseRow",
    "RunDims",
    "case_path",
    "declared_of",
    "file_of",
    "index_cases",
    "is_synthetic",
    "is_target_scope",
    "is_unscoped",
    "key_of",
    "name_tags",
    "nodeid_to_case_path",
    "pseudo_target",
    "run_dims_from_path",
    "target_of",
    "workspace_relative",
]

SYNTHETIC_PATH = "[target]"
"""Path of the single result of a target that reported no per-case results."""

UNNAMED_PATH = "[unnamed]"
"""Path of a case whose classname and name are both empty (a key's path is never empty)."""

RECORD_PREFIX = "record:"
SUITE_PREFIX = "suite:"
_RECORD_SUFFIXES = (".rr.yaml", ".rr.yml", ".rr.json")


@dataclass(frozen=True, order=True)
class CaseKey:
    """``(target, path)``; ``str()`` is ``<target>#<path>``.

    Labels cannot contain ``#`` and pseudo-targets are sanitized
    (:func:`pseudo_target`), so the *first* ``#`` always separates the two
    parts — the path itself may contain anything, ``#`` and ``::`` included.
    """

    target: str
    path: str

    def __str__(self) -> str:
        return f"{self.target}#{self.path}"

    @classmethod
    def parse(cls, text: str) -> CaseKey:
        target, sep, path = text.partition("#")
        if not sep or not target or not path:
            raise ValueError(f"not a case key (expected <target>#<path>): {text!r}")
        return cls(target, path)

    @property
    def synthetic(self) -> bool:
        return self.path == SYNTHETIC_PATH


def pseudo_target(prefix: str, name: str) -> str:
    """``record:<name>`` / ``suite:<name>``, with ``#`` (the key separator) replaced."""
    clean = unicodedata.normalize("NFC", name).strip().replace("#", "_")
    return prefix + (clean or "unnamed")


def case_path(classname: str, name: str) -> str:
    """The canonical path of a case: ``<classname>::<name>``, or ``<name>``.

    Both parts are taken as the XML parser returns them (already unescaped),
    normalized to Unicode NFC with surrounding whitespace stripped; internal
    whitespace is kept. ``[rr:ID]`` tags are removed from the name. The result
    is never split again, so names containing ``::`` or `` > `` are fine.
    A case with neither gets :data:`UNNAMED_PATH`.
    """
    cls = unicodedata.normalize("NFC", classname or "").strip()
    leaf = NAME_TAG.sub("", unicodedata.normalize("NFC", name or "")).strip()
    return (f"{cls}::{leaf}" if cls else leaf) or UNNAMED_PATH


def nodeid_to_case_path(nodeid: str) -> str:
    """The :func:`case_path` of a pytest ``nodeid``.

    Mirrors pytest's own ``mangle_test_address`` (the JUnit ``classname`` /
    ``name`` split), so ``pkg/test_m.py::TestK::test_s`` and
    ``pkg/test_m.py::test_a[x]`` map to the same paths a ``--junitxml`` report
    would file them under — ``pkg.test_m.TestK::test_s`` and
    ``pkg.test_m::test_a[x]`` — including the parametrization id.
    """
    path, open_bracket, params = nodeid.partition("[")
    names = path.split("::")
    names[0] = re.sub(r"\.py$", "", names[0].replace("/", "."))
    names[-1] += open_bracket + params
    classname = ".".join(names[:-1])
    return case_path(classname, names[-1])


def is_synthetic(case: TestCase) -> bool:
    """The target's single generated result (Bazel's fingerprint or ``rr.synthetic``)."""
    return case.synthetic


def is_target_scope(case: TestCase) -> bool:
    """A result about the whole target run (``rr.scope=target``), not a test case."""
    return case.scope == "target"


def declared_of(case: TestCase) -> tuple[str, ...]:
    """The ids a raw case declares: its ``declared`` tags plus any ``[rr:ID]``
    name tags (also for a hand-built :class:`TestCase`). Tags, never owners."""
    return tuple(dedupe([*case.declared, *name_tags(case.name)]))


def target_of(case: TestCase) -> str:
    """The target half of a case's key.

    The build label when the evidence is pinned to one (``bazel-testlogs``
    paths, ``rr wrap --target``, a record's ``target:``); otherwise
    ``record:<file stem>`` for records and ``suite:<testsuite name>`` (or the
    report's file stem) for anything else.
    """
    if case.target:
        return case.target
    base = os.path.basename(case.source or "")
    for suffix in _RECORD_SUFFIXES:
        if base.endswith(suffix):
            return pseudo_target(RECORD_PREFIX, base[: -len(suffix)])
    return pseudo_target(SUITE_PREFIX, case.suite or os.path.splitext(base)[0])


def is_unscoped(target: str) -> bool:
    """A ``suite:`` pseudo-target: evidence no build label can be matched to."""
    return target.startswith(SUITE_PREFIX)


def key_of(case: TestCase) -> CaseKey:
    """The :class:`CaseKey` a raw ingested case is filed under."""
    path = SYNTHETIC_PATH if is_synthetic(case) else case_path(case.classname, case.name)
    return CaseKey(target_of(case), path)


def file_of(case: TestCase) -> str:
    """The test source a case came from (``rr.file``), workspace-relative; "" if unknown."""
    return case.file or workspace_relative(case.properties.get(FILE_PROPERTY, ""))


class RunDims(NamedTuple):
    """Where in a Bazel test run a report sits. ``0`` means "not sharded" /
    "a single run" / "the final attempt" (``test.xml``)."""

    shard: int = 0
    shards: int = 0
    run: int = 0
    runs: int = 0
    attempt: int = 0


# Bazel's shard/run directories and attempt files (shared with target_from_path).
_SHARD_RUN = SHARD_RUN
_ATTEMPT = ATTEMPT


def _shard_run(part: str) -> RunDims | None:
    """``RunDims`` (without the attempt) of one path component, if it is a shard/run directory."""
    m = _SHARD_RUN.match(part)
    if not m:
        return None
    shard, shards, run, runs, run_only, runs_only = (int(g) if g else 0 for g in m.groups())
    return RunDims(shard, shards, run or run_only, runs or runs_only)


def run_dims_from_path(path: str) -> RunDims:
    """Parse ``shard_i_of_n``, ``run_k_of_n`` (also combined, as
    ``shard_i_of_n_run_k_of_m``) and ``test_attempts/attempt_N.xml``.

    ``.../name/shard_1_of_4_run_2_of_3/test_attempts/attempt_1.xml`` ->
    ``RunDims(shard=1, shards=4, run=2, runs=3, attempt=1)``; a ``test.xml``
    is the final attempt (``attempt=0``).
    """
    shard = shards = run = runs = attempt = 0
    for part in path.replace("\\", "/").split("/"):
        if dims := _shard_run(part):
            if dims.shards:
                shard, shards = dims.shard, dims.shards
            if dims.runs:
                run, runs = dims.run, dims.runs
        elif m := _ATTEMPT.match(part):
            attempt = int(m.group(1))
    return RunDims(shard, shards, run, runs, attempt)


@dataclass
class CaseRow:
    """Every observation of one :class:`CaseKey`, folded into one result."""

    key: CaseKey
    status: str
    declared: tuple[str, ...] = ()  # requirement ids its evidence names (union)
    level: str = ""
    synthetic: bool = False
    target_scope: bool = False
    file: str = ""
    line: int = 0
    flaky: bool = False  # an earlier attempt failed, the final one passed
    attempts: int = 1
    duplicate: bool = False  # the same key twice in one report, or in two shards
    message: str = ""
    sources: tuple[str, ...] = ()
    cases: tuple[TestCase, ...] = ()

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "case": str(self.key),
            "target": self.key.target,
            "path": self.key.path,
            "status": self.status,
            "declared": list(self.declared),
        }
        if self.level:
            out["level"] = self.level
        for flag in ("synthetic", "target_scope", "flaky", "duplicate"):
            if getattr(self, flag):
                out[flag] = True
        if self.attempts > 1:
            out["attempts"] = self.attempts
        if self.file:
            out["file"] = self.file
        if self.line:
            out["line"] = self.line
        if self.message and self.status in ("failed", "error"):
            out["message"] = self.message.splitlines()[0][:300]
        out["sources"] = list(self.sources)
        return out


def _worst(statuses: Iterable[str]) -> str:
    return max(statuses, key=lambda s: STATUS_ORDER.get(s, 0))


def index_cases(evidence: Evidence | Iterable[TestCase]) -> dict[CaseKey, CaseRow]:
    """One :class:`CaseRow` per key, sorted by key.

    * **Attempts** (``test_attempts/attempt_N.xml`` next to ``test.xml``): the
      final report is authoritative; an earlier failure under a final pass
      makes the row ``flaky``. A key seen only in earlier attempts of a run
      that has a final report (a crashed attempt's ``[target]`` result) is
      not a case: its failure makes the run's passing cases ``flaky``.
    * **Runs** (``--runs_per_test``) and **evidence roots**: the worst
      status wins — every repetition must pass.
    * **Shards**: their cases are unioned; one key in two shards (or twice in
      one report) is a ``duplicate``, folded worst-of — except a whole-run
      result (``[target]``, ``rr.scope=target``), which every shard has.

    Declared ids are the union over every observation. No ownership is
    decided here.
    """
    cases = evidence.cases if isinstance(evidence, Evidence) else list(evidence)
    observed = [(case, key_of(case), run_dims_from_path(case.source)) for case in cases]

    def slot_of(case: TestCase, key: CaseKey, dims: RunDims) -> tuple[str, str, int, int]:
        # Everything but the attempt: one (target, root, run, shard) slot.
        return (key.target, _report_dir(case.source, dims), dims.run, dims.shard)

    # The final report of a slot is authoritative for the whole slot: a key
    # seen only in earlier attempts (a crashed first attempt's [target]
    # result, say) is not a case of it. Its failure makes the slot flaky.
    finals = {slot_of(c, k, d) for c, k, d in observed if d.attempt == 0}
    failed_earlier: dict[tuple[str, str, int, int], set[str]] = {}  # slot -> sources of failed attempts
    by_key: dict[CaseKey, list[tuple[TestCase, RunDims]]] = {}
    for case, key, dims in observed:
        by_key.setdefault(key, []).append((case, dims))
    for key in list(by_key):
        seen = by_key[key]
        with_final = {slot_of(c, key, d) for c, d in seen if d.attempt == 0}
        orphan = [slot_of(c, key, d) in finals and slot_of(c, key, d) not in with_final for c, d in seen]
        for (c, d), lost in zip(seen, orphan):
            if lost and c.is_failure:
                failed_earlier.setdefault(slot_of(c, key, d), set()).add(c.source)
        kept = [obs for obs, lost in zip(seen, orphan) if not lost]
        if kept:
            by_key[key] = kept
        else:
            del by_key[key]

    rows: dict[CaseKey, CaseRow] = {}
    for key in sorted(by_key, key=lambda k: (natural_key(k.target), natural_key(k.path))):
        seen = by_key[key]
        slots: dict[tuple[str, str, int, int], list[tuple[TestCase, RunDims]]] = {}
        for case, dims in seen:
            slots.setdefault(slot_of(case, key, dims), []).append((case, dims))
        statuses, flaky, attempts, duplicate = [], False, 1, False
        for slot, obs in slots.items():
            final = [c for c, d in obs if d.attempt == 0]
            earlier = [c for c, d in sorted(obs, key=lambda o: o[1].attempt) if d.attempt]
            final = final or earlier[-1:]
            prior = [c for c in earlier if all(c is not f for f in final)]
            status = _worst(c.status for c in final)
            crashed = failed_earlier.get(slot, set())
            if status == "passed" and (any(c.is_failure for c in prior) or crashed):
                flaky = True
            attempts = max(attempts, len({c.source for c in prior} | crashed) + 1)
            statuses.append(status)
            duplicate = duplicate or len(final) > 1
        shards = {s for (_, _, _, s) in slots if s}
        whole_run = key.synthetic or any(is_target_scope(c) for c, _ in seen)
        # Each shard of a target that writes no JUnit gets its own [target]
        # result (and each may have its own exit-status): one per shard is
        # expected there, folded worst-of, not a duplicate case.
        duplicate = duplicate or (len(shards) > 1 and not whole_run)
        status = _worst(statuses)
        worst = next((c for c, _ in seen if c.status == status), seen[0][0])
        rows[key] = CaseRow(
            key=key,
            status=status,
            declared=tuple(dedupe([rid for c, _ in seen for rid in declared_of(c)])),
            level=next((c.level for c, _ in seen if c.level), ""),
            synthetic=key.synthetic,
            target_scope=any(is_target_scope(c) for c, _ in seen),
            file=next((f for f in (file_of(c) for c, _ in seen) if f), ""),
            line=next((c.line for c, _ in seen if c.line), 0),
            flaky=flaky,
            attempts=attempts,
            duplicate=duplicate,
            message=worst.message,
            sources=tuple(dedupe([c.source for c, _ in seen])),
            cases=tuple(c for c, _ in seen),
        )
    return rows


def _report_dir(source: str, dims: RunDims) -> str:
    """The directory of a target's run, above any run/shard/attempt level —
    so ``attempt_1.xml`` and ``test.xml`` of one run share a slot, while the
    same target from two evidence roots does not."""
    parts = source.replace("\\", "/").split("/")[:-1]
    while parts and (_shard_run(parts[-1]) or parts[-1] == "test_attempts"):
        parts.pop()
    return "/".join(parts)
