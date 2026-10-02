# SPDX-License-Identifier: AGPL-3.0-or-later
import os
import subprocess
import sys
import textwrap
import unittest
import warnings
from xml.etree import ElementTree as ET

import pytest

from rules_requirements import ingest, rr
from rules_requirements.hooks import junit_writer, wrap
from rules_requirements.hooks import unittest as rr_unittest
from rules_requirements.hooks.ids import MultipleRequirementsWarning

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

    @pytest.mark.skip(reason="not yet")
    @pytest.mark.rr("REQ-6")
    def test_skip_marked():
        pass

    @pytest.mark.requirements("REQ-7", level="simulation")
    def test_near_level():
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
    assert cases["test_decorated"].level == "inspection"  # the method's own decorator is nearest
    # a skip marker must not lose the traces (pytest skips before setup hooks)
    assert cases["test_skip_marked"].status == "skipped" and cases["test_skip_marked"].requirements == (
        "REQ-6",
        "REQ-1",
    )
    # nearest level wins across both marker names
    assert cases["test_near_level"].level == "simulation"


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


def test_markers_added_by_conftest_are_recorded(tmp_path, monkeypatch):
    from rules_requirements.hooks import pytest_runner

    (tmp_path / "conftest.py").write_text(
        "import pytest\n"
        "def pytest_collection_modifyitems(items):\n"
        "    for item in items:\n"
        "        item.add_marker(pytest.mark.rr('REQ-1', level='hil'))\n"
    )
    (tmp_path / "test_bench.py").write_text("def test_cutoff_bench():\n    assert False\n")
    xml = tmp_path / "out.xml"
    monkeypatch.setenv("XML_OUTPUT_FILE", str(xml))
    monkeypatch.setattr(sys, "argv", ["main", "-q"])
    # the nested run registers its own "conftest" module; restore ours after
    monkeypatch.setitem(sys.modules, "conftest", sys.modules["conftest"])
    assert pytest_runner.main(str(tmp_path / "main.py")) == 1
    (case,) = ingest.collect([str(xml)]).cases
    assert case.status == "failed" and case.requirements == ("REQ-1",) and case.level == "hil"


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
    # multi-id markers still record every id (above), but warn once per declaration
    out = proc.stdout + proc.stderr
    assert "MultipleRequirementsWarning" in out and "RR-E101" in out
    assert "test_sample.py::test_marked: marker names REQ-2, REQ-3" in out
    assert "test_legacy_alias: marker" not in out  # one id per scope: no warning


