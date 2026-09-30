# SPDX-License-Identifier: AGPL-3.0-or-later
"""Run a test binary and convert its output into traceability JUnit.

Used by the ``rr_wrapped_test`` / ``rr_rust_test`` Bazel macros, and usable
directly::

    python -m rules_requirements.hooks.wrap --format libtest -- ./my_tests --test-threads=4

It sets ``$RR_TRACE_FILE`` (where ``rr::verifies!`` records traces), runs the
binary with the remaining arguments, echoes its output, parses it with the
chosen format, merges the traces, writes JUnit to ``$XML_OUTPUT_FILE`` (or
``--junit-xml``), and exits with the binary's exit code — so the wrapper never
turns a failing test green or a passing one red.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile

from rules_requirements.hooks.junit_writer import JUnitWriter
from rules_requirements.ingest import TestCase
from rules_requirements.ingest.libtest import merge_trace, parse_libtest

FORMATS = ("libtest",)


def _resolve(binary: str) -> str:
    if os.path.exists(binary):
        return os.path.abspath(binary)
    # Under `bazel run`/`bazel test`, $(rootpath) paths are relative to the
    # runfiles root of the main repo; external ones start with "../".
    for base in (os.environ.get("RUNFILES_DIR", ""), os.environ.get("TEST_SRCDIR", "")):
        if not base:
            continue
        ws = os.environ.get("TEST_WORKSPACE", "_main")
        for cand in (os.path.join(base, ws, binary), os.path.join(base, binary)):
            cand = os.path.normpath(cand)
            if os.path.exists(cand):
                return cand
    return binary


def _traced(trace: str) -> dict[str, list[str]]:
    """test name -> ids recorded by rr::verifies! (in first-seen order)."""
    out: dict[str, list[str]] = {}
    for line in trace.splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        ids = out.setdefault(str(rec.get("test", "")), [])
        ids.extend(i for i in rec.get("requirements", []) if i not in ids)
    out.pop("", None)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--format", choices=FORMATS, default="libtest")
    parser.add_argument("--junit-xml", default="")
    parser.add_argument("--suite", default="")
    parser.add_argument("--target", default="", help="label to stamp on every case")
    parser.add_argument("--level", default="", help="default level for cases without one")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    cmd = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not cmd:
        parser.error("missing test binary")
    cmd[0] = _resolve(cmd[0])

    tmpdir = os.environ.get("TEST_TMPDIR") or tempfile.mkdtemp(prefix="rr-wrap-")
    trace_path = os.path.join(tmpdir, "rr_trace.jsonl")
    if os.path.exists(trace_path):
        os.remove(trace_path)
    env = dict(os.environ, RR_TRACE_FILE=trace_path)
    # The wrapped binary must not clobber the JUnit we are about to write.
    env.pop("XML_OUTPUT_FILE", None)
    proc = subprocess.run(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
    text = proc.stdout.decode("utf-8", "replace")
    sys.stdout.write(text)
    sys.stdout.flush()

    cases = parse_libtest(text, target=args.target)
    try:
        with open(trace_path, encoding="utf-8") as fh:
            trace = fh.read()
    except OSError:
        trace = ""
    merge_trace(cases, trace)
    # Tests that recorded traces but never reported a result crashed the
    # binary mid-run (abort, stack overflow): they are errors, not absent.
    reported = {f"{c.classname}::{c.name}" if c.classname else c.name for c in cases}
    for test, ids in _traced(trace).items():
        if test not in reported:
            module, _, leaf = test.rpartition("::")
            cases.append(
                TestCase(
                    name=leaf,
                    classname=module,
                    status="error",
                    requirements=tuple(ids),
                    message=f"no result reported: the test binary exited with {proc.returncode} while it ran",
                    target=args.target,
                )
            )
    if proc.returncode != 0 and cases and not any(c.is_failure for c in cases):
        cases.append(
            TestCase(
                name="exit-status",
                status="error",
                message=f"test binary exited with {proc.returncode} although every reported test passed\n"
                + text[-2000:],
                target=args.target,
            )
        )

    suite = args.suite or (args.target.rsplit(":", 1)[-1] if args.target else os.path.basename(cmd[0]))
    writer = JUnitWriter(suite, default_level=args.level)
    for c in cases:
        writer.add(
            c.name,
            c.requirements,
            status=c.status,
            message=c.message,
            level=c.level,
            artifact=c.artifact,
            classname=c.classname,
        )
    if not cases:
        # Nothing parseable (e.g. the binary crashed before running tests):
        # record one synthetic case so the failure is visible in reports.
        status = "passed" if proc.returncode == 0 else "error"
        writer.add(writer.suite, [], status=status, message=f"exit code {proc.returncode}\n{text[-2000:]}")
    out = args.junit_xml or os.environ.get("XML_OUTPUT_FILE", "")
    if out:
        writer.write(out)
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
