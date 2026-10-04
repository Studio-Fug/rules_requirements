# SPDX-License-Identifier: AGPL-3.0-or-later
"""Rust libtest (``cargo test`` / ``rust_test``) plain-text output ingestor.

libtest has no stable machine-readable output, but its default "pretty" format
is stable and line-oriented::

    running 3 tests
    test parse::rejects_empty ... ok
    test parse::accepts_celsius ... FAILED
    test slow::soak ... ignored, needs hardware

    failures:

    ---- parse::accepts_celsius stdout ----
    thread 'parse::accepts_celsius' panicked at src/lib.rs:12:5: ...

Requirement traces come from the ``rr::verifies!`` macro in the
``rules_requirements`` Rust crate, which appends one JSON line per call to the
file named by ``$RR_TRACE_FILE`` (see :func:`merge_trace`). The
:mod:`rules_requirements.hooks.wrap` test wrapper sets that variable, runs the
binary, and writes JUnit — so this ingestor is mostly used by the wrapper, and
directly only for ``*.libtest.txt`` captures.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable

from rules_requirements.ingest import (
    FAILED,
    PASSED,
    SKIPPED,
    Ingestor,
    TestCase,
    apply_properties,
)

_RESULT = re.compile(
    r"^test (?P<name>\S+)(?: - should panic)? \.\.\. (?P<result>ok|FAILED|ignored)(?:, (?P<reason>.*))?$"
)
_STDOUT_HEADER = re.compile(r"^---- (?P<name>\S+) stdout ----$")
# With --nocapture, libtest prints "test name ... " first — the test's live
# output may continue on that same line — and the result on a line of its own
# once the test finishes.
_PENDING = re.compile(r"^test (?P<name>\S+)(?: - should panic)? \.\.\.(?: .*)?$")
_BARE_RESULT = re.compile(r"^(?P<result>ok|FAILED|ignored)(?:, (?P<reason>.*))?$")
_FAILURE_ENTRY = re.compile(r"^    (?P<name>\S+)$")


def parse_libtest(text: str, target: str = "", source: str = "") -> list[TestCase]:
    cases: dict[str, TestCase] = {}

    def record(name: str, result: str, reason: str = "") -> None:
        module, _, leaf = name.rpartition("::")
        status = {"ok": PASSED, "FAILED": FAILED, "ignored": SKIPPED}[result]
        cases[name] = TestCase(name=leaf, classname=module, status=status, message=reason, source=source, target=target)

    in_output = False  # inside a "---- name stdout ----" block of captured output
    in_failure_list = False  # the final "failures:" list of failed test names
    # --nocapture: the test's name is printed first and its verdict on a line
    # of its own when it finishes — but the test may print "ok" itself, so the
    # *last* bare verdict before the next test (or the summary) counts.
    pending: str | None = None
    verdict: tuple[str, str] | None = None

    def settle() -> None:
        nonlocal pending, verdict
        if pending is not None and verdict is not None:
            record(pending, *verdict)
        pending, verdict = None, None

    for line in text.splitlines():
        stripped = line.rstrip()
        if _STDOUT_HEADER.match(line):
            settle()
            in_output, in_failure_list = True, False
            continue
        if line.startswith(("failures:", "successes:", "test result:", "running ")):
            settle()
            in_output = False
            in_failure_list = stripped == "failures:"
            continue
        if in_output:
            continue  # e.g. trybuild prints its own "test x ... ok" lines here
        if in_failure_list:
            f = _FAILURE_ENTRY.match(stripped)
            if f:
                # libtest's own list of failed tests is authoritative.
                name = f.group("name")
                prev = cases.get(name)
                record(name, "FAILED", prev.message if prev else "")
            elif stripped:
                in_failure_list = False
            continue
        m = _RESULT.match(stripped)
        if m is not None:
            settle()
            record(m.group("name"), m.group("result"), m.group("reason") or "")
            continue
        p = _PENDING.match(stripped)
        if p:
            settle()
            pending = p.group("name")
            continue
        b = _BARE_RESULT.match(stripped.strip()) if pending else None
        if b is not None:
            verdict = (b.group("result"), b.group("reason") or "")
    settle()
    # Attach captured output of failures as the failure message.
    current = None
    buf: list[str] = []

    def flush() -> None:
        if current and current in cases and cases[current].status == FAILED:
            cases[current].message = "\n".join(buf).strip()

    for line in text.splitlines():
        m = _STDOUT_HEADER.match(line)
        if m:
            flush()
            current, buf = m.group("name"), []
        elif current is not None:
            if line.startswith(("failures:", "successes:")) or line.startswith("test result:"):
                flush()
                current, buf = None, []
            else:
                buf.append(line)
    flush()
    return list(cases.values())


def trace_ids(rec: dict[str, Any]) -> list[str]:
    """Every id one ``rr::verifies!`` trace line names, in any shape a producer
    writes: ``"requirement": "<id>"`` (0.3), the 0.2 list ``"requirements":
    [...]``, and also a string under ``requirements`` or a list under
    ``requirement``. Values are returned as written (:func:`~rules_requirements.ingest.apply_properties`
    splits ``"A,B"``); nothing a line names is dropped, so a line naming two
    ids always reaches attribution as a multi-tag, never as one id or none.
    """
    out: list[str] = []
    for key in ("requirement", "requirements"):
        value = rec.get(key)
        for item in value if isinstance(value, (list, tuple)) else [value]:
            if item is not None and item != "":
                out.append(str(item))
    return out


def merge_trace(cases: list[TestCase], trace_text: str) -> list[TestCase]:
    """Apply ``rr::verifies!`` trace lines to the matching cases.

    Each line is JSON ``{"test": "<module::name>", "requirement": "PR-4",
    "level": "...", "artifact": {...}}``; ``test`` is the libtest thread name,
    which is the test's full path. The 0.2 list form (``"requirements":
    [...]``) is still read. Every id becomes a declared tag of the case, so a
    list of two, or two lines with different ids, is a ``multi-tag`` case.
    """
    by_name = {(f"{c.classname}::{c.name}" if c.classname else c.name): c for c in cases}
    for line in trace_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        case = by_name.get(str(rec.get("test", "")))
        if case is None:
            continue
        props = [("requirement", r) for r in trace_ids(rec)]
        if rec.get("level"):
            props.append(("level", str(rec["level"])))
        props += [(f"artifact.{k}", str(v)) for k, v in (rec.get("artifact") or {}).items()]
        apply_properties(case, props)
    return cases


class LibtestIngestor(Ingestor):
    name = "libtest"
    suffixes = (".libtest.txt", ".libtest")

    def ingest(self, path: str) -> Iterable[TestCase]:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        cases = parse_libtest(text, source=path)
        trace = path.rsplit(".libtest", 1)[0] + ".rrtrace.jsonl"
        try:
            with open(trace, encoding="utf-8") as fh:
                merge_trace(cases, fh.read())
        except OSError:
            pass
        return cases
