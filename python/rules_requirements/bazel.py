# SPDX-License-Identifier: AGPL-3.0-or-later
"""Build-time helpers behind the Bazel rules in ``@rules_requirements//rr:defs.bzl``.

``run-tests``
    Run test executables inside a build action (``rr_evidence``), emulating
    the environment ``bazel test`` provides, and lay their JUnit out like
    ``bazel-testlogs`` (``testlogs/<pkg>/<name>/test.xml``) so target labels
    are recovered exactly as for real test logs.
``golden``
    Compare a generated file with a checked-in golden (``rr_golden_test``),
    or overwrite the golden with ``--update`` (``bazel run :<name>.update``).
"""

from __future__ import annotations

import argparse
import difflib
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any

from rules_requirements.hooks.junit_writer import JUnitWriter
from rules_requirements.ingest.junit import SYNTHETIC, TARGET_SCOPE


def _label_dir(label: str) -> str:
    """``@@repo+//pkg/sub:name`` -> ``external/repo+/pkg/sub/name``."""
    repo, _, rest = label.lstrip("@").partition("//")
    pkg, _, name = rest.partition(":")
    parts = (["external", repo] if repo else []) + ([pkg] if pkg else []) + [name]
    return "/".join(parts)


def _norm_label(label: str) -> str:
    return label[2:] if label.startswith("@@//") else (label[1:] if label.startswith("@//") else label)


# After our own timeout: how long the test gets to end on SIGTERM before
# SIGKILL, and how long its output pipe is drained once it has ended.
_KILL_GRACE = 2.0
_DRAIN = 1.0


def _drain(fd: int, chunks: list[bytes], done: threading.Event) -> None:
    """Reads ``fd`` into ``chunks`` until EOF or until ``done`` is set.

    Once the caller has its output (``done``), the next read ends the thread
    and closes the pipe instead of buffering what a process the test left
    behind keeps writing: that writer then gets EPIPE.
    """
    try:
        while not done.is_set():
            data = os.read(fd, 65536)
            if not data or done.is_set():
                return
            chunks.append(data)
    except OSError:
        return
    finally:
        os.close(fd)


def _run_one(exe: str, cwd: str, env: dict[str, str], timeout: float) -> tuple[int, str]:
    """Runs one test executable: (exit code, combined output).

    The test stays in this action's process group, so when Bazel kills the
    action (a cancelled build, the action's own timeout) the kill reaches the
    test and everything it started. On ``timeout`` the test itself gets
    SIGTERM (an ``rr_case.h`` runner then kills the case it is running), and
    SIGKILL if it is still there after a short grace. Only the test itself is
    waited for: output is read on a thread, so a process the test left behind
    that still holds the output pipe (a daemon, ``setsid``) never blocks the
    action, and what was read by then is kept; the pipe is then closed at
    the next thing such a process writes, so it is not buffered for the rest
    of the action.
    """
    read_fd, write_fd = os.pipe()
    try:
        proc = subprocess.Popen([exe], cwd=cwd, env=env, stdout=write_fd, stderr=write_fd)
    except BaseException:
        os.close(read_fd)
        raise
    finally:
        os.close(write_fd)
    chunks: list[bytes] = []
    done = threading.Event()
    reader = threading.Thread(target=_drain, args=(read_fd, chunks, done), daemon=True)
    reader.start()
    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.terminate()
        try:
            proc.wait(timeout=_KILL_GRACE)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    reader.join(_DRAIN)
    done.set()
    out = b"".join(list(chunks)).decode("utf-8", "replace")
    if timed_out:
        return -1, out + f"\nTIMEOUT after {timeout}s"
    return proc.returncode, out


