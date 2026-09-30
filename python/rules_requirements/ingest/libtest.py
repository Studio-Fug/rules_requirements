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
from typing import Iterable

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


def parse_libtest(text: str, target: str = "", source: str = "") -> list[TestCase]:
    cases: dict[str, TestCase] = {}
    for line in text.splitlines():
        m = _RESULT.match(line.rstrip())
        if not m:
            continue
        status = {"ok": PASSED, "FAILED": FAILED, "ignored": SKIPPED}[m.group("result")]
        name = m.group("name")
        module, _, leaf = name.rpartition("::")
        cases[name] = TestCase(
            name=leaf,
            classname=module,
            status=status,
            message=m.group("reason") or "",
            source=source,
            target=target,
        )
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


def merge_trace(cases: list[TestCase], trace_text: str) -> list[TestCase]:
    """Apply ``rr::verifies!`` trace lines to the matching cases.

    Each line is JSON ``{"test": "<module::name>", "requirements": [...],
    "level": "...", "artifact": {...}}``; ``test`` is the libtest thread name,
    which is the test's full path.
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
        case = by_name.get(str(rec.get("test", "")))
        if case is None:
            continue
        props = [("requirement", r) for r in rec.get("requirements", [])]
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
