# SPDX-License-Identifier: AGPL-3.0-or-later
import os
import shutil
import signal
import sys
import time

import pytest

from rules_requirements import bazel, ingest


def _exe(path, body):
    path.write_text("#!" + sys.executable + "\n" + body)
    path.chmod(0o755)
    return path


def test_label_helpers():
    assert bazel._label_dir("//pkg/sub:name") == "pkg/sub/name"
    assert bazel._label_dir("@@repo+//pkg:name") == "external/repo+/pkg/name"
    assert bazel._label_dir("//:top") == "top"
    assert bazel._norm_label("@@//pkg:t") == "//pkg:t"
    assert bazel._norm_label("@//pkg:t") == "//pkg:t"
    assert bazel._norm_label("@@r+//pkg:t") == "@@r+//pkg:t"


def test_run_tests(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "writes.runfiles" / "_main").mkdir(parents=True)
    _exe(
        tmp_path / "bin" / "writes",
        "import os\nassert os.getcwd().endswith('writes.runfiles/_main')\n"
        "open(os.environ['XML_OUTPUT_FILE'],'w').write('<testsuite><testcase name=\"w\"><properties>"
        '<property name="requirement" value="REQ-1"/></properties></testcase></testsuite>\')\n',
    )
    _exe(tmp_path / "bin" / "silent_fail", "import sys\nprint('bad')\nsys.exit(3)\n")
    _exe(tmp_path / "bin" / "silent_pass", "pass\n")
    _exe(tmp_path / "bin" / "hang", "import time\ntime.sleep(30)\n")
    out = tmp_path / "ev" / "testlogs"
    rc = bazel.main(
        [
            "run-tests", "--out", str(out), "--timeout", "2",
            "--test", "@@//pkg:writes=bin/writes=_main",
            "--test", "//pkg:silent_fail=bin/silent_fail=_main",
            "--test", "@@ext+//x:silent_pass=bin/silent_pass=ext+",
            "--test", "//pkg:hang=bin/hang=_main",
        ]
    )  # fmt: skip
    assert rc == 0
    ev = ingest.collect([str(out)])
    assert ev.target_status == {
        "//pkg:writes": "passed",
        "//pkg:silent_fail": "failed",
        "@ext+//x:silent_pass": "passed",
        "//pkg:hang": "failed",
    }
    assert ev.for_id("REQ-1")[0].target == "//pkg:writes"
    assert "TIMEOUT" in (out / "pkg" / "hang" / "test.log").read_text()
    assert "FAILED (exit 3)" in capsys.readouterr().err


@pytest.mark.skipif(not shutil.which("setsid"), reason="needs setsid")
def test_a_timeout_never_waits_for_what_the_test_left_holding_its_output(tmp_path):
    # A process that left the test's group but holds its stdout must not keep
    # the action waiting: the timeout returns after SIGTERM's grace, with the
    # output read so far.
    pids = tmp_path / "pids"
    script = _exe(tmp_path / "escapes", "")
    script.write_text(
        f"#!/bin/sh\necho partial output\nsetsid sleep 20 & echo $! > {pids}\nsleep 60 & echo $! >> {pids}\nwait\n"
    )
    start = time.monotonic()
    try:
        code, log = bazel._run_one(str(script), str(tmp_path), {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}, 1)
        elapsed = time.monotonic() - start
    finally:
        for pid in pids.read_text().split() if pids.exists() else []:
            try:
                os.kill(int(pid), signal.SIGKILL)
            except OSError:
                pass
    assert code == -1
    assert log.startswith("partial output\n") and log.endswith("TIMEOUT after 1s")
    assert 1 <= elapsed < 1 + bazel._KILL_GRACE + 1, elapsed


@pytest.mark.skipif(not shutil.which("setsid"), reason="needs setsid")
def test_output_of_an_escaped_process_is_not_buffered_after_the_test_ends(tmp_path):
    # A chatty process the test left behind, out of its group and holding its
    # stdout: once _run_one has returned, the pipe is closed on that
    # process's next write (it dies of SIGPIPE) instead of being read into
    # memory for the rest of the action.
    pidfile = tmp_path / "pid"
    script = _exe(tmp_path / "chatty", "")
    script.write_text(
        f"#!/bin/sh\necho hello\nsetsid sh -c 'echo $$ > {pidfile}; while :; do echo more; sleep 0.01; done' &\nsleep 0.5\n"
    )
    pid = None
    try:
        code, log = bazel._run_one(str(script), str(tmp_path), {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}, 30)
        assert code == 0 and log.startswith("hello\n")
        pid = int(pidfile.read_text())
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and _running(pid):
            time.sleep(0.05)
        assert not _running(pid), "the escaped writer's output is still being read"
    finally:
        if pid is not None and _running(pid):
            os.kill(pid, signal.SIGKILL)


