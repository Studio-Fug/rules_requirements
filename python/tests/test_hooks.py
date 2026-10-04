# SPDX-License-Identifier: AGPL-3.0-or-later
import os
import stat
import subprocess
import sys
import textwrap
import time
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
    # The nearest scope naming an id wins (P2): the module's REQ-1 is replaced,
    # never added. test_marked's own marker names two ids (deprecated): both
    # are recorded, so attribution quarantines it.
    assert cases["test_marked"].requirements == ("REQ-2", "REQ-3")
    assert cases["test_marked"].level == "hil"
    assert cases["test_marked"].artifact == {"fw": "7"}
    assert cases["test_legacy_alias"].status == "failed"
    assert cases["test_legacy_alias"].requirements == ("REQ-4",)
    assert cases["test_module_default"].status == "skipped"
    assert cases["test_module_default"].requirements == ("REQ-1",)
    assert cases["test_module_default"].level == "sil"
    assert cases["test_decorated"].requirements == ("REQ-5",)
    assert cases["test_decorated"].level == "inspection"  # the method's own decorator is nearest
    # a skip marker must not lose the traces (pytest skips before setup hooks)
    assert cases["test_skip_marked"].status == "skipped" and cases["test_skip_marked"].requirements == ("REQ-6",)
    # rr.file: the test file relative to the workspace (here: the run's cwd or an absolute path)
    assert all(c.properties.get("rr.file", "").endswith("test_sample.py") for c in cases.values())
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
        "@pytest.mark.rr('REQ-2')\n"  # the nearest scope wins: not a multi-id declaration
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
    # nearest wins (P2): a param mark replaces the function's, which replaces the module's
    assert cases == {"test_a": ("REQ-2",), "test_b[1]": ("REQ-3",), "test_b[2]": ("REQ-1",)}


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


_CLASS_MARK_FORMS = {
    # The decorator stores Mark objects in the class's own pytestmark.
    "decorator": ("@pytest.mark.rr('{req}')\nclass {cls}:\n", ""),
    # The class-body forms store MarkDecorator objects (a list, or a bare one).
    "body-list": ("class {cls}:\n", "    pytestmark = [pytest.mark.rr('{req}')]\n"),
    "body-bare": ("class {cls}:\n", "    pytestmark = pytest.mark.rr('{req}')\n"),
}


def _marked_class(form, cls, req, body):
    head, mark = _CLASS_MARK_FORMS[form]
    return head.format(cls=cls, req=req) + mark.format(req=req) + body