def test_pytest_single_id_markers_do_not_warn(tmp_path):
    (tmp_path / "test_single.py").write_text(
        "import pytest\n"
        "pytestmark = pytest.mark.rr('REQ-1')\n"
        "@pytest.mark.rr('REQ-2')\n"  # accumulates across scopes, as before: not a multi-id declaration
        "def test_a():\n    pass\n"
        "@pytest.mark.parametrize('x', [pytest.param(1, marks=pytest.mark.rr('REQ-3')), 2])\n"
        "def test_b(x):\n    pass\n"
    )
    (tmp_path / "main.py").write_text(
        "from rules_requirements.hooks.pytest_runner import main\nraise SystemExit(main(__file__))\n"
    )
    xml = tmp_path / "out.xml"
    proc = subprocess.run(
        [sys.executable, str(tmp_path / "main.py"), "-q", "-W", "error::DeprecationWarning"],
        env=_env(XML_OUTPUT_FILE=str(xml)),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    cases = {c.name: c.requirements for c in ingest.collect([str(xml)]).cases}
    assert cases == {"test_a": ("REQ-2", "REQ-1"), "test_b[1]": ("REQ-3", "REQ-1"), "test_b[2]": ("REQ-1",)}


def test_pytest_multi_id_module_marker_warns_once(tmp_path):
    (tmp_path / "test_mod.py").write_text(
        "import pytest\npytestmark = pytest.mark.rr('REQ-1, REQ-2')\ndef test_a():\n    pass\ndef test_b():\n    pass\n"
    )
    (tmp_path / "main.py").write_text(
        "from rules_requirements.hooks.pytest_runner import main\nraise SystemExit(main(__file__))\n"
    )
    proc = subprocess.run(
        [sys.executable, str(tmp_path / "main.py"), "-q"],
        env=_env(XML_OUTPUT_FILE=str(tmp_path / "out.xml")),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.count("test_mod.py: marker names REQ-1, REQ-2") == 1, proc.stdout
    assert {c.requirements for c in ingest.collect([str(tmp_path / "out.xml")]).cases} == {("REQ-1", "REQ-2")}


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
    with pytest.warns(MultipleRequirementsWarning, match="stacked decorators"):  # REQ-1 + REQ-2 on test_pass
        sample = _sample_cases()
    suite = unittest.TestSuite([unittest.defaultTestLoader.loadTestsFromTestCase(c) for c in sample])
    xml = tmp_path / "u.xml"
    ok = rr_unittest.run(suite, str(xml), "sample", verbosity=0)
    assert not ok
    cases = {c.name: c for c in ingest.collect([str(xml)]).cases}
    assert cases["test_pass"].requirements == ("REQ-2", "REQ-1") and cases["test_pass"].level == "sil"
    assert cases["test_pass"].artifact == {"k": "v"}
    assert cases["test_fail"].status == "failed" and "nope" in cases["test_fail"].message
    assert cases["test_error"].status == "error"
    assert cases["test_skip"].status == "skipped"
    assert cases["test_xfail"].status == "skipped"  # a documented known defect is not evidence
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


def test_unittest_subtests_and_fixture_errors(tmp_path):
    @rr.verifies("REQ-8")
    class Broken(unittest.TestCase):
        @classmethod
        def setUpClass(cls):
            raise RuntimeError("no bench")

        def test_never_runs(self):
            pass

    class Sub(unittest.TestCase):
        @rr.verifies("REQ-9")
        def test_many(self):
            for i in range(3):
                with self.subTest(i=i):
                    self.assertLess(i, 2)

    Broken.__module__, Broken.__qualname__ = __name__, "Broken"
    globals()["Broken"] = Broken
    try:
        suite = unittest.TestSuite([unittest.defaultTestLoader.loadTestsFromTestCase(c) for c in (Broken, Sub)])
        xml = tmp_path / "s.xml"
        assert not rr_unittest.run(suite, str(xml), "s", verbosity=0)
        cases = ingest.collect([str(xml)]).cases
        fixture = next(c for c in cases if c.name == "setUpClass")
        assert fixture.status == "error" and fixture.requirements == ("REQ-8",) and fixture.duration < 60
        sub = [c for c in cases if c.name.startswith("test_many")]
        assert [c.status for c in sub] == ["failed"] and sub[0].requirements == ("REQ-9",) and "(i=2)" in sub[0].name
    finally:
        globals().pop("Broken", None)


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
    assert case.status == "error" and "exited with 139" in case.message


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


CRASHING_LIBTEST = r"""
import json, os, sys
with open(os.environ["RR_TRACE_FILE"], "a") as fh:
    fh.write(json.dumps({"test": "tests::passes", "requirements": ["REQ-1"]}) + "\n")
    fh.write(json.dumps({"test": "tests::crashes", "requirements": ["REQ-2"]}) + "\n")
print("running 2 tests")
print("test tests::passes ... ok")
sys.stdout.flush()
os._exit(134)  # abort while tests::crashes runs
"""


def test_wrap_attributes_a_crash_to_the_running_test(tmp_path, monkeypatch):
    fake = tmp_path / "crashy"
    fake.write_text("#!" + sys.executable + "\n" + CRASHING_LIBTEST)
    fake.chmod(0o755)
    monkeypatch.setenv("TEST_TMPDIR", str(tmp_path))
    xml = tmp_path / "c.xml"
    assert wrap.main(["--junit-xml", str(xml), "--", str(fake)]) == 134
    cases = {c.name: c for c in ingest.collect([str(xml)]).cases}
    assert cases["passes"].status == "passed"
    assert cases["crashes"].status == "error" and cases["crashes"].requirements == ("REQ-2",)


def test_wrap_nonzero_exit_after_all_passed(tmp_path, monkeypatch):
    fake = tmp_path / "leaky"
    fake.write_text("#!/bin/sh\necho 'running 1 test'\necho 'test t ... ok'\nexit 23\n")
    fake.chmod(0o755)
    monkeypatch.setenv("TEST_TMPDIR", str(tmp_path))
    xml = tmp_path / "l.xml"
    assert wrap.main(["--junit-xml", str(xml), "--", str(fake)]) == 23
    statuses = {c.name: c.status for c in ingest.collect([str(xml)]).cases}
    assert statuses == {"t": "passed", "exit-status": "error"}


NOCAPTURE_SPAWNED = r"""
import json, os, sys
with open(os.environ["RR_TRACE_FILE"], "a") as fh:
    fh.write(json.dumps({"test": "hw::flash_and_boot", "requirements": ["REQ-1"]}) + "\n")
    fh.write(json.dumps({"test": "tokio-runtime-worker", "requirements": ["REQ-2"]}) + "\n")
print("running 1 test")
print("test hw::flash_and_boot ... ")
print("[boot] app started")
print("ok")
print("test result: ok. 1 passed")
sys.exit(int(sys.argv[1]) if len(sys.argv) > 1 else 0)
"""


def test_wrap_nocapture_and_spawned_threads(tmp_path, monkeypatch, capsys):
    fake = tmp_path / "nocap"
    fake.write_text("#!" + sys.executable + "\n" + NOCAPTURE_SPAWNED)
    fake.chmod(0o755)
    monkeypatch.setenv("TEST_TMPDIR", str(tmp_path))
    xml = tmp_path / "n.xml"
    assert wrap.main(["--junit-xml", str(xml), "--", str(fake)]) == 0
    cases = {c.name: c for c in ingest.collect([str(xml)]).cases}
    assert set(cases) == {"flash_and_boot"} and cases["flash_and_boot"].status == "passed"
    assert cases["flash_and_boot"].requirements == ("REQ-1",)
    assert "tokio-runtime-worker" in capsys.readouterr().err
    # the same run exiting non-zero: the unattributable ids ride on the exit-status error
    assert wrap.main(["--junit-xml", str(xml), "--", str(fake), "3"]) == 3
    (exit_case,) = [c for c in ingest.collect([str(xml)]).cases if c.name == "exit-status"]
    assert set(exit_case.requirements) == {"REQ-1", "REQ-2"}


def test_control_characters_do_not_hide_failures(tmp_path):
    w = junit_writer.JUnitWriter("bench")
    w.add("flash", ["REQ-1"], status="failed", message="\x1b[31mE (123) boot: bad image\x1b[0m\x00")
    path = tmp_path / "bench.xml"
    w.write(str(path))
    (case,) = ingest.collect([str(path)]).cases
    assert case.status == "failed" and "#x1B[31mE (123) boot" in case.message


def _recorded(fn):
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        fn()
    return [w for w in caught if issubclass(w.category, MultipleRequirementsWarning)]


def test_rr_verifies_multi_id_is_deprecated_but_recorded():
    def single():
        @rr.verifies("REQ-1")
        @rr.verifies("REQ-1", level="sil")  # the same id twice is still one requirement
        def test_x():
            pass

        assert test_x.__rr__["ids"] == ["REQ-1", "REQ-1"]

    assert _recorded(single) == []

    def several():
        @rr.verifies("REQ-1", "REQ-2")
        def test_y():
            pass

        assert test_y.__rr__["ids"] == ["REQ-1", "REQ-2"]  # union semantics unchanged

    (w,) = _recorded(several)
    assert "test_y" in str(w.message) and "REQ-1, REQ-2" in str(w.message) and "RR-E101" in str(w.message)
    assert w.filename == __file__  # points at the decorated test

    def comma():
        rr.verifies("REQ-1, REQ-2")(lambda: None)

    assert len(_recorded(comma)) == 1

    def stacked():
        @rr.verifies("REQ-2")
        @rr.verifies("REQ-1")
        def test_z():
            pass

        assert test_z.__rr__["ids"] == ["REQ-1", "REQ-2"]

    (w,) = _recorded(stacked)
    assert "stacked decorators" in str(w.message)
    assert issubclass(MultipleRequirementsWarning, DeprecationWarning)


def test_junit_writer_single_requirement(tmp_path):
    w = junit_writer.JUnitWriter("bench", file="")
    w.add("a", "REQ-1")
    w.add("b", requirement="REQ-2", status="failed", message="x")
    with w.case("c", "REQ-3"):
        pass
    with w.case("d", requirement="REQ-4", classname="bench.step"):
        pass
    w.add("e")
    assert [c.requirement for c in w.cases] == ["REQ-1", "REQ-2", "REQ-3", "REQ-4", None]
    assert w.cases[3].classname == "bench.step"
    for bad in ("REQ-1, REQ-2", "REQ-1 REQ-2", "", " "):
        with pytest.raises(ValueError, match="RR-E104"):
            w.add("bad", bad)
        with pytest.raises(ValueError, match="RR-E104"), w.case("bad", bad):
            pass
    with pytest.raises(TypeError):
        w.add("bad", 7)
    with pytest.raises(TypeError, match="not both"):
        w.add("bad", "REQ-1", requirements=["REQ-2"])
    assert len(w.cases) == 5
    path = tmp_path / "w.xml"
    w.write(str(path))
    got = {c.name: c.requirements for c in ingest.collect([str(path)]).cases}
    assert got == {"a": ("REQ-1",), "b": ("REQ-2",), "c": ("REQ-3",), "d": ("REQ-4",), "e": ()}


def test_junit_writer_legacy_lists_warn_only_with_several_ids(tmp_path):
    w = junit_writer.JUnitWriter("bench", file="")
    assert _recorded(lambda: w.add("zero", [])) == []
    assert _recorded(lambda: w.add("one", ["REQ-1"])) == []
    assert _recorded(lambda: w.add("kw", requirements=("REQ-1",))) == []
    (warned,) = _recorded(lambda: w.add("two", ["REQ-1", "REQ-2"]))
    assert warned.filename == __file__ and "'two' names REQ-1, REQ-2" in str(warned.message)
    assert len(_recorded(lambda: w.add("kw2", requirements=["REQ-1", "REQ-2"]))) == 1
    assert len(_recorded(lambda: w.add("comma", ["REQ-1, REQ-2"]))) == 1

    def in_case():
        with w.case("ctx", ["REQ-3", "REQ-4"]):
            pass

    (warned,) = _recorded(in_case)
    assert warned.filename == __file__
    path = tmp_path / "legacy.xml"
    w.write(str(path))
    got = {c.name: c.requirements for c in ingest.collect([str(path)]).cases}
    # every id is still written, exactly as before
    assert got["two"] == ("REQ-1", "REQ-2") and got["ctx"] == ("REQ-3", "REQ-4") and got["comma"] == ("REQ-1", "REQ-2")
    assert '<property name="requirement" value="REQ-1, REQ-2" />' in path.read_text()
    assert w.cases[3].requirement == "REQ-1"  # the first id of a deprecated multi-id case


def test_junit_writer_records_the_source_file(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["/x/bazel-out/k8-fastbuild/bin/pi/hitl/e2e.runfiles/_main/pi/hitl/e2e.py"])
    w = junit_writer.JUnitWriter("bench")
    assert w.file == "pi/hitl/e2e.py"
    w.add("a", "REQ-1")
    w.add("b", file="tools/other.py")
    w.add("c", file="")
    path = tmp_path / "f.xml"
    w.write(str(path))
    files = {c.name: c.properties.get("rr.file") for c in ingest.collect([str(path)]).cases}
    assert files == {"a": "pi/hitl/e2e.py", "b": "tools/other.py", "c": None}
    assert junit_writer.JUnitWriter("x", file="").file == ""


def test_source_file(tmp_path, monkeypatch):
    sf = junit_writer.source_file
    monkeypatch.delenv("TEST_WORKSPACE", raising=False)
    monkeypatch.delenv("BUILD_WORKSPACE_DIRECTORY", raising=False)
    assert sf("") == ""
    assert sf("/c/execroot/_main/bazel-out/k8/bin/pkg/t.runfiles/_main/pkg/t.py") == "pkg/t.py"
    assert sf("/c/bazel-out/k8/bin/pkg/t.runfiles/rr~/hooks/x.py") == "external/rr~/hooks/x.py"
    monkeypatch.setenv("TEST_WORKSPACE", "splanc")
    assert sf("/r/pkg/t.runfiles/splanc/pi/t.py") == "pi/t.py"
    assert sf("/c/execroot/_main/bazel-out/k8-opt/bin/tools/bench") == "tools/bench"
    monkeypatch.setenv("BUILD_WORKSPACE_DIRECTORY", str(tmp_path))
    assert sf(str(tmp_path / "scripts" / "hitl.py")) == "scripts/hitl.py"
    monkeypatch.chdir(tmp_path)
    assert sf("scripts/hitl.py") == "scripts/hitl.py"
    assert sf("/elsewhere/hitl.py") == "/elsewhere/hitl.py"


def test_junit_writer_not_reached(tmp_path):
    w = junit_writer.JUnitWriter("hitl_e2e", default_level="hitl", file="")
    w.not_reached(
        ["rename", "cert_page"],
        "websocket_checks failed: OSError: reset",
        "hitl_e2e.websocket_checks",
        tags={"rename": "REQ-13"},
    )
    rename, cert = w.cases
    assert (rename.status, rename.requirements, rename.classname) == ("failed", ["REQ-13"], "hitl_e2e.websocket_checks")
    assert rename.message == "not reached: websocket_checks failed: OSError: reset"
    assert cert.requirements == [] and cert.level == "hitl"
    with pytest.raises(ValueError, match="RR-E101"):
        w.not_reached(["x"], "r", tags={"x": ["REQ-1", "REQ-2"]})
    with pytest.raises(ValueError, match="RR-E104"):
        w.not_reached(["x"], "r", tags={"x": "REQ-1,REQ-2"})


def test_junit_writer_append(tmp_path):
    path = tmp_path / "a.xml"
    first = junit_writer.JUnitWriter("shell", file="")
    first.add("flash", "REQ-1", duration=1.5)
    first.write(str(path), append=True)  # nothing there yet: written afresh
    second = junit_writer.JUnitWriter("shell", file="")
    second.add("boot", "REQ-2", status="failed", message="no banner", duration=0.5)
    second.add("ota", status="skipped", message="no server")
    second.write(str(path), append=True)
    other = junit_writer.JUnitWriter("other", file="")
    other.add("x", status="error")
    other.write(str(path), append=True)
    root = ET.parse(path).getroot()
    shell, oth = root.findall("testsuite")
    assert (shell.get("tests"), shell.get("failures"), shell.get("skipped"), shell.get("errors")) == (
        "3",
        "1",
        "1",
        "0",
    )
    assert shell.get("time") == "2.000" and oth.get("errors") == "1"
    assert [c.name for c in ingest.collect([str(path)]).cases] == ["flash", "boot", "ota", "x"]
    assert not (tmp_path / "a.xml.tmp").exists()

    bare = tmp_path / "bare.xml"
    bare.write_text('<testsuite name="shell" tests="1"><testcase classname="c" name="old"/></testsuite>')
    first.write(str(bare), append=True)
    assert [c.name for c in ingest.collect([str(bare)]).cases] == ["old", "flash"]
    (tmp_path / "empty.xml").write_text("")
    first.write(str(tmp_path / "empty.xml"), append=True)
    assert len(ingest.collect([str(tmp_path / "empty.xml")]).cases) == 1
    (tmp_path / "html.xml").write_text("<html/>")
    with pytest.raises(ValueError, match="not JUnit"):
        first.write(str(tmp_path / "html.xml"), append=True)


JUNIT_RUNNER = r"""
import os, sys
out = os.environ["RUNNER_JUNIT"]
os.makedirs(os.path.dirname(out), exist_ok=True)
mode = sys.argv[1]
if mode != "none":
    status = '<failure message="bad">bad</failure>' if mode == "fail" else ""
    with open(out, "w") as fh:
        fh.write(
            '<?xml version="1.0"?><testsuites><testsuite name="go">'
            '<testcase classname="pkg" name="TestA"><properties><property name="requirement" value="REQ-1"/>'
            '</properties></testcase>'
            '<testcase classname="pkg" name="TestB"><properties><property name="level" value="hil"/>'
            '<property name="requirement" value="REQ-2"/></properties>' + status + '</testcase>'
            '</testsuite></testsuites>'
        )
print("ran", mode)
sys.exit(int(sys.argv[2]))
"""


def test_wrap_junit_format(tmp_path, monkeypatch):
    runner = tmp_path / "runner"
    runner.write_text("#!" + sys.executable + "\n" + JUNIT_RUNNER)
    runner.chmod(0o755)
    monkeypatch.setenv("TEST_TMPDIR", str(tmp_path))
    monkeypatch.setenv("RUNNER_JUNIT", str(tmp_path / "results" / "junit.xml"))
    xml = tmp_path / "out.xml"

    def run(mode, code, *extra):
        argv = ["--format", "junit", "--junit-in", "${TEST_TMPDIR}/results/junit.xml", "--junit-xml", str(xml)]
        rc = wrap.main([*argv, *extra, "--target", "//go:t", "--", str(runner), mode, str(code)])
        return rc, {c.name: c for c in ingest.collect([str(xml)]).cases}

    rc, cases = run("pass", 0)
    assert rc == 0 and set(cases) == {"TestA", "TestB"}
    assert cases["TestA"].requirements == ("REQ-1",) and cases["TestA"].level == ""
    assert xml.read_bytes() == (tmp_path / "results" / "junit.xml").read_bytes()  # copied verbatim

    rc, cases = run("pass", 3)  # every case passed, yet the runner failed: the exit taint
    assert rc == 3 and cases["exit-status"].status == "error"
    assert cases["exit-status"].requirements == ("REQ-1", "REQ-2")  # as the libtest path does in 0.2
    assert "exited with 3" in cases["exit-status"].message

    rc, cases = run("fail", 1)  # a reported failure explains the exit code
    assert rc == 1 and "exit-status" not in cases and cases["TestB"].status == "failed"

    rc, cases = run("pass", 0, "--level", "sil")  # the default level, under each case's own
    assert cases["TestA"].level == "sil" and cases["TestB"].level == "hil"

    (tmp_path / "results" / "junit.xml").write_text("<testsuite name='stale'><testcase name='old'/></testsuite>")
    rc, cases = run("none", 0)  # no report this run; the stale one was removed first
    assert rc == 0 and list(cases) == ["t"] and cases["t"].status == "error"
    assert "no JUnit report at" in cases["t"].message

    with pytest.raises(SystemExit):
        wrap.main(["--format", "junit", "--", str(runner), "pass", "0"])


def test_id_helpers():
    from rules_requirements.hooks.ids import check_id, split_ids

    assert split_ids("REQ-1, REQ-2 REQ-1") == ["REQ-1", "REQ-2"]
    assert split_ids(["REQ-1", ("REQ-2,", "")]) == ["REQ-1", "REQ-2"]
    assert check_id("PR-13") == "PR-13"
    with pytest.raises(TypeError):
        check_id(13)
    with pytest.raises(ValueError, match="RR-E101"):
        check_id(("PR-13",))