def run_tests(out: str, tests: list[str], timeout: float, envs: list[str] | None = None) -> int:
    out = os.path.abspath(out)  # tests run with their runfiles dir as cwd
    os.makedirs(out, exist_ok=True)
    execroot = os.getcwd()
    failures = 0
    for spec in tests:
        label, exe, workspace = spec.split("=", 2)
        label = _norm_label(label)
        exe = os.path.join(execroot, exe)
        runfiles = exe + ".runfiles"
        logdir = os.path.join(out, _label_dir(label))
        os.makedirs(logdir, exist_ok=True)
        xml = os.path.join(logdir, "test.xml")
        tmp = tempfile.mkdtemp(prefix="rr-test-")
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": tmp,
            "TMPDIR": tmp,
            "TEST_TMPDIR": tmp,
            "TEST_SRCDIR": runfiles,
            "RUNFILES_DIR": runfiles,
            "TEST_WORKSPACE": workspace,
            "TEST_TARGET": label,
            "XML_OUTPUT_FILE": xml,
            "TEST_UNDECLARED_OUTPUTS_DIR": os.path.join(tmp, "outputs"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "LANG": "C.UTF-8",
        }
        for spec_env in envs or []:
            env_label, key, value = spec_env.split("=", 2)
            if _norm_label(env_label) == label:
                env[key] = value
        os.makedirs(env["TEST_UNDECLARED_OUTPUTS_DIR"], exist_ok=True)
        cwd = os.path.join(runfiles, workspace)
        if not os.path.isdir(cwd):
            cwd = runfiles if os.path.isdir(runfiles) else execroot
        start = time.monotonic()
        code, log = _run_one(exe, cwd, env, timeout)
        with open(os.path.join(logdir, "test.log"), "w", encoding="utf-8") as fh:
            fh.write(log)
        failures += code != 0
        reported = _report(xml) if os.path.exists(xml) and os.path.getsize(xml) > 0 else None
        if code != 0 and reported is not None and not any(c.is_failure for c in reported):
            # The binary failed (sanitizer, crash after writing its report,
            # non-zero exit from main) although every case it reported passed:
            # the whole run is suspect, so the failure carries every id the
            # report traced — those requirements must not read VERIFIED.
            ids = [i for c in reported for i in c.requirements]
            w = JUnitWriter(label, classname=label, file="")
            w._append(
                "exit-status",
                list(dict.fromkeys(ids)),
                "error",
                f"test binary exited with {code} although its report shows no failure\n{log[-4000:]}",
                properties=TARGET_SCOPE,  # about the run, not a case of it
            )
            w.write(os.path.join(logdir, "test.exit.xml"))
        if not os.path.exists(xml) or os.path.getsize(xml) == 0:
            # Like Bazel: a test that writes no JUnit gets one synthetic case.
            w = JUnitWriter(label, classname=label, file="")
            status = "passed" if code == 0 else "failed"
            w._append(
                label.rsplit(":", 1)[-1],
                (),
                status,
                "" if code == 0 else f"exit code {code}\n{log[-4000:]}",
                time.monotonic() - start,
                properties=SYNTHETIC,  # the target's [target] case, as Bazel's own would be
            )
            w.write(xml)
        shutil.rmtree(tmp, ignore_errors=True)
        print(f"rr_evidence: {label}: {'PASSED' if code == 0 else f'FAILED (exit {code})'}", file=sys.stderr)
    return 0  # failing tests are evidence, not build failures


def _report(xml: str) -> list[Any]:
    from rules_requirements.ingest.junit import JUnitIngestor

    return list(JUnitIngestor().ingest(xml))


def golden(actual: str, golden_path: str, update: bool) -> int:
    with open(actual, encoding="utf-8") as fh:
        got = fh.read()
    if update:
        ws = os.environ.get("BUILD_WORKSPACE_DIRECTORY")
        if not ws:
            print("--update must run under `bazel run`", file=sys.stderr)
            return 2
        dest = os.path.join(ws, golden_path)
        with open(dest, "w", encoding="utf-8") as fh:
            fh.write(got)
        print(f"updated {golden_path}")
        return 0
    try:
        with open(golden_path, encoding="utf-8") as fh:
            want = fh.read()
    except OSError:
        want = ""
    if got == want:
        print(f"{golden_path}: matches")
        return 0
    sys.stdout.writelines(
        difflib.unified_diff(
            want.splitlines(True), got.splitlines(True), f"golden/{golden_path}", f"actual/{golden_path}"
        )
    )
    print(f"\n{golden_path} is out of date; regenerate with `bazel run <this target>.update`", file=sys.stderr)
    return 1


ENTRY_POINTS = {
    "cli": "rules_requirements.cli:main",
    "pytest": "rules_requirements.hooks.pytest_runner:main_argv",
    "wrap": "rules_requirements.hooks.wrap:main",
    "bazel": "rules_requirements.bazel:main",
}


def run_generated_main(entry: str, baked_args: list[str]) -> int:
    """Body of the ``main`` files the Bazel macros generate.

    Arguments are baked into the generated file (instead of a test's ``args``
    attribute) because Bazel only passes ``args`` under ``bazel test`` / ``run``
    — a test executed by ``rr_evidence`` would otherwise lose them.
    Runfiles-relative paths resolve against the working directory, which is the
    workspace's runfiles directory in every context the macros run in.
    """
    import importlib

    module, _, func = ENTRY_POINTS[entry].partition(":")
    fn = getattr(importlib.import_module(module), func)
    return int(fn(list(baked_args) + sys.argv[1:]))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="rr-bazel")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run-tests")
    r.add_argument("--out", required=True)
    r.add_argument("--test", action="append", default=[], help="LABEL=EXECUTABLE=WORKSPACE")
    r.add_argument("--timeout", type=float, default=300)
    r.add_argument("--env", action="append", default=[], help="LABEL=KEY=VALUE (the test's env attribute)")
    g = sub.add_parser("golden")
    g.add_argument("--actual", required=True)
    g.add_argument("--golden", required=True, help="workspace-relative path")
    g.add_argument("--update", action="store_true")
    args = p.parse_args(argv)
    if args.cmd == "run-tests":
        return run_tests(args.out, args.test, args.timeout, args.env)
    return golden(args.actual, args.golden, args.update)
