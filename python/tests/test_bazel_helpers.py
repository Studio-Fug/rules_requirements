# SPDX-License-Identifier: AGPL-3.0-or-later
import os
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


def _alive(pid):
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
            return fh.read().rsplit(")", 1)[1].split()[0] != "Z"  # a zombie is dead
    except OSError:
        return False


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")
def test_a_timeout_kills_everything_the_test_started(tmp_path, monkeypatch):
    # A test that forks (rr_case.h runs each case in a child) and hangs: the
    # timeout must not leave the child running in the build action.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "bin").mkdir()
    pidfile = tmp_path / "grandchild.pid"
    _exe(
        tmp_path / "bin" / "forks_and_hangs",
        "import subprocess, sys, time\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        f"open({str(pidfile)!r}, 'w').write(str(p.pid) + '\\n')\n"
        "time.sleep(120)\n",
    )
    out = tmp_path / "ev" / "testlogs"
    start = time.monotonic()
    rc = bazel.main(["run-tests", "--out", str(out), "--timeout", "2", "--test", "//pkg:h=bin/forks_and_hangs=_main"])
    assert rc == 0 and time.monotonic() - start < 60
    assert "TIMEOUT after 2.0s" in (out / "pkg" / "h" / "test.log").read_text()
    grandchild = int(pidfile.read_text())
    try:
        deadline = time.monotonic() + 10
        while _alive(grandchild) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not _alive(grandchild), "the test's child outlived the timeout"
    finally:
        if _alive(grandchild):
            os.kill(grandchild, 9)


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
    assert "exited with 23" in exit_case.message and exit_case.requirements == ("REQ-2",)