@pytest.mark.parametrize("form", sorted(_CLASS_MARK_FORMS))
def test_marked_subclass_of_marked_base_does_not_warn(tmp_path, form):
    """pytest (>=7.2) merges a base class's pytestmark into the subclass's
    Class node. Two declaration sites (the base's marker and the subclass's)
    that the nearest-scope rule resolves must not look like one declaration
    naming several ids: no MultipleRequirementsWarning, whichever documented
    way marks the classes (decorator, or a class-body pytestmark list or bare
    marker)."""
    (tmp_path / "test_inh.py").write_text(
        "import pytest\n\n\n"
        + _marked_class(form, "TestBase", "REQ-1", "    def test_base(self):\n        pass\n\n\n")
        + _marked_class(form, "TestSub(TestBase)", "REQ-2", "    def test_sub(self):\n        pass\n")
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
    assert "MultipleRequirementsWarning" not in proc.stdout, proc.stdout
    cases = {(c.classname, c.name): set(c.requirements) for c in ingest.collect([str(xml)]).cases}
    # The subclass's own declaration replaces its base's (nearest wins), for
    # its own tests and the ones it inherits.
    sub = {name: ids for (cls, name), ids in cases.items() if cls.endswith("TestSub")}
    assert sub == {"test_sub": {"REQ-2"}, "test_base": {"REQ-2"}}
    base = {name: ids for (cls, name), ids in cases.items() if cls.endswith("TestBase")}
    assert base == {"test_base": {"REQ-1"}}


@pytest.mark.parametrize("form", sorted(_CLASS_MARK_FORMS))
def test_subclass_pytestmark_naming_several_ids_still_warns(tmp_path, form):
    """A single declaration site that names several ids still warns — even on a
    subclass whose base is also marked — and names that site's ids, not the
    base's id merged in."""
    (tmp_path / "test_inh2.py").write_text(
        "import pytest\n\n\n"
        + _marked_class(form, "TestBase", "REQ-1", "    def test_base(self):\n        pass\n\n\n")
        + _marked_class(form, "TestSub(TestBase)", "REQ-2', 'REQ-3", "    def test_sub(self):\n        pass\n")
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
    assert "marker names REQ-2, REQ-3" in proc.stdout, proc.stdout
    assert "REQ-1, REQ-2" not in proc.stdout  # not the false base-plus-subclass merge


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
        assert [c.status for c in sub] == ["failed", "failed"] and sub[0].requirements == ("REQ-9",)
        assert "(i=2)" in sub[0].name
        # unittest reports no outcome for the test itself: its own key reads failed, not missing
        assert sub[1].name == "test_many" and sub[1].requirements == ("REQ-9",)
        assert sub[1].message == "subtest(s) failed: (i=2)"
    finally:
        globals().pop("Broken", None)


def test_unittest_sibling_base_classes_naming_different_ids_record_both(tmp_path):
    """unittest: as under pytest, bases no nearer class overrides are one
    scope; two naming different ids record both (quarantined), not the first
    in the MRO. A fixture error in such a class carries the same ids."""

    @rr.verifies("REQ-1")
    class MixA:
        pass

    @rr.verifies("REQ-2", level="hil")
    class MixB:
        pass

    class Both(MixA, MixB, unittest.TestCase):
        def test_both(self):
            pass

        @rr.verifies("REQ-3")
        def test_own(self):
            pass

    class Left(MixB):
        pass

    class Shared(Left, MixB, unittest.TestCase):
        def test_shared(self):
            pass

    class BrokenBoth(MixA, MixB, unittest.TestCase):
        @classmethod
        def setUpClass(cls):
            raise RuntimeError("no bench")

        def test_never_runs(self):
            pass

    BrokenBoth.__module__, BrokenBoth.__qualname__ = __name__, "BrokenBoth"
    globals()["BrokenBoth"] = BrokenBoth
    try:
        suite = unittest.TestSuite(
            [unittest.defaultTestLoader.loadTestsFromTestCase(c) for c in (Both, Shared, BrokenBoth)]
        )
        xml = tmp_path / "b.xml"
        with pytest.warns(MultipleRequirementsWarning, match=r"Both: inherited from .*MixA, .*MixB") as caught:
            rr_unittest.run(suite, str(xml), "b", verbosity=0)
        assert ["Both" in str(w.message) for w in caught] == [True]  # once per class; Shared does not warn
        cases = {c.name: c for c in ingest.collect([str(xml)]).cases}
        assert cases["test_both"].requirements == ("REQ-1", "REQ-2")
        assert cases["test_both"].level == "hil"
        assert cases["test_own"].requirements == ("REQ-3",)
        assert cases["test_shared"].requirements == ("REQ-2",)
        assert cases["setUpClass"].requirements == ("REQ-1", "REQ-2")
    finally:
        globals().pop("BrokenBoth", None)


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


WHITESPACE_LIBTEST = r"""
import json, os, sys
with open(os.environ["RR_TRACE_FILE"], "a") as fh:
    fh.write(json.dumps({"test": "tests::spaced", "requirement": "REQ-1 REQ-2"}) + "\n")
    fh.write(json.dumps({"test": "tests::single", "requirement": "REQ-3"}) + "\n")
print("running 2 tests")
print("test tests::spaced ... ok")
print("test tests::single ... ok")
print("test result: ok. 2 passed; 0 failed")
"""


def test_wrap_warns_on_a_whitespace_separated_id(tmp_path, monkeypatch, capsys):
    """rr::verifies!("REQ-1 REQ-2") names two ids (as the Python hooks and
    0.3 ingest read it): RR-E101, like any other multi-id declaration."""
    fake = tmp_path / "fake_test"
    fake.write_text("#!" + sys.executable + "\n" + WHITESPACE_LIBTEST)
    fake.chmod(0o755)
    monkeypatch.setenv("TEST_TMPDIR", str(tmp_path))
    assert wrap.main(["--junit-xml", str(tmp_path / "w.xml"), "--", str(fake)]) == 0
    err = capsys.readouterr().err
    assert "tests::spaced names REQ-1, REQ-2; a test case verifies at most one requirement [RR-E101]" in err
    assert "tests::single" not in err
    cases = {c.name: c.requirements for c in ingest.collect([str(tmp_path / "w.xml")]).cases}
    assert cases == {"spaced": ("REQ-1", "REQ-2"), "single": ("REQ-3",)}  # both written: quarantined


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
    # the same run exiting non-zero: the exit-status error declares no id (P8),
    # neither the traced ones nor the spawned thread's, which are dropped
    assert wrap.main(["--junit-xml", str(xml), "--", str(fake), "3"]) == 3
    (exit_case,) = [c for c in ingest.collect([str(xml)]).cases if c.name == "exit-status"]
    assert exit_case.requirements == () and exit_case.properties.get("rr.scope") == "target"
    assert "dropped" in capsys.readouterr().err


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

        assert test_y.__rr__["ids"] == ["REQ-1", "REQ-2"]  # all recorded: attribution quarantines it

    (w,) = _recorded(several)
    assert "test_y" in str(w.message) and "REQ-1, REQ-2" in str(w.message) and "RR-E101" in str(w.message)
    assert w.filename == __file__  # points at the decorated test

    def comma():
        rr.verifies("REQ-1, REQ-2")(lambda: None)

    assert len(_recorded(comma)) == 1

    def spaced():
        @rr.verifies("REQ-1 REQ-2")
        def test_s():
            pass

        assert test_s.__rr__["ids"] == ["REQ-1", "REQ-2"]  # whitespace separates ids from 0.3

    (w,) = _recorded(spaced)
    assert "names REQ-1, REQ-2" in str(w.message)

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
    for bad in ("REQ-1, REQ-2", "REQ-1 REQ-2"):  # "" is no requirement, as in 0.1
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
    assert (rename.status, rename.requirements, rename.classname) == (
        "failed",
        ("REQ-13",),
        "hitl_e2e.websocket_checks",
    )
    assert rename.message == "not reached: websocket_checks failed: OSError: reset"
    assert cert.requirements == () and cert.level == "hitl"
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
    assert cases["exit-status"].requirements == ()  # no ids: target-scope taint (P8)
    assert cases["exit-status"].properties.get("rr.scope") == "target"
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

    assert split_ids("REQ-1, REQ-2,REQ-1") == ["REQ-1", "REQ-2"]
    # whitespace separates ids too (0.3), as ingest splits a declared value
    assert split_ids(" REQ-1 , REQ-2 REQ-1") == ["REQ-1", "REQ-2"]
    assert split_ids(["REQ-1", ("REQ-2,", "")]) == ["REQ-1", "REQ-2"]
    assert check_id("PR-13") == "PR-13"
    with pytest.raises(TypeError):
        check_id(13)
    with pytest.raises(ValueError, match="RR-E101"):
        check_id(("PR-13",))


def test_junit_writer_warnings_point_at_the_callers_line():
    w = junit_writer.JUnitWriter("bench", file="")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        add_line = sys._getframe().f_lineno + 1
        w.add("two", ["REQ-1", "REQ-2"])
        case_line = sys._getframe().f_lineno + 1
        with w.case("ctx", ["REQ-3", "REQ-4"]):
            pass
    added, cased = (w for w in caught if issubclass(w.category, MultipleRequirementsWarning))
    assert (added.filename, added.lineno) == (__file__, add_line)
    assert (cased.filename, cased.lineno) == (__file__, case_line)


def test_junit_writer_warning_shows_at_a_scripts_top_level(tmp_path):
    # Python shows a DeprecationWarning by default only when it is attributed
    # to __main__: a plain harness script must see it at its own line.
    script = tmp_path / "harness.py"
    script.write_text(
        "from rules_requirements.hooks.junit_writer import JUnitWriter\n"
        "w = JUnitWriter('bench', file='')\n"
        "w.add('a', ['REQ-1', 'REQ-2'])\n"
        "with w.case('b', ['REQ-3', 'REQ-4']):\n"
        "    pass\n"
    )
    env = _env()
    env.pop("PYTHONWARNINGS", None)
    proc = subprocess.run([sys.executable, str(script)], env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert "harness.py:3: MultipleRequirementsWarning" in proc.stderr, proc.stderr
    assert "harness.py:4: MultipleRequirementsWarning" in proc.stderr, proc.stderr


def test_junit_writer_legacy_form_accepts_any_iterable(tmp_path):
    w = junit_writer.JUnitWriter("bench", file="")
    # one id in a list: a plain DeprecationWarning, not a multi-id one
    for name, value in (("set", {"REQ-1"}), ("gen", (r for r in ["REQ-2"])), ("keys", {"REQ-3": 1}.keys())):
        with pytest.warns(DeprecationWarning, match="pass the ONE id as a string") as caught:
            w.add(name, value)
        assert not any(issubclass(c.category, MultipleRequirementsWarning) for c in caught)
    assert len(_recorded(lambda: w.add("several", (r for r in ["REQ-4", "REQ-5"])))) == 1
    with pytest.warns(DeprecationWarning, match="empty requirement list"):
        w.add("none", [])
    with pytest.raises(TypeError):
        w.add("bad", 7)
    assert [c.requirements for c in w.cases] == [("REQ-1",), ("REQ-2",), ("REQ-3",), ("REQ-4", "REQ-5"), ()]


_APPENDER = r"""
import errno, fcntl, os, sys, time
from rules_requirements.hooks.junit_writer import JUnitWriter
path, go, i, nfs = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4] == "nfs"
if nfs:  # flock as an NFS client does it: an exclusive lock needs a writable fd
    real = fcntl.flock
    def flock(fd, op):
        if op & fcntl.LOCK_EX and fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY:
            raise OSError(errno.EBADF, os.strerror(errno.EBADF))
        return real(fd, op)
    fcntl.flock = flock
while not os.path.exists(go):
    time.sleep(0.001)
w = JUnitWriter("shell", file="")
w.add(f"case{i}", f"REQ-{i}")
w.write(path, append=True)
"""


@pytest.mark.parametrize(
    "existing",
    ["missing", "read-only", "read-only-nfs", "live-symlink", "dangling-symlink"],
)
def test_junit_writer_concurrent_appends_keep_every_case(tmp_path, existing):
    # Every shape the append must serialise: a file it creates, a read-only
    # file (locked read-only; on NFS, where that is refused, by a sidecar), a
    # symlink to a file (the lock follows it, then the append replaces the
    # link) and a dangling symlink (nothing to lock: a sidecar).
    path, go = tmp_path / "shared.xml", tmp_path / "go"
    script = tmp_path / "append.py"
    script.write_text(_APPENDER)
    first, left = [], ["append.py", "go", "shared.xml"]
    if existing in ("read-only", "read-only-nfs", "live-symlink"):
        seeded = tmp_path / ("real.xml" if existing == "live-symlink" else "shared.xml")
        seed = junit_writer.JUnitWriter("shell", file="")
        seed.add("seed")
        seed.write(str(seeded), append=True)
        first = ["seed"]
        if existing == "live-symlink":
            path.symlink_to("real.xml")
            left.append("real.xml")
        else:  # the directory is writable, the file is not
            seeded.chmod(0o444)
    elif existing == "dangling-symlink":
        path.symlink_to("nowhere.xml")
    nfs = "nfs" if existing == "read-only-nfs" else "local"
    procs = [
        subprocess.Popen(
            [sys.executable, str(script), str(path), str(go), str(i), nfs], env=_env(), stderr=subprocess.PIPE
        )
        for i in range(16)
    ]
    time.sleep(0.5)  # let every process reach the start line
    go.write_text("")
    errors = [p.communicate(timeout=60)[1].decode() for p in procs]
    assert all(p.returncode == 0 for p in procs), errors
    assert not path.is_symlink()
    names = sorted(c.name for c in ingest.collect([str(path)]).cases)
    assert names == sorted(first + [f"case{i}" for i in range(16)])
    assert sorted(os.listdir(tmp_path)) == sorted(left)  # no temp or lock files left
    if existing.startswith("read-only"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o444
    if existing == "live-symlink":  # the link's old target is left as it was
        assert [c.name for c in ingest.collect([str(tmp_path / "real.xml")]).cases] == ["seed"]


def test_junit_writer_append_keeps_the_files_mode_and_needs_no_write_access_to_it(tmp_path):
    path = tmp_path / "a.xml"
    w = junit_writer.JUnitWriter("shell", file="")
    w.add("flash", "REQ-1")
    w.write(str(path), append=True)
    for mode in (0o604, 0o444):  # neither is mkstemp's 0600; 0444 is not writable
        path.chmod(mode)
        w.write(str(path), append=True)
        assert stat.S_IMODE(path.stat().st_mode) == mode
    assert [c.name for c in ingest.collect([str(path)]).cases] == ["flash"] * 3
    assert os.listdir(tmp_path) == ["a.xml"]


_DANGLING = r"""
import sys
from rules_requirements.hooks.junit_writer import JUnitWriter
w = JUnitWriter("shell", file="")
w.add("flash", "REQ-1")
w.write(sys.argv[1], append=True)
"""


@pytest.mark.parametrize("target", ["nowhere.xml", os.path.join("gone", "nowhere.xml")])
def test_junit_writer_appends_through_a_dangling_symlink(tmp_path, target):
    # An $XML_OUTPUT_FILE symlink whose target is gone: the append ends (it
    # once spun forever) and, as before the lock, replaces the link.
    path = tmp_path / "out.xml"
    path.symlink_to(target)
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _DANGLING, str(path)], env=_env(), capture_output=True, text=True, timeout=30
        )
    except subprocess.TimeoutExpired:
        pytest.fail("the append to a dangling symlink never ended")
    assert proc.returncode == 0, proc.stderr
    assert not path.is_symlink() and [c.name for c in ingest.collect([str(path)]).cases] == ["flash"]
    assert os.listdir(tmp_path) == ["out.xml"]  # no empty target or temp file left behind
    plain = tmp_path / "plain.xml"
    junit_writer.JUnitWriter("shell", file="").write(str(plain))
    assert stat.S_IMODE(path.stat().st_mode) == stat.S_IMODE(plain.stat().st_mode)  # the umask's, not 0600


def _nfs_flock(calls):
    """``fcntl.flock`` as an NFS client does it: an exclusive lock needs a
    file opened for writing (EBADF otherwise)."""
    import errno
    import fcntl

    real = fcntl.flock

    def flock(fd, op):
        if op & fcntl.LOCK_EX and fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY:
            raise OSError(errno.EBADF, os.strerror(errno.EBADF))
        calls.append(op)
        return real(fd, op)

    return flock


def test_junit_writer_append_locks_on_nfs(tmp_path, monkeypatch):
    import fcntl

    calls = []
    monkeypatch.setattr(fcntl, "flock", _nfs_flock(calls))
    path = tmp_path / "a.xml"
    w = junit_writer.JUnitWriter("shell", file="")
    w.add("flash", "REQ-1")
    w.write(str(path), append=True)  # missing: created, then locked
    w.write(str(path), append=True)  # writable: opened for writing, so locked
    assert calls == [fcntl.LOCK_EX, fcntl.LOCK_EX]
    path.chmod(0o444)  # read-only: no exclusive lock on the file there, so a sidecar is locked
    w.write(str(path), append=True)
    assert calls == [fcntl.LOCK_EX] * 3
    assert [c.name for c in ingest.collect([str(path)]).cases] == ["flash"] * 3
    assert stat.S_IMODE(path.stat().st_mode) == 0o444 and os.listdir(tmp_path) == ["a.xml"]


def test_wrap_junit_format_without_cases_still_leaves_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_TMPDIR", str(tmp_path))
    report = tmp_path / "report.xml"
    xml = tmp_path / "out.xml"

    def run(body, code):
        runner = tmp_path / "runner"
        runner.write_text(
            f"#!{sys.executable}\nimport sys\nopen({str(report)!r}, 'w').write({body!r})\nsys.exit({code})\n"
        )
        runner.chmod(0o755)
        rc = wrap.main(["--format", "junit", "--junit-in", str(report), "--junit-xml", str(xml), "--", str(runner)])
        return rc, ingest.collect([str(xml)]).cases

    rc, cases = run("<testsuites/>", 0)
    assert rc == 0 and [(c.name, c.status) for c in cases] == [("runner", "passed")]
    rc, cases = run("<html/>", 0)
    assert rc == 0 and [(c.name, c.status) for c in cases] == [("runner", "error")]
    assert "not JUnit XML" in cases[0].message
    rc, cases = run("<testsuites/>", 4)  # no case failed, yet the runner did
    assert rc == 4 and [(c.name, c.status) for c in cases] == [("exit-status", "error")]


def _run_pytest(tmp_path, *args):
    (tmp_path / "main.py").write_text(
        "from rules_requirements.hooks.pytest_runner import main\nraise SystemExit(main(__file__))\n"
    )
    xml = tmp_path / "out.xml"
    proc = subprocess.run(
        [sys.executable, str(tmp_path / "main.py"), "-q", "-p", "no:cacheprovider", *args],
        env=_env(XML_OUTPUT_FILE=str(xml)),
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    return proc, proc.stdout + proc.stderr, {c.name: c for c in ingest.collect([str(xml)]).cases}


def test_pytest_multi_id_marker_escalated_fails_only_its_test(tmp_path):
    (tmp_path / "test_multi.py").write_text(
        "import pytest\n@pytest.mark.rr('REQ-1', 'REQ-2')\ndef test_multi():\n    pass\n"
    )
    (tmp_path / "test_other.py").write_text("def test_other():\n    pass\n")
    proc, out, cases = _run_pytest(tmp_path, "-W", "error::DeprecationWarning")
    assert "INTERNALERROR>" not in out and proc.returncode == 1, (proc.returncode, out)
    assert cases["test_other"].status == "passed"
    assert cases["test_multi"].status == "error" and "RR-E101" in cases["test_multi"].message
    assert cases["test_multi"].requirements == ("REQ-1", "REQ-2")  # still recorded


def test_pytest_multi_id_marker_warns_once_per_declaration_at_its_line(tmp_path):
    (tmp_path / "test_param.py").write_text(
        "import pytest\n"
        "\n"
        "@pytest.mark.rr('REQ-1', 'REQ-2')\n"
        "@pytest.mark.parametrize('x', [1, 2, 3])\n"
        "def test_c(x):\n"
        "    pass\n"
    )
    proc, out, cases = _run_pytest(tmp_path, "-rN")
    assert proc.returncode == 0, out
    assert out.count("test_param.py::test_c: marker names REQ-1, REQ-2") == 1, out
    assert "test_param.py:3: MultipleRequirementsWarning" in out, out  # the marker's line
    assert {c.requirements for c in cases.values()} == {("REQ-1", "REQ-2")} and len(cases) == 3


_MULTI_ID_DECLARATIONS = {
    "test_mod.py": "import pytest\npytestmark = pytest.mark.rr('REQ-1, REQ-2')\n"
    "def test_a():\n    pass\ndef test_b():\n    pass\n",
    "test_par.py": "import pytest\n@pytest.mark.rr('REQ-3', 'REQ-4')\n@pytest.mark.parametrize('x', [1, 2, 3])\n"
    "def test_c(x):\n    pass\n",
    "test_cls.py": "import pytest\n@pytest.mark.rr('REQ-5', 'REQ-6')\nclass TestK:\n"
    "    def test_d(self):\n        pass\n    def test_e(self):\n        pass\n",
}

# Counts the warning records themselves: pytest's summary groups identical
# messages, so counting the summary's text cannot tell one record from seven.
_RECORD_WARNINGS = """
def pytest_warning_recorded(warning_message, when, nodeid, location):
    if warning_message.category.__name__ == "MultipleRequirementsWarning":
        with open(__file__ + ".records", "a") as fh:
            fh.write(f"{nodeid}\\n")
"""


def test_pytest_multi_id_warning_records_once_per_declaration(tmp_path):
    for name, text in _MULTI_ID_DECLARATIONS.items():
        (tmp_path / name).write_text(text)
    (tmp_path / "conftest.py").write_text(_RECORD_WARNINGS)
    proc, out, cases = _run_pytest(tmp_path)
    assert proc.returncode == 0, out
    records = (tmp_path / "conftest.py.records").read_text().splitlines()
    assert sorted(records) == ["test_cls.py::TestK::test_d", "test_mod.py::test_a", "test_par.py::test_c[1]"], out
    assert len(cases) == 7 and all(c.status == "passed" for c in cases.values())


def test_pytest_multi_id_warning_escalated_errors_one_test_per_declaration(tmp_path):
    for name, text in _MULTI_ID_DECLARATIONS.items():
        (tmp_path / name).write_text(text)
    proc, out, cases = _run_pytest(tmp_path, "-W", "error::rules_requirements.hooks.ids.MultipleRequirementsWarning")
    assert "INTERNALERROR>" not in out and proc.returncode == 1, out
    errored = sorted(name for name, c in cases.items() if c.status == "error")
    assert errored == ["test_a", "test_c[1]", "test_d"], out  # the first test of each declaration only
    assert all(c.status == "passed" for name, c in cases.items() if name not in errored)


def test_pytest_space_separated_marker_names_two_ids(tmp_path):
    # From 0.3 whitespace separates ids, as ingest splits a declared value:
    # "REQ-1 REQ-2" is a multi-id declaration, warned about and recorded in
    # full (attribution quarantines the case).
    (tmp_path / "test_space.py").write_text("import pytest\n@pytest.mark.rr('REQ-1 REQ-2')\ndef test_s():\n    pass\n")
    proc, out, cases = _run_pytest(tmp_path)
    assert proc.returncode == 0, out
    assert cases["test_s"].requirements == ("REQ-1", "REQ-2")
    assert "marker names REQ-1, REQ-2" in out and "RR-E101" in out


def _keys(xml):
    from rules_requirements.case_keys import index_cases

    return {str(k): r for k, r in index_cases(ingest.collect([str(xml)])).items()}


def test_wrap_marks_whole_run_results(tmp_path, monkeypatch):
    """rr wrap's own whole-target results are [target] (rr.synthetic) or
    rr.scope=target, like Bazel's generated XML and rr_node_test's errors."""
    monkeypatch.setenv("TEST_TMPDIR", str(tmp_path))
    xml = tmp_path / "out.xml"
    # libtest: nothing parseable from a clean exit -> the target's [target] case
    quiet = tmp_path / "quiet"
    quiet.write_text("#!/bin/sh\nexit 0\n")
    quiet.chmod(0o755)
    assert wrap.main(["--target", "//rs:nothing_test", "--junit-xml", str(xml), "--", str(quiet)]) == 0
    (row,) = _keys(xml).values()
    assert row.synthetic and str(row.key) == "suite:nothing_test#[target]"
    # ... from a crash: the exit taint, about the whole run
    quiet.write_text("#!/bin/sh\nexit 139\n")
    assert wrap.main(["--target", "//rs:nothing_test", "--junit-xml", str(xml), "--", str(quiet)]) == 139
    (row,) = _keys(xml).values()
    assert row.target_scope and row.status == "error"
    # libtest: every test passed, the binary failed -> the exit taint is target scope
    leaky = tmp_path / "leaky"
    leaky.write_text("#!/bin/sh\necho 'running 1 test'\necho 'test parse::a ... ok'\nexit 101\n")
    leaky.chmod(0o755)
    assert wrap.main(["--suite", "parse_test", "--junit-xml", str(xml), "--", str(leaky)]) == 101
    rows = _keys(xml)
    assert rows["suite:parse_test#parse_test::exit-status"].target_scope
    assert not rows["suite:parse_test#parse::a"].target_scope

    # --format junit
    report = tmp_path / "report.xml"

    def run(body, code):
        runner = tmp_path / "runner"
        write_body = f"open({str(report)!r}, 'w').write({body!r})\n" if body is not None else ""
        runner.write_text(f"#!{sys.executable}\nimport sys\n{write_body}sys.exit({code})\n")
        runner.chmod(0o755)
        if report.exists():
            report.unlink()
        wrap.main(
            ["--format", "junit", "--junit-in", str(report), "--junit-xml", str(xml), "--suite", "t", "--", str(runner)]
        )
        return _keys(xml)

    (row,) = run("<testsuites/>", 0).values()  # no test cases
    assert row.synthetic and row.status == "passed"
    (row,) = run(None, 0).values()  # no report
    assert row.target_scope and row.status == "error" and not row.synthetic
    (row,) = run("<html/>", 0).values()  # not JUnit
    assert row.target_scope and row.status == "error"
    two = (
        "<testsuite name='bench'><testcase classname='bench' name='a'><properties>"
        "<property name='requirement' value='REQ-1'/></properties></testcase>"
        "<testcase classname='bench' name='b'><properties>"
        "<property name='requirement' value='REQ-2'/></properties></testcase></testsuite>"
    )
    rows = run(two, 3)
    exit_row = rows["suite:t#t::exit-status"]
    assert exit_row.target_scope and exit_row.declared == ()  # no ids: the target-scope taint (P8)
    assert not rows["suite:bench#bench::a"].target_scope


def test_wrap_warns_on_a_case_naming_several_ids(tmp_path, monkeypatch, capsys):
    """rr::verifies! with two ids in one test: recorded (0.2), with RR-E101 on stderr."""
    fake = tmp_path / "multi"
    fake.write_text(
        "#!" + sys.executable + "\n"
        "import json, os\n"
        "with open(os.environ['RR_TRACE_FILE'], 'a') as fh:\n"
        "    for rid in ('REQ-5', 'REQ-6'):\n"
        "        fh.write(json.dumps({'test': 'parse::a', 'requirements': [rid], 'level': 'sil'}) + '\\n')\n"
        "    fh.write(json.dumps({'test': 'parse::b', 'requirements': ['REQ-7']}) + '\\n')\n"
        "print('running 2 tests')\n"
        "print('test parse::a ... ok')\n"
        "print('test parse::b ... ok')\n"
    )
    fake.chmod(0o755)
    monkeypatch.setenv("TEST_TMPDIR", str(tmp_path))
    xml = tmp_path / "m.xml"
    assert wrap.main(["--junit-xml", str(xml), "--", str(fake)]) == 0
    err = capsys.readouterr().err
    assert "rr wrap: warning: parse::a names REQ-5, REQ-6" in err and "[RR-E101]" in err
    assert "parse::b" not in err
    cases = {c.name: c for c in ingest.collect([str(xml)]).cases}
    assert cases["a"].requirements == ("REQ-5", "REQ-6")


def _run_strict(tmp_path):
    """pytest over tmp_path with DeprecationWarnings escalated; (proc, cases by name)."""
    (tmp_path / "main.py").write_text(
        "from rules_requirements.hooks.pytest_runner import main\nraise SystemExit(main(__file__))\n"
    )
    xml = tmp_path / "out.xml"
    proc = subprocess.run(
        [sys.executable, str(tmp_path / "main.py"), "-q", "-p", "no:cacheprovider", "-W", "error::DeprecationWarning"],
        env=_env(XML_OUTPUT_FILE=str(xml)),
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    cases = {c.name: c for c in ingest.collect([str(xml)]).cases} if xml.exists() else {}
    return proc, cases


def test_pytest_marker_and_rr_verifies_on_one_function_warn(tmp_path):
    """One scope, two declarations naming different ids: a multi-id case."""
    (tmp_path / "test_same.py").write_text(
        "import pytest, unittest\n"
        "from rules_requirements import rr\n"
        "@pytest.mark.rr('REQ-A')\n"
        "@rr.verifies('REQ-B')\n"
        "def test_same():\n    pass\n"
        "@pytest.mark.rr('REQ-C')\n"
        "@rr.verifies('REQ-C')\n"  # the same id twice: one requirement
        "def test_agree():\n    pass\n"
        "@pytest.mark.rr('REQ-D')\n"
        "@rr.verifies('REQ-E')\n"
        "class TestK:\n"
        "    def test_k(self):\n        pass\n"
    )
    proc, cases = _run_strict(tmp_path)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 1, out
    assert "test_same.py::test_same: marker and rr.verifies names REQ-A, REQ-B" in out
    assert "test_same.py::TestK: marker and rr.verifies names REQ-D, REQ-E" in out
    assert cases["test_same"].status == "error" and cases["test_agree"].status == "passed"
    assert cases["test_k"].status == "error"


def test_pytest_param_mark_and_function_marker_do_not_warn(tmp_path):
    """A pytest.param mark is nearer than the function's marker (0.3 keeps
    it alone): not a multi-id declaration. Several ids in one param mark are."""
    (tmp_path / "test_p.py").write_text(
        "import pytest\n"
        "@pytest.mark.parametrize('x', [1, pytest.param(2, marks=pytest.mark.rr('REQ-3'))])\n"
        "@pytest.mark.rr('REQ-1')\n"
        "def test_params(x):\n    pass\n"
    )
    proc, cases = _run_strict(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert cases["test_params[2]"].requirements == ("REQ-3",)  # the param mark is nearest (P2)
    (tmp_path / "test_p.py").write_text(
        "import pytest\n"
        "@pytest.mark.parametrize('x', [1, pytest.param(2, marks=pytest.mark.rr('REQ-3', 'REQ-4'))])\n"
        "def test_params(x):\n    pass\n"
    )
    proc, cases = _run_strict(tmp_path)
    assert proc.returncode == 1 and "test_params[2]: pytest.param marks names REQ-3, REQ-4" in proc.stdout
    assert cases["test_params[1]"].status == "passed" and cases["test_params[2]"].status == "error"


def test_rr_verifies_on_a_subclass_of_a_decorated_class_does_not_warn(tmp_path):
    (tmp_path / "test_inh.py").write_text(
        "import unittest\n"
        "from rules_requirements import rr\n"
        "@rr.verifies('REQ-1')\n"
        "class Base(unittest.TestCase):\n"
        "    def test_base(self):\n        pass\n"
        "@rr.verifies('REQ-2')\n"
        "class Sub(Base):\n"
        "    def test_sub(self):\n        pass\n"
    )
    proc, cases = _run_strict(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert cases["test_sub"].requirements == ("REQ-2",)  # the subclass's own declaration is nearest (P4)

    def stacked_on_a_subclass():
        @rr.verifies("REQ-1")
        class Base:
            pass

        @rr.verifies("REQ-3")
        @rr.verifies("REQ-2")
        class Sub(Base):
            pass

    (w,) = _recorded(stacked_on_a_subclass)  # its own two decorators are still one scope
    assert "stacked decorators" in str(w.message) and "names REQ-2, REQ-3;" in str(w.message)


def test_pytest_runner_pins_the_rootdir_to_the_runfiles_tree(tmp_path):
    """An ini file above the runfiles tree (the execroot's pyproject.toml in a
    local run) must not put bazel-out/.../runfiles into the classnames."""
    execroot = tmp_path / "execroot" / "_main"
    runfiles = execroot / "bazel-out" / "k8-fastbuild" / "bin" / "pi" / "server" / "server_test.runfiles"
    pkg = runfiles / "_main" / "pi" / "server"
    pkg.mkdir(parents=True)
    (pkg / "test_handler.py").write_text("def test_configure():\n    pass\n")
    (execroot / "pyproject.toml").write_text("[tool.pytest.ini_options]\nminversion = '6.0'\n")
    xml = tmp_path / "out.xml"
    main = tmp_path / "main.py"
    main.write_text(
        "from rules_requirements.hooks.pytest_runner import main_argv\n"
        "raise SystemExit(main_argv(['pi/server/test_handler.py']))\n"
    )
    env = _env(XML_OUTPUT_FILE=str(xml), TEST_SRCDIR=str(runfiles), TEST_WORKSPACE="_main")
    proc = subprocess.run([sys.executable, str(main)], env=env, capture_output=True, text=True, cwd=runfiles / "_main")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    (case,) = ingest.collect([str(xml)]).cases
    assert case.classname == "pi.server.test_handler"
    # An explicit --rootdir is the caller's.
    main.write_text(
        "from rules_requirements.hooks.pytest_runner import main_argv\n"
        f"raise SystemExit(main_argv(['--rootdir={execroot}', 'pi/server/test_handler.py']))\n"
    )
    proc = subprocess.run([sys.executable, str(main)], env=env, capture_output=True, text=True, cwd=runfiles / "_main")
    (case,) = ingest.collect([str(xml)]).cases
    assert case.classname.startswith("bazel-out.k8-fastbuild.bin.pi.server")


def test_junit_writer_empty_requirement_string_is_no_requirement(tmp_path):
    """0.1.0 recorded no id for ''; a comma or inner whitespace is still RR-E104."""
    w = junit_writer.JUnitWriter("bench", file="")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        w.add("a", "")
        w.add("b", requirements="")
        w.add("c", "  ")
        with w.case("d", ""):
            pass
    assert [c.requirements for c in w.cases] == [(), (), (), ()]
    with pytest.raises(ValueError, match="RR-E104"):
        w.add("e", "REQ-1, REQ-2")
    with pytest.raises(ValueError, match="RR-E104"):
        w.add("f", "REQ 1")


def test_source_file_on_another_drive_never_raises(tmp_path, monkeypatch):
    """Windows: relpath raises for a script on another drive than the root; the
    derived rr.file default must not make JUnitWriter(...) raise."""
    import ntpath

    monkeypatch.delenv("BUILD_WORKSPACE_DIRECTORY", raising=False)

    def cross_drive(path, start=None):
        return ntpath.relpath("D:\\tools\\bench.py", "C:\\work")

    monkeypatch.setattr(junit_writer.os.path, "relpath", cross_drive)
    with pytest.raises(ValueError):
        cross_drive("x")  # what ntpath does
    script = str(tmp_path / "bench.py")
    assert junit_writer.source_file(script) == script.replace(os.sep, "/")
    monkeypatch.setattr(sys, "argv", [script])
    assert junit_writer.JUnitWriter("bench").file == script.replace(os.sep, "/")


# --------------------------------------------------------------------------- #
# 0.3: one declared id per case (P2-P5, P7-P9)                                #
# --------------------------------------------------------------------------- #


def test_pytest_raw_requirement_property_is_dropped_and_fails_the_test(tmp_path):
    """P3: record_property("requirement") bypasses the marker rules: RR-E102."""
    (tmp_path / "test_raw.py").write_text(
        "import pytest\n"
        "@pytest.mark.rr('REQ-1')\n"
        "def test_second_id(record_property):\n"
        "    record_property('requirement', 'REQ-2')\n"
        "def test_plural(record_property):\n"
        "    record_property('requirements', 'REQ-3,REQ-4')\n"
        "def test_failing_anyway(record_property):\n"
        "    record_property('requirement', 'REQ-5')\n"
        "    assert False, 'its own failure'\n"
        "@pytest.fixture\n"
        "def tagger(record_property):\n"
        "    yield\n"
        "    record_property('requirement', 'REQ-6')\n"
        "def test_in_teardown(tagger):\n"
        "    pass\n"
        "@pytest.mark.rr('REQ-7')\n"
        "def test_other_properties(record_property):\n"
        "    record_property('dut', 'c6')\n"
        # junitxml writes any two-item entry (`for name, value in ...`), not just a tuple
        "@pytest.mark.rr('REQ-10')\n"
        "def test_list_entry(request):\n"
        "    request.node.user_properties.append(['requirement', 'REQ-11'])\n"
        "def test_dict_entry(request):\n"
        "    request.node.user_properties.append({'requirement': 0, 'REQ-12': 0})\n"
        "def test_iterator_entry(request):\n"
        "    request.node.user_properties.append(iter(('requirements', 'REQ-13')))\n"
        "class Name:\n"
        "    def __str__(self):\n"
        "        return 'requirement'\n"
        "def test_str_name(record_property):\n"
        "    record_property(Name(), 'REQ-14')\n"
        "def test_iterator_other(request):\n"
        "    request.node.user_properties.append(iter(('bench', 'rig-2')))\n"
        # an xfail test's guard failure is a failure, not an expected one (<skipped>)
        "@pytest.mark.rr('REQ-15')\n"
        "@pytest.mark.xfail(reason='known defect')\n"
        "def test_xfail_raw(record_property):\n"
        "    record_property('requirement', 'REQ-16')\n"
        "    assert False\n"
        "@pytest.mark.rr('REQ-17')\n"
        "@pytest.mark.xfail(reason='known defect')\n"
        "def test_xpass_raw(record_property):\n"
        "    record_property('requirement', 'REQ-18')\n"
    )
    proc, out, cases = _run_pytest(tmp_path)
    assert proc.returncode == 1, out
    assert "[RR-E102]" in out and "@pytest.mark.rr" in out
    assert cases["test_second_id"].status == "failed" and cases["test_second_id"].requirements == ("REQ-1",)
    assert "RR-E102" in cases["test_second_id"].message
    assert cases["test_plural"].status == "failed" and cases["test_plural"].requirements == ()
    assert cases["test_failing_anyway"].status == "failed" and cases["test_failing_anyway"].requirements == ()
    assert "its own failure" in cases["test_failing_anyway"].message
    assert cases["test_in_teardown"].status == "error" and cases["test_in_teardown"].requirements == ()
    other = cases["test_other_properties"]
    assert other.status == "passed" and other.requirements == ("REQ-7",) and other.properties["dut"] == "c6"
    for name, ids in [
        ("test_list_entry", ("REQ-10",)),
        ("test_dict_entry", ()),
        ("test_iterator_entry", ()),
        ("test_str_name", ()),
        ("test_xfail_raw", ("REQ-15",)),
        ("test_xpass_raw", ("REQ-17",)),
    ]:
        assert (cases[name].status, cases[name].requirements) == ("failed", ids), name
        assert "RR-E102" in cases[name].message, name
    bench = cases["test_iterator_other"]
    assert bench.status == "passed" and bench.properties["bench"] == "rig-2"


def test_pytest_nearest_scope_wins_over_class_and_module(tmp_path):
    """P2: one property per case, from the nearest scope that names an id."""
    (tmp_path / "test_near.py").write_text(
        "import pytest\n"
        "from rules_requirements import rr\n"
        "pytestmark = pytest.mark.rr('REQ-1', level='sil')\n"
        "@pytest.mark.rr('REQ-2')\n"
        "class TestOuter:\n"
        "    def test_class(self):\n        pass\n"
        "    @pytest.mark.rr('REQ-3')\n"
        "    def test_method(self):\n        pass\n"
        "    @rr.verifies('REQ-4')\n"
        "    def test_verifies(self):\n        pass\n"
        "    @pytest.mark.rr(level='hil')\n"
        "    def test_level_only(self):\n        pass\n"
        "def test_module():\n    pass\n"
    )
    proc, out, cases = _run_pytest(tmp_path, "-W", "error::DeprecationWarning")
    assert proc.returncode == 0, out
    got = {name: (c.requirements, c.level) for name, c in cases.items()}
    assert got == {
        "test_class": (("REQ-2",), "sil"),
        "test_method": (("REQ-3",), "sil"),
        "test_verifies": (("REQ-4",), "sil"),
        "test_level_only": (("REQ-2",), "hil"),  # a level-only marker names no id: the class's id stays
        "test_module": (("REQ-1",), "sil"),
    }
    assert {c.properties.get("rr.file") for c in cases.values()} == {"test_near.py"}


def test_pytest_sibling_base_classes_naming_different_ids_are_one_scope(tmp_path):
    """Bases that no nearer class overrides are ONE scope: two of them naming
    different ids make the case multi-id (warned, every id recorded, so it is
    quarantined), never resolved silently by MRO order."""
    (tmp_path / "test_bases.py").write_text(
        "import pytest\n"
        "from rules_requirements import rr\n"
        "class MixA:\n    pytestmark = pytest.mark.rr('REQ-1')\n"
        "class MixB:\n    pytestmark = pytest.mark.rr('REQ-2')\n"
        "class TestDiamond(MixA, MixB):\n"
        "    def test_d(self):\n        pass\n"
        "class TestOwn(MixA, MixB):\n"
        "    pytestmark = pytest.mark.rr('REQ-7')\n"  # the class's own id overrides both bases
        "    def test_o(self):\n        pass\n"
        "class Base:\n    pytestmark = pytest.mark.rr('REQ-3', level='hil')\n"
        "class Left(Base):\n    pass\n"
        "class Right(Base):\n    pass\n"
        "class TestShared(Left, Right):\n"  # one declaration, reached two ways
        "    def test_s(self):\n        pass\n"
        "class Over(Base):\n    pytestmark = pytest.mark.rr('REQ-4')\n"
        "class TestRedundant(Over, Base):\n"  # Over overrides Base, listed again or not
        "    def test_r(self):\n        pass\n"
        "@rr.verifies('REQ-5')\nclass VA:\n    pass\n"
        "@rr.verifies('REQ-6')\nclass VB:\n    pass\n"
        "class TestVerified(VA, VB):\n"
        "    def test_v(self):\n        pass\n"
        "class TestMixed(MixA, VB):\n"
        "    def test_m(self):\n        pass\n"
    )
    proc, out, cases = _run_pytest(tmp_path, "-W", "error::DeprecationWarning")
    got = {name: (c.status, c.requirements) for name, c in cases.items()}
    assert got == {
        "test_d": ("error", ("REQ-1", "REQ-2")),
        "test_o": ("passed", ("REQ-7",)),
        "test_s": ("passed", ("REQ-3",)),
        "test_r": ("passed", ("REQ-4",)),
        "test_v": ("error", ("REQ-5", "REQ-6")),
        "test_m": ("error", ("REQ-1", "REQ-6")),
    }, out
    assert cases["test_s"].level == "hil"
    assert "RR-E101" in cases["test_d"].message and "MixA" in cases["test_d"].message
    assert "MixB" in cases["test_d"].message
    proc, out, cases = _run_pytest(tmp_path)  # not escalated: warned once per class, every id recorded
    assert proc.returncode == 0, out
    assert out.count("TestDiamond: inherited from") == 1 and "TestVerified: inherited from" in out
    assert "TestShared" not in out and "TestRedundant" not in out and "TestOwn" not in out
    assert cases["test_d"].requirements == ("REQ-1", "REQ-2")


def test_pytest_trace_of_gives_the_nearest_scope(tmp_path):
    (tmp_path / "conftest.py").write_text(
        "import json, os\n"
        "from rules_requirements.hooks.pytest_plugin import trace_of\n"
        "def pytest_collection_finish(session):\n"
        "    out = {i.name: trace_of(i)[0] for i in session.items}\n"
        "    open(os.environ['DUMP'], 'w').write(json.dumps(out))\n"
    )
    (tmp_path / "test_t.py").write_text(
        "import pytest\npytestmark = pytest.mark.rr('REQ-1')\n"
        "@pytest.mark.rr('REQ-2')\ndef test_a():\n    pass\n"
        "@pytest.mark.rr('REQ-3', 'REQ-4')\ndef test_multi():\n    pass\n"
        "def test_b():\n    pass\n"
    )
    dump = tmp_path / "dump.json"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--collect-only", "-p",
         "rules_requirements.hooks.pytest_plugin", str(tmp_path)],
        env=_env(DUMP=str(dump)),
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )  # fmt: skip
    assert proc.returncode == 0, proc.stdout + proc.stderr
    import json

    assert json.loads(dump.read_text()) == {"test_a": ["REQ-2"], "test_multi": ["REQ-3", "REQ-4"], "test_b": ["REQ-1"]}


def test_unittest_method_declaration_beats_the_class(tmp_path):
    """P4: nearest wins; fixture errors and failing subtests carry that one id."""

    @rr.verifies("REQ-1", level="hil")
    class Suite(unittest.TestCase):
        @rr.verifies("REQ-2")
        def test_method(self):
            pass

        def test_class(self):
            pass

        @rr.verifies("REQ-3")
        def test_subtests(self):
            for i in range(2):
                with self.subTest(i=i):
                    self.assertEqual(i, 0)

    @rr.verifies("REQ-4")
    class Sub(Suite):
        def test_sub(self):
            pass

    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)  # no multi-id declaration anywhere
        suite = unittest.TestSuite([unittest.defaultTestLoader.loadTestsFromTestCase(c) for c in (Suite, Sub)])
        xml = tmp_path / "u.xml"
        rr_unittest.run(suite, str(xml), "near", verbosity=0)
    got = {
        (c.classname.rsplit(".", 1)[-1], c.name): (c.requirements, c.level) for c in ingest.collect([str(xml)]).cases
    }
    assert got[("Suite", "test_method")] == (("REQ-2",), "hil")
    assert got[("Suite", "test_class")] == (("REQ-1",), "hil")
    assert got[("Suite", "test_subtests")] == (("REQ-3",), "hil")
    assert got[("Suite", "test_subtests (i=1)")] == (("REQ-3",), "hil")
    assert got[("Sub", "test_sub")] == (("REQ-4",), "hil")  # the subclass's own, not its base's too
    assert got[("Sub", "test_class")] == (("REQ-4",), "hil")
    assert got[("Sub", "test_method")] == (("REQ-2",), "hil")
    files = {c.properties.get("rr.file", "") for c in ingest.collect([str(xml)]).cases}
    assert len(files) == 1 and files.pop().endswith("test_hooks.py")


def test_junit_writer_cases_are_read_only():
    """P5: a recorded case cannot be re-attributed afterwards."""
    import dataclasses

    w = junit_writer.JUnitWriter("bench", file="")
    w.add("a", "REQ-1")
    (case,) = w.cases
    assert isinstance(w.cases, tuple) and case.requirement == "REQ-1" and case.requirements == ("REQ-1",)
    with pytest.raises(AttributeError):
        w.cases.append(case)  # type: ignore[attr-defined]
    with pytest.raises(AttributeError):
        w.cases = []  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        case.requirements = []  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        case.declared = ("REQ-2",)  # type: ignore[misc]
    with pytest.raises(AttributeError):
        case.requirements.clear()  # type: ignore[attr-defined]
    with pytest.raises(TypeError):
        case.properties["rr.scope"] = "target"  # type: ignore[index]
    with pytest.raises(TypeError):
        case.artifact["k"] = "v"  # type: ignore[index]
    assert "requirement" in w.to_string() and "REQ-2" not in w.to_string()


def test_rust_trace_lines_singular_and_list_forms(tmp_path, monkeypatch):
    """P7: the 0.3 singular form, the 0.2 list form, and two calls in one test."""
    fake = tmp_path / "traced"
    fake.write_text(
        "#!" + sys.executable + "\n"
        "import json, os\n"
        "with open(os.environ['RR_TRACE_FILE'], 'a') as fh:\n"
        "    for rec in ({'test': 't::one', 'requirement': 'REQ-1', 'level': 'sil'},\n"
        "                {'test': 't::old', 'requirements': ['REQ-2']},\n"
        "                {'test': 't::list', 'requirements': ['REQ-3', 'REQ-4']},\n"
        "                {'test': 't::twice', 'requirement': 'REQ-5'},\n"
        "                {'test': 't::twice', 'requirement': 'REQ-6'}):\n"
        "        fh.write(json.dumps(rec) + '\\n')\n"
        "print('running 4 tests')\n"
        "for t in ('one', 'old', 'list', 'twice'):\n"
        "    print(f'test t::{t} ... ok')\n"
    )
    fake.chmod(0o755)
    monkeypatch.setenv("TEST_TMPDIR", str(tmp_path))
    xml = tmp_path / "r.xml"
    assert wrap.main(["--junit-xml", str(xml), "--", str(fake)]) == 0
    got = {c.name: (c.requirements, c.level) for c in ingest.collect([str(xml)]).cases}
    assert got == {
        "one": (("REQ-1",), "sil"),
        "old": (("REQ-2",), ""),
        "list": (("REQ-3", "REQ-4"), ""),  # every id recorded: attribution quarantines it
        "twice": (("REQ-5", "REQ-6"), ""),
    }
    # the same lines through the libtest ingestor (a .libtest file plus its trace)
    from rules_requirements.ingest.libtest import merge_trace, parse_libtest

    cases = parse_libtest("running 1 test\ntest t::one ... ok\n")
    merge_trace(cases, '{"test": "t::one", "requirement": "REQ-1"}\n["not", "a", "record"]\n')
    assert [c.requirements for c in cases] == [("REQ-1",)]