def _running(pid):
    """True while ``pid`` exists and is not a zombie."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        if os.path.isdir("/proc/self"):
            return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def test_a_timeout_kills_a_test_that_ignores_sigterm(tmp_path):
    exe = _exe(
        tmp_path / "stubborn",
        "import signal, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "print('ready', flush=True)\n"
        "time.sleep(60)\n",
    )
    start = time.monotonic()
    code, log = bazel._run_one(str(exe), str(tmp_path), dict(os.environ), 1)
    assert code == -1 and log.startswith("ready\n")
    assert time.monotonic() - start < 1 + bazel._KILL_GRACE + 2


def test_golden(tmp_path, monkeypatch, capsys):
    actual = tmp_path / "a.json"
    actual.write_text("new\n")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "g.json").write_text("old\n")
    assert bazel.main(["golden", "--actual", str(actual), "--golden", "pkg/g.json"]) == 1
    assert "-old" in capsys.readouterr().out
    assert bazel.main(["golden", "--actual", str(actual), "--golden", "pkg/missing.json"]) == 1
    monkeypatch.delenv("BUILD_WORKSPACE_DIRECTORY", raising=False)
    assert bazel.main(["golden", "--update", "--actual", str(actual), "--golden", "pkg/g.json"]) == 2
    monkeypatch.setenv("BUILD_WORKSPACE_DIRECTORY", str(tmp_path))
    assert bazel.main(["golden", "--update", "--actual", str(actual), "--golden", "pkg/g.json"]) == 0
    assert bazel.main(["golden", "--actual", str(actual), "--golden", "pkg/g.json"]) == 0


def test_nonzero_exit_with_all_pass_report_is_an_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "bin").mkdir()
    _exe(
        tmp_path / "bin" / "leaky",
        "import os, sys\n"
        "open(os.environ['XML_OUTPUT_FILE'],'w').write('<testsuite><testcase name=\"ok\"><properties>"
        '<property name="requirement" value="REQ-2"/></properties></testcase></testsuite>\')\n'
        "sys.exit(23)  # e.g. LeakSanitizer after all tests passed\n",
    )
    _exe(tmp_path / "bin" / "envcheck", "import os, sys\nsys.exit(0 if os.environ.get('MODE') == 'strict' else 1)\n")
    out = tmp_path / "ev" / "testlogs"
    bazel.main(
        [
            "run-tests", "--out", str(out),
            "--test", "//pkg:leaky=bin/leaky=_main",
            "--test", "//pkg:envcheck=bin/envcheck=_main",
            "--env", "//pkg:envcheck=MODE=strict",
        ]
    )  # fmt: skip
    ev = ingest.collect([str(out)])
    assert ev.target_status == {"//pkg:leaky": "error", "//pkg:envcheck": "passed"}
    (exit_case,) = [c for c in ev.cases if c.name == "exit-status"]
    # P9: the exit taint declares no requirement (not the union of the ids the
    # report traced); it is target-scope and taints the target's cases.
    assert "exited with 23" in exit_case.message and exit_case.requirements == ()
    assert exit_case.properties.get("rr.scope") == "target"


def test_no_junit_result_is_the_targets_single_case(tmp_path, monkeypatch):
    """rr_evidence's case for a test that wrote no JUnit is the same
    [target] case as Bazel's generated test.xml for that target: one key."""
    from conftest import write

    from rules_requirements.case_keys import SYNTHETIC_PATH, CaseKey, index_cases

    monkeypatch.chdir(tmp_path)
    (tmp_path / "bin").mkdir()
    _exe(tmp_path / "bin" / "flash_test", "print('flashed')\n")
    out = tmp_path / "ev" / "testlogs"
    bazel.main(["run-tests", "--out", str(out), "--test", "//hitl:flash_test=bin/flash_test=_main"])
    write(
        tmp_path,
        "bazel-testlogs/hitl/flash_test/test.xml",
        '<?xml version="1.0" encoding="UTF-8"?>\n<testsuites>\n'
        '<testsuite name="hitl/flash_test" tests="1" failures="0" errors="0">\n'
        '<testcase name="hitl/flash_test" status="run" duration="0" time="0"></testcase>\n'
        "<system-out>\nGenerated test.log (if the file is not UTF-8, then this may be unreadable):\n"
        "<![CDATA[flashed]]>\n</system-out>\n</testsuite>\n</testsuites>\n",
    )
    rows = index_cases(ingest.collect([str(tmp_path / "bazel-testlogs"), str(out)]))
    assert list(rows) == [CaseKey("//hitl:flash_test", SYNTHETIC_PATH)]
    assert rows[CaseKey("//hitl:flash_test", SYNTHETIC_PATH)].status == "passed"


def test_exit_status_case_is_target_scope(tmp_path, monkeypatch):
    from rules_requirements.case_keys import index_cases

    monkeypatch.chdir(tmp_path)
    (tmp_path / "bin").mkdir()
    _exe(
        tmp_path / "bin" / "leaky",
        "import os, sys\n"
        "open(os.environ['XML_OUTPUT_FILE'],'w').write('<testsuite><testcase name=\"ok\"><properties>"
        '<property name="requirement" value="REQ-2"/></properties></testcase></testsuite>\')\n'
        "sys.exit(23)\n",
    )
    out = tmp_path / "ev" / "testlogs"
    bazel.main(["run-tests", "--out", str(out), "--test", "//pkg:leaky=bin/leaky=_main"])
    rows = {k.path: r for k, r in index_cases(ingest.collect([str(out)])).items()}
    assert rows["//pkg:leaky::exit-status"].target_scope
    assert rows["//pkg:leaky::exit-status"].declared == ()  # no ids: target-scope taint (P9)
    assert rows["ok"].declared == ("REQ-2",)
    assert not rows["ok"].target_scope
