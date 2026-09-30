# SPDX-License-Identifier: AGPL-3.0-or-later
"""Traceability for plain :mod:`unittest` suites.

Decorate tests (or whole ``TestCase`` classes) with
:func:`rules_requirements.rr.verifies`::

    import unittest
    from rules_requirements import rr

    class ThermostatTest(unittest.TestCase):
        @rr.verifies("REQ-3", level="simulation")
        def test_rejects_setpoint_above_limit(self): ...

    if __name__ == "__main__":
        rr.unittest_main()

:func:`main` runs the module's tests (or discovers under a directory) with a
result collector that writes JUnit XML — with ``requirement`` / ``level``
properties — to ``$XML_OUTPUT_FILE`` (Bazel) or ``--junit-xml PATH``. The same
decorators are honoured when pytest collects the ``TestCase``.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import traceback
import unittest
from typing import Any

from rules_requirements.hooks.junit_writer import JUnitWriter


def trace_of(test: unittest.TestCase) -> Any:
    method = getattr(test, getattr(test, "_testMethodName", ""), None)
    ids: list[str] = []
    level = ""
    artifact: dict[str, str] = {}
    for holder in (getattr(method, "__func__", method), type(test)):
        rr = getattr(holder, "__rr__", None) or {}
        ids.extend(rr.get("ids", ()))
        level = level or rr.get("level", "")
        for k, v in (rr.get("artifact") or {}).items():
            artifact.setdefault(k, v)
    return {"ids": list(dict.fromkeys(ids)), "level": level, "artifact": artifact}


class JUnitResult(unittest.TextTestResult):
    """Collects every outcome into a :class:`JUnitWriter`."""

    def __init__(self, stream: Any, descriptions: bool, verbosity: int, writer: JUnitWriter):
        super().__init__(stream, descriptions, verbosity)
        self.writer = writer
        self._start = 0.0

    def startTest(self, test: unittest.TestCase) -> None:  # noqa: N802
        self._start = time.monotonic()
        super().startTest(test)

    def _record(self, test: Any, status: str, message: str = "") -> None:
        tr: Any = trace_of(test) if isinstance(test, unittest.TestCase) else {"ids": [], "level": "", "artifact": {}}
        name = getattr(test, "_testMethodName", str(test))
        module = type(test).__module__
        if module == "__main__":  # run as a script: name the module after its file
            main_file = getattr(sys.modules.get("__main__"), "__file__", "") or ""
            module = os.path.splitext(os.path.basename(main_file))[0] or module
        classname = f"{module}.{type(test).__qualname__}"
        self.writer.add(
            name,
            tr["ids"],
            status=status,
            message=message,
            duration=time.monotonic() - self._start,
            level=tr["level"],
            artifact=tr["artifact"],
            classname=classname,
        )

    def addSuccess(self, test: unittest.TestCase) -> None:  # noqa: N802
        super().addSuccess(test)
        self._record(test, "passed")

    def addFailure(self, test: unittest.TestCase, err: Any) -> None:  # noqa: N802
        super().addFailure(test, err)
        self._record(test, "failed", "".join(traceback.format_exception(*err)))

    def addError(self, test: unittest.TestCase, err: Any) -> None:  # noqa: N802
        super().addError(test, err)
        self._record(test, "error", "".join(traceback.format_exception(*err)))

    def addSkip(self, test: unittest.TestCase, reason: str) -> None:  # noqa: N802
        super().addSkip(test, reason)
        self._record(test, "skipped", reason)

    def addExpectedFailure(self, test: unittest.TestCase, err: Any) -> None:  # noqa: N802
        super().addExpectedFailure(test, err)
        self._record(test, "passed", "expected failure")

    def addUnexpectedSuccess(self, test: unittest.TestCase) -> None:  # noqa: N802
        super().addUnexpectedSuccess(test)
        self._record(test, "failed", "unexpected success")


def run(suite: unittest.TestSuite, junit_xml: str = "", suite_name: str = "unittest", verbosity: int = 2) -> bool:
    """Run ``suite``; write JUnit to ``junit_xml`` (or ``$XML_OUTPUT_FILE``)."""
    writer = JUnitWriter(suite_name, default_level="")
    runner = unittest.TextTestRunner(
        stream=sys.stderr,
        verbosity=verbosity,
        resultclass=lambda s, d, v: JUnitResult(s, d, v, writer),  # type: ignore[arg-type]
    )
    result = runner.run(suite)
    path = junit_xml or os.environ.get("XML_OUTPUT_FILE", "")
    if path:
        writer.write(path)
    return result.wasSuccessful()


def main(module: str | None = "__main__", argv: list[str] | None = None) -> int:
    """Entry point: run a module's tests, or ``--discover DIR``."""
    parser = argparse.ArgumentParser(description="unittest with traceability JUnit output")
    parser.add_argument("--junit-xml", default="")
    parser.add_argument("--discover", default="", help="discover tests under this directory")
    parser.add_argument("--pattern", default="test*.py")
    parser.add_argument("-q", "--quiet", action="store_true")
    args, rest = parser.parse_known_args(sys.argv[1:] if argv is None else argv)
    loader = unittest.TestLoader()
    if args.discover:
        suite = loader.discover(args.discover, pattern=args.pattern)
        name = os.path.basename(os.path.abspath(args.discover))
    else:
        mod = sys.modules[module or "__main__"]
        suite = loader.loadTestsFromNames(rest, mod) if rest else loader.loadTestsFromModule(mod)
        name = os.path.splitext(os.path.basename(getattr(mod, "__file__", "") or "unittest"))[0]
    ok = run(suite, args.junit_xml, name, 1 if args.quiet else 2)
    return 0 if ok else 1
