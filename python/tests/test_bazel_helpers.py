# SPDX-License-Identifier: AGPL-3.0-or-later
import sys

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
