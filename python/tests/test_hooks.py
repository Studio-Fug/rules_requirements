# SPDX-License-Identifier: AGPL-3.0-or-later
import os
import subprocess
import sys
import textwrap
import unittest

import pytest

from rules_requirements import ingest, rr
from rules_requirements.hooks import junit_writer, wrap
from rules_requirements.hooks import unittest as rr_unittest

PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _env(**extra):
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([PKG_ROOT] + [p for p in sys.path if p])
    env.pop("XML_OUTPUT_FILE", None)
    env.update(extra)
    return env


SAMPLE = textwrap.dedent(
    """
    import pytest, unittest
    from rules_requirements import rr

    pytestmark = pytest.mark.rr("REQ-1", level="sil")

    @pytest.mark.rr("REQ-2", "REQ-3, REQ-2", level="hil", artifact={"fw": "7"})
    def test_marked():
        pass

    @pytest.mark.requirements(["REQ-4"])
    def test_legacy_alias():
        assert False

    def test_module_default():
        pytest.skip("later")

    class Cases(unittest.TestCase):
        @rr.verifies("REQ-5", level="inspection")
        def test_decorated(self):
            pass
    """
)


def _check_sample(xml):
    cases = {c.name: c for c in ingest.collect([str(xml)]).cases}
    assert sorted(cases["test_marked"].requirements) == ["REQ-1", "REQ-2", "REQ-3"]
    assert cases["test_marked"].level == "hil"
    assert cases["test_marked"].artifact == {"fw": "7"}
    assert cases["test_legacy_alias"].status == "failed"
    assert sorted(cases["test_legacy_alias"].requirements) == ["REQ-1", "REQ-4"]
    assert cases["test_module_default"].status == "skipped"
    assert cases["test_module_default"].level == "sil"
    assert sorted(cases["test_decorated"].requirements) == ["REQ-1", "REQ-5"]
    assert cases["test_decorated"].level == "sil"  # the nearer pytest marker level wins


def test_pytest_runner_in_process(tmp_path, monkeypatch):
    from rules_requirements.hooks import pytest_runner

    (tmp_path / "test_sample.py").write_text(SAMPLE)
    xml = tmp_path / "out.xml"
    monkeypatch.setenv("XML_OUTPUT_FILE", str(xml))
    monkeypatch.setattr(sys, "argv", ["main", "-q", "-p", "no:randomly"])
    monkeypatch.syspath_prepend(str(tmp_path))
    rc = pytest_runner.main(str(tmp_path / "main.py"))
    assert rc == 1  # one failing test
    _check_sample(xml)


