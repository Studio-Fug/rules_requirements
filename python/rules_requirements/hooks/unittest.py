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
import re
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


def _module_name(module: str) -> str:
    if module == "__main__":  # run as a script: name the module after its file
        main_file = getattr(sys.modules.get("__main__"), "__file__", "") or ""
        return os.path.splitext(os.path.basename(main_file))[0] or module
    return module


_HOLDER = re.compile(r"^(?P<fixture>\w+) \((?P<path>[^()]+)\)$")


def _holder_trace(description: str) -> tuple[str, str, Any]:
    """(name, classname, trace) for a class/module fixture error.

    unittest reports ``setUpClass`` / ``setUpModule`` failures as an
    ``_ErrorHolder`` whose description is e.g. ``"setUpClass (pkg.mod.Cls)"``;
    the class's ``@rr.verifies`` ids still apply to that failure.
    """
    m = _HOLDER.match(description or "")
    if not m:
        return description or "fixture", "", {"ids": [], "level": "", "artifact": {}}
    path = m.group("path")
    module, _, attr = path.rpartition(".")
    owner: Any = sys.modules.get(path)
    if owner is None:  # "pkg.mod.Outer.Inner": find the longest importable module prefix
        parts = path.split(".")
        for i in range(len(parts) - 1, 0, -1):
            obj: Any = sys.modules.get(".".join(parts[:i]))
            for name in parts[i:]:
                obj = getattr(obj, name, None)
            if obj is not None:
                owner, module, attr = obj, ".".join(parts[:i]), ".".join(parts[i:])
                break
    rr = getattr(owner, "__rr__", None) or {}
    trace = {"ids": list(rr.get("ids", [])), "level": rr.get("level", ""), "artifact": dict(rr.get("artifact") or {})}
    classname = f"{_module_name(module)}.{attr}" if module else _module_name(path)
    return m.group("fixture"), classname, trace


class JUnitResult(unittest.TextTestResult):
    """Collects every outcome into a :class:`JUnitWriter`."""

    def __init__(self, stream: Any, descriptions: bool, verbosity: int, writer: JUnitWriter):
        super().__init__(stream, descriptions, verbosity)
        self.writer = writer
        self._start: float | None = None
        self._failed_subtests: list[tuple[str, str]] = []  # (status, label) of the running test's
        self._own_outcome = False  # whether the running test recorded a result under its own key

    def startTest(self, test: unittest.TestCase) -> None:  # noqa: N802
        self._start = time.monotonic()
        self._failed_subtests, self._own_outcome = [], False
        super().startTest(test)

    def stopTest(self, test: unittest.TestCase) -> None:  # noqa: N802
        # unittest reports no outcome for a test itself once one of its
        # subtests failed: record one under the test's own key, so the key
        # reads failed, not missing, in the runs where a subtest fails.
        if self._failed_subtests and not self._own_outcome and isinstance(test, unittest.TestCase):
            status = "failed" if any(st == "failed" for st, _ in self._failed_subtests) else "error"
            labels = ", ".join(label for _, label in self._failed_subtests)
            self._record(test, status, f"subtest(s) failed: {labels}")
        super().stopTest(test)

    def _record(self, test: Any, status: str, message: str = "", name: str = "") -> None:
        if isinstance(test, unittest.TestCase):
            tr: Any = trace_of(test)
            name = name or test._testMethodName
            classname = f"{_module_name(type(test).__module__)}.{type(test).__qualname__}"
            self._own_outcome = self._own_outcome or name == test._testMethodName
        else:  # a class/module fixture error
            name, classname, tr = _holder_trace(getattr(test, "description", str(test)))
        self.writer._append(
            name,
            tr["ids"],
            status=status,
            message=message,
            duration=(time.monotonic() - self._start) if self._start is not None else 0.0,
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

    def addSubTest(self, test: unittest.TestCase, subtest: Any, err: Any) -> None:  # noqa: N802
        super().addSubTest(test, subtest, err)
        if err is not None:  # a failing subtest fails its test; passing ones are not reported
            status = "failed" if issubclass(err[0], test.failureException) else "error"
            label = subtest._subDescription() if hasattr(subtest, "_subDescription") else str(subtest)
            self._failed_subtests.append((status, label))
            self._record(
                test, status, "".join(traceback.format_exception(*err)), name=f"{test._testMethodName} {label}"
            )

    def addSkip(self, test: unittest.TestCase, reason: str) -> None:  # noqa: N802
        super().addSkip(test, reason)
        self._record(test, "skipped", reason)

    def addExpectedFailure(self, test: unittest.TestCase, err: Any) -> None:  # noqa: N802
        # A test documenting a known defect is not evidence the requirement holds.
        super().addExpectedFailure(test, err)
        self._record(test, "skipped", "expected failure")

    def addUnexpectedSuccess(self, test: unittest.TestCase) -> None:  # noqa: N802
        super().addUnexpectedSuccess(test)
        self._record(test, "failed", "unexpected success")


def run(suite: unittest.TestSuite, junit_xml: str = "", suite_name: str = "unittest", verbosity: int = 2) -> bool:
    """Run ``suite``; write JUnit to ``junit_xml`` (or ``$XML_OUTPUT_FILE``)."""
    writer = JUnitWriter(suite_name, default_level="", file="")
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
