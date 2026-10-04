# SPDX-License-Identifier: AGPL-3.0-or-later
"""Run a test binary and convert its output into traceability JUnit.

Used by the ``rr_wrapped_test`` / ``rr_rust_test`` Bazel macros, and usable
directly::

    python -m rules_requirements.hooks.wrap --format libtest -- ./my_tests --test-threads=4
    python -m rules_requirements.hooks.wrap --format junit --junit-in out/junit.xml -- ./run_suite.sh

It sets ``$RR_TRACE_FILE`` (where ``rr::verifies!`` records traces), runs the
binary with the remaining arguments, echoes its output, parses it with the
chosen format, merges the traces, writes JUnit to ``$XML_OUTPUT_FILE`` (or
``--junit-xml``), and exits with the binary's exit code — so the wrapper never
turns a failing test green or a passing one red.

The ``exit-status`` error case a non-zero exit adds when no reported test
failed declares no requirement: it is a target-scope result
(``rr.scope=target``) that taints every case claimed on the target. A test
that traced and then died without a result keeps its own declared id.

``--format junit`` is for runners that write JUnit themselves, to a fixed path
(``--junit-in``): the wrapper copies that file to the output and adds the same
``exit-status`` error case when the runner exits non-zero although no case in
its report failed. A missing or non-JUnit report is recorded as one error
case, and a report without cases from a clean exit as one synthetic passed
case, so a run never ends without evidence.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from xml.etree import ElementTree as ET

from rules_requirements.hooks.ids import multiple_warning, split_ids
from rules_requirements.hooks.junit_writer import JUnitWriter, _merge, _read_root
from rules_requirements.ingest import TestCase
from rules_requirements.ingest.junit import SYNTHETIC, TARGET_SCOPE, JUnitIngestor
from rules_requirements.ingest.libtest import merge_trace, parse_libtest, trace_ids

FORMATS = ("libtest", "junit")


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


_TEST_PATH = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_][A-Za-z0-9_]*)*$")


def _traced(trace: str) -> dict[str, list[str]]:
    """test name -> ids recorded by rr::verifies! (in first-seen order)."""
    out: dict[str, list[str]] = {}
    for line in trace.splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        ids = out.setdefault(str(rec.get("test", "")), [])
        ids.extend(i for i in split_ids(trace_ids(rec)) if i not in ids)
    out.pop("", None)
    return out


def main(argv: list[str] | None = None, prog: str | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog=prog, description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--format", choices=FORMATS, default="libtest")
    parser.add_argument("--junit-xml", default="")
    parser.add_argument("--suite", default="")
    parser.add_argument(
        "--target",
        default="",
        help="the test's label; its name is the default --suite. It is not written into the report: "
        "a case's label comes from where its report lies (bazel-testlogs/...)",
    )
    parser.add_argument("--level", default="", help="default level for cases without one")
    parser.add_argument(
        "--junit-in",
        default="",
        help="--format junit: where the command writes its JUnit ($VARS expanded; relative to the working directory)",
    )
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    cmd = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not cmd:
        parser.error("missing test binary")
    if args.format == "junit" and not args.junit_in:
        parser.error("--format junit needs --junit-in PATH")
    cmd[0] = _resolve(cmd[0])
    junit_in = os.path.abspath(os.path.expandvars(args.junit_in)) if args.junit_in else ""
    if junit_in and os.path.exists(junit_in):
        os.remove(junit_in)  # a report left by an earlier run is not this run's evidence

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
    suite = args.suite or (args.target.rsplit(":", 1)[-1] if args.target else os.path.basename(cmd[0]))
    out = args.junit_xml or os.environ.get("XML_OUTPUT_FILE", "")
    if args.format == "junit":
        _copy_junit(junit_in, out, proc.returncode, text, suite, args.target, args.level)
        return proc.returncode

    cases = parse_libtest(text, target=args.target)
    try:
        with open(trace_path, encoding="utf-8") as fh:
            trace = fh.read()
    except OSError:
        trace = ""
    merge_trace(cases, trace)
    reported = {f"{c.classname}::{c.name}" if c.classname else c.name for c in cases}
    for test, ids in _traced(trace).items():
        if test in reported:
            continue
        if proc.returncode != 0 and _TEST_PATH.match(test):
            # A test that recorded traces but never reported a result was
            # running when the binary died (abort, stack overflow): its own
            # declared id, and no other test's.
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
        else:
            # rr::verifies! from a spawned thread or async task (its thread is
            # not named after the test): the ids belong to no case, so they
            # are dropped.
            print(
                f"rr wrap: warning: traces from thread {test!r} match no test and are dropped; "
                "call rr::verifies! on the test's own thread",
                file=sys.stderr,
            )
    exit_case = _exit_status(cases, proc.returncode, text, args.target)
    if exit_case is not None:
        cases.append(exit_case)

    writer = JUnitWriter(suite, default_level=args.level, file="")
    for c in cases:
        writer._append(
            c.name,
            c.requirements,
            status=c.status,
            message=c.message,
            level=c.level,
            artifact=c.artifact,
            classname=c.classname,
            properties=c.properties,
        )
    _warn_multi_id(cases)
    if not cases:
        # Nothing parseable (e.g. the binary crashed before running tests):
        # record one synthetic case so the failure is visible in reports. It
        # is the target's single whole-run result, as Bazel's generated
        # test.xml would be.
        status = "passed" if proc.returncode == 0 else "error"
        writer._append(writer.suite, (), status, f"exit code {proc.returncode}\n{text[-2000:]}", properties=SYNTHETIC)
    if out:
        writer.write(out)
    return proc.returncode


def _exit_status(cases: list[TestCase], returncode: int, text: str, target: str) -> TestCase | None:
    """The ``exit-status`` error case for a run that exited non-zero although
    no reported test failed, else None.

    Every reported test passed, yet the binary failed: the run is suspect.
    The case declares NO requirement (it is no test case, and naming the ids
    the run traced would make it a case of several requirements); it is
    target-scope (``rr.scope=target``), which taints every case claimed on
    the target, so each requirement fails through its own cases.
    """
    if returncode == 0 or any(c.is_failure for c in cases):
        return None
    return TestCase(
        name="exit-status",
        status="error",
        message=f"test binary exited with {returncode} although no reported test failed\n" + text[-2000:],
        target=target,
        properties=dict(TARGET_SCOPE),
    )


def _warn_multi_id(cases: list[TestCase]) -> None:
    """RR-E101 on stderr for each case that records two or more ids
    (``rr::verifies!`` called with different ids in one test)."""
    for c in cases:
        if c.name == "exit-status" or len(c.requirements) < 2:
            continue
        test = f"{c.classname}::{c.name}" if c.classname else c.name
        text = str(multiple_warning(test, list(c.requirements)))
        print(text.replace("rr: ", "rr wrap: warning: ", 1), file=sys.stderr)


def _copy_junit(junit_in: str, out: str, returncode: int, text: str, suite: str, target: str, level: str) -> None:
    """``--format junit``: copy the runner's report to ``out``, with ``level``
    as the default level of its suites and the exit taint added."""
    writer = JUnitWriter(suite, default_level=level, file="")
    if not os.path.exists(junit_in) or os.path.getsize(junit_in) == 0:
        # The runner wrote no report where it was expected to: whatever its
        # exit code, nothing it verified can be read.
        writer._append(
            suite,
            (),
            "error",
            f"no JUnit report at {junit_in} (exit code {returncode})",
            properties=TARGET_SCOPE,
        )
        if out:
            writer.write(out)
        return
    if not out:
        return
    exit_case = _exit_status(list(JUnitIngestor().ingest(junit_in)), returncode, text, target)
    root: ET.Element | None = None
    try:
        root = _read_root(junit_in)
    except (SyntaxError, OSError):
        pass  # unreadable: copied as is; ingestion reports it as an error of its own
    except ValueError as exc:
        # Well-formed, but not JUnit (ingestion would skip it): nothing it
        # verified can be read, which must not pass silently.
        writer._append(suite, (), "error", f"{exc} (exit code {returncode})", properties=TARGET_SCOPE)
        writer.write(out)
        return
    if root is not None and root.find(".//testcase") is None and exit_case is None:
        # A report without a single case, and a clean exit: one synthetic
        # case (as for a libtest binary that printed nothing) so the run
        # still leaves evidence.
        writer._append(
            suite, (), "passed", f"no test cases in {junit_in} (exit code {returncode})", properties=SYNTHETIC
        )
        writer.write(out)
        return
    if not level and exit_case is None:
        root = None
    if root is None:  # nothing to add: the report as the runner wrote it
        if not os.path.exists(out) or not os.path.samefile(junit_in, out):
            shutil.copyfile(junit_in, out)
        return
    if level:
        for el in root.iter("testsuite"):
            props = el.find("properties")
            if props is None:
                props = ET.Element("properties")
                el.insert(0, props)
            if not any(p.get("name") == "level" for p in props.findall("property")):
                ET.SubElement(props, "property", name="level", value=level)
    if exit_case is not None:
        writer._append(exit_case.name, (), exit_case.status, exit_case.message, properties=exit_case.properties)
        root = _merge(root, writer.to_element())
    tree = ET.ElementTree(root)
    tree.write(out, encoding="utf-8", xml_declaration=True)


if __name__ == "__main__":
    raise SystemExit(main())