def test_pytest_runner_subprocess(tmp_path):
    (tmp_path / "test_sample.py").write_text(SAMPLE)
    (tmp_path / "main.py").write_text(
        "from rules_requirements.hooks.pytest_runner import main\nraise SystemExit(main(__file__))\n"
    )
    xml = tmp_path / "out.xml"
    proc = subprocess.run(
        [sys.executable, str(tmp_path / "main.py"), "-q"],
        env=_env(XML_OUTPUT_FILE=str(xml)),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    _check_sample(xml)


def _sample_cases():
    class _Sample(unittest.TestCase):
        @rr.verifies("REQ-1", level="sil")
        @rr.verifies("REQ-2", artifact={"k": "v"})
        def test_pass(self):
            pass

        @rr.verifies("REQ-3")
        def test_fail(self):
            self.fail("nope")

        def test_error(self):
            raise RuntimeError("kaboom")

        @unittest.skip("not today")
        def test_skip(self):
            pass

        @unittest.expectedFailure
        def test_xfail(self):
            self.fail()

        @unittest.expectedFailure
        def test_xpass(self):
            pass

    @rr.verifies("REQ-9", level="hil")
    class _ClassLevel(unittest.TestCase):
        def test_inherits(self):
            pass

    return _Sample, _ClassLevel


def test_unittest_runner(tmp_path):
    suite = unittest.TestSuite([unittest.defaultTestLoader.loadTestsFromTestCase(c) for c in _sample_cases()])
    xml = tmp_path / "u.xml"
    ok = rr_unittest.run(suite, str(xml), "sample", verbosity=0)
    assert not ok
    cases = {c.name: c for c in ingest.collect([str(xml)]).cases}
    assert cases["test_pass"].requirements == ("REQ-2", "REQ-1") and cases["test_pass"].level == "sil"
    assert cases["test_pass"].artifact == {"k": "v"}
    assert cases["test_fail"].status == "failed" and "nope" in cases["test_fail"].message
    assert cases["test_error"].status == "error"
    assert cases["test_skip"].status == "skipped"
    assert cases["test_xfail"].status == "passed"
    assert cases["test_xpass"].status == "failed"
    assert cases["test_inherits"].requirements == ("REQ-9",) and cases["test_inherits"].level == "hil"
    assert cases["test_pass"].classname.endswith("_Sample")


def test_unittest_main_discover_and_module(tmp_path, monkeypatch):
    pkg = tmp_path / "suite"
    pkg.mkdir()
    (pkg / "test_mod.py").write_text(
        "import unittest\nfrom rules_requirements import rr\n"
        "class T(unittest.TestCase):\n    @rr.verifies('REQ-1')\n    def test_a(self):\n        pass\n"
        "if __name__ == '__main__':\n    rr.unittest_main()\n"
    )
    xml = tmp_path / "d.xml"
    assert rr_unittest.main(argv=["--discover", str(pkg), "--junit-xml", str(xml), "-q"]) == 0
    assert ingest.collect([str(xml)]).for_id("REQ-1")[0].name == "test_a"
    xml2 = tmp_path / "m.xml"
    proc = subprocess.run(
        [sys.executable, str(pkg / "test_mod.py")], env=_env(XML_OUTPUT_FILE=str(xml2)), capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr
    assert ingest.collect([str(xml2)]).for_id("REQ-1")


def test_implements_decorator():
    @rr.implements("REQ-1, REQ-2", ["MIT-1"])
    def f():
        return 1

    assert f() == 1 and f.__rr_implements__ == ["REQ-1", "REQ-2", "MIT-1"]


def test_junit_writer(tmp_path):
    w = junit_writer.JUnitWriter("bench", default_level="hitl", artifact={"sha": "abc"})
    with w.case("ok_phase", ["REQ-1"]):
        pass
    with pytest.raises(ValueError), w.case("bad_phase", ["REQ-2"], level="hil", artifact={"board": "C"}):
        raise ValueError("no boot")
    w.add("skipped_phase", status="skipped", message="no DUT")
    w.add("err", status="error", message="")
    with pytest.raises(ValueError):
        w.add("x", status="bogus")
    path = tmp_path / "w.xml"
    w.write(str(path))
    assert "<testsuites>" in w.to_string()
    cases = {c.name: c for c in ingest.collect([str(path)]).cases}
    assert cases["ok_phase"].level == "hitl" and cases["ok_phase"].artifact == {"sha": "abc"}
    bad = cases["bad_phase"]
    assert bad.status == "failed" and bad.level == "hil" and bad.artifact == {"sha": "abc", "board": "C"}
    assert bad.message == "ValueError: no boot"
    assert cases["skipped_phase"].status == "skipped"
    assert cases["err"].status == "error"


FAKE_LIBTEST = r"""
import json, os, sys
trace = os.environ.get("RR_TRACE_FILE")
if trace:
    with open(trace, "a") as fh:
        fh.write(json.dumps({"test": "tests::ok_one", "requirements": ["REQ-1"], "level": "sil"}) + "\n")
print("running 2 tests")
print("test tests::ok_one ... ok")
print("test tests::bad ... FAILED")
print()
print("failures:")
print()
print("---- tests::bad stdout ----")
print("assertion failed: 1 == 2")
print()
print("test result: FAILED. 1 passed; 1 failed")
assert "XML_OUTPUT_FILE" not in os.environ
sys.exit(int(sys.argv[1]) if len(sys.argv) > 1 else 101)
"""


def test_wrap_libtest(tmp_path, monkeypatch):
    fake = tmp_path / "fake_test"
    fake.write_text("#!" + sys.executable + "\n" + FAKE_LIBTEST)
    fake.chmod(0o755)
    xml = tmp_path / "wrap.xml"
    monkeypatch.setenv("TEST_TMPDIR", str(tmp_path))
    rc = wrap.main(["--target", "//r:t", "--level", "simulation", "--junit-xml", str(xml), "--", str(fake)])
    assert rc == 101
    cases = {c.name: c for c in ingest.collect([str(xml)]).cases}
    assert cases["ok_one"].requirements == ("REQ-1",) and cases["ok_one"].level == "sil"
    assert cases["bad"].status == "failed" and "assertion failed" in cases["bad"].message
    assert cases["bad"].level == "simulation"


def test_wrap_crash_records_synthetic_case(tmp_path, monkeypatch):
    crash = tmp_path / "crash"
    crash.write_text("#!/bin/sh\necho segfault\nexit 139\n")
    crash.chmod(0o755)
    monkeypatch.setenv("XML_OUTPUT_FILE", str(tmp_path / "c.xml"))
    monkeypatch.delenv("TEST_TMPDIR", raising=False)
    assert wrap.main(["--", str(crash)]) == 139
    (case,) = ingest.collect([str(tmp_path / "c.xml")]).cases
    assert case.status == "error" and "exit code 139" in case.message


def test_wrap_resolves_runfiles_paths(tmp_path, monkeypatch):
    (tmp_path / "_main" / "pkg").mkdir(parents=True)
    target = tmp_path / "_main" / "pkg" / "bin"
    target.write_text("")
    monkeypatch.setenv("RUNFILES_DIR", str(tmp_path))
    monkeypatch.chdir(tmp_path / "_main" / "pkg")
    assert wrap._resolve("pkg/bin") == str(target)
    assert wrap._resolve("nope/bin") == "nope/bin"
    with pytest.raises(SystemExit):
        wrap.main(["--junit-xml", "x"])
