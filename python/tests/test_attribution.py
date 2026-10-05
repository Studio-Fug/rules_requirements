# SPDX-License-Identifier: AGPL-3.0-or-later
"""attribution.attribute(): the only place a test case gets an owner.

A test case verifies at most one requirement. This module holds one
regression test per path of the design's table by which a case could acquire
a requirement (``test_P01_*`` ... ``test_P31_*``, each named after its path),
the seeded fuzz of the owner invariant, and the unit behaviour of
:func:`~rules_requirements.attribution.attribute`.
"""

import ast
import dataclasses
import inspect
import os
import random
import re
import subprocess
import sys
import textwrap
import unittest
from types import MappingProxyType

import pytest
from conftest import write

from rules_requirements import attribution as rr_attribution
from rules_requirements import bazel, ingest, rr, trace
from rules_requirements import lock as rr_lock
from rules_requirements import model as rr_model
from rules_requirements.annotations import Reference
from rules_requirements.attribution import (
    ATTRIBUTION_CONFLICT,
    MULTI_TAG,
    SAME_CODE,
    Attribution,
    AttributionInvariantError,
    CaseKey,
    Member,
    Quarantine,
    attribute,
    resolve_cases,
)
from rules_requirements.config import DEFAULT_RULES, Config, parse_config
from rules_requirements.hooks import junit_writer, wrap
from rules_requirements.hooks import unittest as rr_unittest
from rules_requirements.hooks.checkplan import CheckPlan
from rules_requirements.hooks.ids import MultipleRequirementsWarning
from rules_requirements.ingest import Evidence, TestCase
from rules_requirements.model import Mitigation, Model, Requirement, Risk, UserNeed, VerifiedBy, read_model
from rules_requirements.trace import (
    FAILED,
    INCOMPLETE,
    INVALID,
    UNDER_VERIFIED,
    UNVERIFIED,
    VALIDATED,
    VERIFIED,
    build_matrix,
)
from rules_requirements.validate import validate

PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REQS = "".join(f"  - {{id: REQ-{i}, title: r{i}, satisfies: [UN-1]}}\n" for i in range(1, 7))


def model_at(tmp_path, requirements=REQS, config="", extra=""):
    text = (f"config: {config}\n" if config else "") + "user_needs: [{id: UN-1, title: n}]\n"
    text += "requirements:\n" + requirements + extra
    model, _ = read_model(write(tmp_path, "model/requirements.yaml", text))
    assert not model.parse_errors, model.parse_errors
    return model


def evidence(*cases):
    ev = Evidence()
    for case in cases:
        ev.add(case)
    return ev


def tc(name, status="passed", *, classname="c", target="//p:t", declared=(), **kw):
    return TestCase(name, status, classname=classname, target=target, declared=declared, **kw)


def owners(att):
    return {str(k): v for k, v in att.owner.items()}


def quarantines(att):
    return {str(q.key): (q.code, q.entities) for q in att.quarantined}


def issues(att, code):
    return [i for i in att.issues if i.code == code]


def static(model, code):
    return [i for i in validate(model) if i.code == code]


def states(att, entity):
    return sorted((str(m.key) if m.key else m.name, m.state) for m in att.members_of(entity))


def counted_once(mx):
    """No case key is an owned member of two entities' verdicts."""
    seen = {}
    for v in mx.verdicts.values():
        for m in v.members:
            if m.owned:
                assert seen.setdefault(m.key, v.id) == v.id, f"{m.key} counted for {seen[m.key]} and {v.id}"
    assert seen == dict(mx.attribution.owner)
    return seen


def junit_at(tmp_path, rel, body):
    return write(tmp_path, rel, f'<?xml version="1.0"?><testsuites>{body}</testsuites>')


# --------------------------------------------------------------------------- #
# P1-P3: pytest markers (a real pytest session through the plugin)           #
# --------------------------------------------------------------------------- #

_PYTEST_SOURCES = {
    # P1: one marker naming two ids
    "test_p01.py": """
        import pytest

        @pytest.mark.rr("REQ-1", "REQ-2")
        def test_two_ids():
            pass
        """,
    # P2: a marker at every scope: the nearest one's id is the case's only id
    "test_p02.py": """
        import pytest

        pytestmark = pytest.mark.rr("REQ-3")

        @pytest.mark.rr("REQ-4")
        class TestNear:
            @pytest.mark.rr("REQ-5")
            def test_function_wins(self):
                pass

            def test_class_wins(self):
                pass

        def test_module_wins():
            pass
        """,
    # P3: a raw record_property("requirement") next to a single-id marker
    "test_p03.py": """
        import pytest

        @pytest.mark.rr("REQ-6")
        def test_raw(record_property):
            record_property("requirement", "REQ-1")
        """,
}


@pytest.fixture(scope="module")
def pytest_run(tmp_path_factory):
    root = tmp_path_factory.mktemp("pyrun")
    for name, text in _PYTEST_SOURCES.items():
        (root / name).write_text(textwrap.dedent(text))
    (root / "main.py").write_text(
        "from rules_requirements.hooks.pytest_runner import main\nraise SystemExit(main(__file__))\n"
    )
    xml = root / "bazel-testlogs" / "py" / "hooks_test" / "test.xml"
    xml.parent.mkdir(parents=True)
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([PKG_ROOT] + [p for p in sys.path if p])
    env["XML_OUTPUT_FILE"] = str(xml)
    proc = subprocess.run(
        [sys.executable, str(root / "main.py"), "-q", "-p", "no:cacheprovider"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    return proc, ingest.collect([str(xml)])


def test_P01_pytest_marker_with_several_ids(tmp_path, pytest_run):
    proc, ev = pytest_run
    out = proc.stdout + proc.stderr
    assert "RR-E101" in out and "marker names REQ-1, REQ-2" in out  # L0: the deprecation, at collection
    mx = build_matrix(model_at(tmp_path), ev)
    key = "//py:hooks_test#test_p01::test_two_ids"
    assert quarantines(mx.attribution)[key] == (MULTI_TAG, ("REQ-1", "REQ-2"))
    assert key not in owners(mx.attribution)
    # Both ids it names read INVALID; it is passing evidence of neither.
    assert mx.status("REQ-1") == INVALID and mx.status("REQ-2") == INVALID
    for rid in ("REQ-1", "REQ-2"):
        assert not [e for e in mx.verdicts[rid].evidence if e.name == "test_p01::test_two_ids"]
    counted_once(mx)


def test_P02_markers_at_several_scopes_do_not_accumulate(tmp_path, pytest_run):
    _, ev = pytest_run
    mx = build_matrix(model_at(tmp_path), ev)
    got = {k: v for k, v in owners(mx.attribution).items() if "test_p02" in k}
    assert got == {
        "//py:hooks_test#test_p02.TestNear::test_function_wins": "REQ-5",
        "//py:hooks_test#test_p02.TestNear::test_class_wins": "REQ-4",
        "//py:hooks_test#test_p02::test_module_wins": "REQ-3",
    }
    for case in ev.cases:
        if case.classname.startswith("test_p02"):
            assert len(case.declared) == 1, case  # one property written, never the union of scopes
    assert [m.key.path for m in mx.attribution.members_of("REQ-3")] == ["test_p02::test_module_wins"]
    counted_once(mx)


def test_P03_raw_record_property_is_dropped_and_fails_the_test(tmp_path, pytest_run):
    _, ev = pytest_run
    (raw,) = [c for c in ev.cases if c.name == "test_raw"]
    assert raw.declared == ("REQ-6",) and raw.status == "failed" and "RR-E102" in raw.message
    mx = build_matrix(model_at(tmp_path), ev)
    assert owners(mx.attribution)["//py:hooks_test#test_p03::test_raw"] == "REQ-6"
    assert mx.status("REQ-6") == FAILED
    # REQ-1 never gets the case the raw property named it in.
    assert all(m.key.path != "test_p03::test_raw" for m in mx.attribution.members_of("REQ-1"))


# --------------------------------------------------------------------------- #
# P4-P10: the other hooks                                                     #
# --------------------------------------------------------------------------- #


def test_P04_stacked_rr_verifies_and_class_with_method_decorators(tmp_path):
    with pytest.warns(MultipleRequirementsWarning, match="RR-E101"):

        class Stacked(unittest.TestCase):
            @rr.verifies("REQ-1")
            @rr.verifies("REQ-2")
            def test_stacked(self):
                pass

    @rr.verifies("REQ-3")
    class Near(unittest.TestCase):
        @rr.verifies("REQ-4")
        def test_method_wins(self):
            pass

        def test_class_id(self):
            pass

    loader = unittest.TestLoader()
    suite = unittest.TestSuite([loader.loadTestsFromTestCase(Stacked), loader.loadTestsFromTestCase(Near)])
    xml = tmp_path / "bazel-testlogs" / "py" / "ut_test" / "test.xml"
    xml.parent.mkdir(parents=True)
    assert rr_unittest.run(suite, str(xml), "ut", verbosity=0)
    mx = build_matrix(model_at(tmp_path), ingest.collect([str(xml)]))
    att = mx.attribution
    (stacked,) = [k for k in quarantines(att) if k.endswith("test_stacked")]
    assert quarantines(att)[stacked] == (MULTI_TAG, ("REQ-1", "REQ-2"))
    assert {k.rsplit("::", 1)[1]: v for k, v in owners(att).items()} == {
        "test_method_wins": "REQ-4",  # nearest wins: the method's id replaces the class's
        "test_class_id": "REQ-3",
    }
    assert mx.status("REQ-1") == INVALID and mx.status("REQ-2") == INVALID
    counted_once(mx)


def test_P05_junit_writer_lists_checkplan_and_read_only_cases(tmp_path):
    w = junit_writer.JUnitWriter("hitl_e2e", file="")
    with pytest.warns(MultipleRequirementsWarning):
        w.add("legacy_phase", ["REQ-1", "REQ-2"])  # 0.2's PHASES: one case naming every PR
    w.add("single", requirement="REQ-3")
    with pytest.raises(dataclasses.FrozenInstanceError):
        w.cases[1].declared = ("REQ-1", "REQ-3")  # record_incomplete's re-attribution
    with pytest.raises(AttributeError):
        w.cases.append(w.cases[0])  # type: ignore[attr-defined]
    with pytest.raises(ValueError, match="RR-E10"):
        CheckPlan(w, {"s": ["c"]}, tags={"s.c": "REQ-1 REQ-2"})  # a check names ONE id
    plan = CheckPlan(w, {"s": ["a", "b"]}, tags={"s.a": "REQ-4", "s.b": "REQ-5"})
    with plan.run():
        plan.setup_done()
        with plan.step("s"):
            plan.passed("a")
            plan.passed("b")
    path = tmp_path / "bazel-testlogs" / "hitl" / "e2e" / "test.xml"
    path.parent.mkdir(parents=True)
    w.write(str(path))
    mx = build_matrix(model_at(tmp_path), ingest.collect([str(path)]))
    att = mx.attribution
    assert quarantines(att) == {"//hitl:e2e#hitl_e2e::legacy_phase": (MULTI_TAG, ("REQ-1", "REQ-2"))}
    assert owners(att) == {
        "//hitl:e2e#hitl_e2e::single": "REQ-3",
        "//hitl:e2e#hitl_e2e.s::a": "REQ-4",
        "//hitl:e2e#hitl_e2e.s::b": "REQ-5",
    }
    assert [mx.status(r) for r in ("REQ-1", "REQ-2", "REQ-3", "REQ-4", "REQ-5")] == [INVALID, INVALID] + [VERIFIED] * 3


def test_P06_rr_verifies_variadic_repeated_and_suite_level(tmp_path):
    # googletest output: RR_VERIFIES("REQ-1", "REQ-2") or two calls record one
    # comma list (0.3) or the 0.2 plural property; at suite level it is a
    # suite property.
    path = junit_at(
        tmp_path,
        "bazel-testlogs/cc/gtest_test/test.xml",
        '<testsuite name="Hook"><properties><property name="requirement" value="REQ-3"/></properties>'
        '<testcase classname="Hook" name="Variadic"><properties>'
        '<property name="requirement" value="REQ-1,REQ-2"/></properties></testcase>'
        '<testcase classname="Hook" name="Legacy" requirements="REQ-1, REQ-2"/>'
        '<testcase classname="Hook" name="Plain"/>'
        "</testsuite>",
    )
    mx = build_matrix(model_at(tmp_path), ingest.collect([path]))
    att = mx.attribution
    assert quarantines(att) == {
        "//cc:gtest_test#Hook::Variadic": (MULTI_TAG, ("REQ-1", "REQ-2")),
        "//cc:gtest_test#Hook::Legacy": (MULTI_TAG, ("REQ-1", "REQ-2")),
    }
    # The suite-level id reaches no case: nothing is owned, a warning says why.
    assert owners(att) == {} and mx.status("REQ-3") == UNVERIFIED
    (suite_level,) = issues(att, "suite-level-requirement")
    assert suite_level.declared == ("REQ-3",) and suite_level.target == "//cc:gtest_test"


def test_P07_rust_verifies_with_several_ids_or_called_twice(tmp_path):
    from rules_requirements.ingest.libtest import merge_trace, parse_libtest

    cases = parse_libtest(
        "running 3 tests\ntest m::list ... ok\ntest m::twice ... ok\ntest m::one ... ok\n", target="//rs:lib_test"
    )
    merge_trace(
        cases,
        '{"test": "m::list", "requirements": ["REQ-1", "REQ-2"]}\n'
        '{"test": "m::twice", "requirement": "REQ-1"}\n'
        '{"test": "m::twice", "requirement": "REQ-3"}\n'
        '{"test": "m::one", "requirement": "REQ-4"}\n',
    )
    mx = build_matrix(model_at(tmp_path), evidence(*cases))
    assert quarantines(mx.attribution) == {
        "//rs:lib_test#m::list": (MULTI_TAG, ("REQ-1", "REQ-2")),
        "//rs:lib_test#m::twice": (MULTI_TAG, ("REQ-1", "REQ-3")),
    }
    assert owners(mx.attribution) == {"//rs:lib_test#m::one": "REQ-4"}
    assert [mx.status(f"REQ-{i}") for i in (1, 2, 3, 4)] == [INVALID, INVALID, INVALID, VERIFIED]


def _exe(path, body):
    path.write_text("#!" + sys.executable + "\n" + body)
    path.chmod(0o755)
    return path


def test_P08_wrap_exit_status_case_declares_no_ids(tmp_path, monkeypatch):
    fake = _exe(
        tmp_path / "leaky",
        "import json, os, sys\n"
        "with open(os.environ['RR_TRACE_FILE'], 'a') as fh:\n"
        "    fh.write(json.dumps({'test': 'm::a', 'requirement': 'REQ-1'}) + '\\n')\n"
        "    fh.write(json.dumps({'test': 'm::b', 'requirement': 'REQ-2'}) + '\\n')\n"
        "print('running 2 tests')\nprint('test m::a ... ok')\nprint('test m::b ... ok')\n"
        "sys.exit(1)  # every test passed, then the binary failed\n",
    )
    monkeypatch.setenv("TEST_TMPDIR", str(tmp_path))
    xml = tmp_path / "bazel-testlogs" / "rs" / "leaky_test" / "test.xml"
    xml.parent.mkdir(parents=True)
    assert wrap.main(["--junit-xml", str(xml), "--target", "//rs:leaky_test", "--", str(fake)]) == 1
    ev = ingest.collect([str(xml)])
    (exit_case,) = [c for c in ev.cases if c.name == "exit-status"]
    assert exit_case.declared == () and exit_case.scope == "target"  # 0.2: the union REQ-1, REQ-2
    mx = build_matrix(model_at(tmp_path), ev)
    att = mx.attribution
    assert owners(att) == {"//rs:leaky_test#m::a": "REQ-1", "//rs:leaky_test#m::b": "REQ-2"}
    assert not [k for k in att.cases if k.path.endswith("exit-status")]  # never a case, never owned
    # The taint fails each requirement through its own member.
    for rid in ("REQ-1", "REQ-2"):
        (member,) = att.members_of(rid)
        assert member.state == "error" and "exit-status" in member.reason
        assert mx.status(rid) == FAILED


def test_P09_rr_evidence_exit_status_declares_no_ids(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "bin").mkdir()
    _exe(
        tmp_path / "bin" / "leaky",
        "import os, sys\n"
        "open(os.environ['XML_OUTPUT_FILE'],'w').write('<testsuite><testcase classname=\"c\" name=\"ok\">"
        '<properties><property name="requirement" value="REQ-2"/></properties></testcase></testsuite>\')\n'
        "sys.exit(23)\n",
    )
    out = tmp_path / "ev" / "testlogs"
    bazel.main(["run-tests", "--out", str(out), "--test", "//pkg:leaky=bin/leaky=_main"])
    assert (out / "pkg" / "leaky" / "test.exit.xml").exists()
    mx = build_matrix(model_at(tmp_path), ingest.collect([str(out)]))
    att = mx.attribution
    assert owners(att) == {"//pkg:leaky#c::ok": "REQ-2"}
    assert att.targets["//pkg:leaky"].tainted and mx.status("REQ-2") == FAILED
    assert [m.state for m in att.members_of("REQ-2")] == ["error"]


def test_P10_node_diagnostics_are_cross_checks_and_failures_outside_tests_taint(tmp_path):
    body = (
        '<testsuite name="probe">'
        '<testcase classname="probe" name="first"><properties>'
        '<property name="requirement" value="REQ-2"/><property name="rr.file" value="web/probe.test.cjs"/>'
        "</properties></testcase>"
        '<testcase classname="probe" name="second"/>'
        '<testcase classname="probe" name="&lt;exit-status&gt;"><error message="node exited 1 although no test '
        'failed"/><properties><property name="rr.scope" value="target"/></properties></testcase>'
        "</testsuite>"
    )
    paths = [
        junit_at(tmp_path, "bazel-testlogs/web/probe_test/test.xml", body),
        junit_at(tmp_path, "bazel-testlogs/web/zero_test/test.xml", '<testsuite name="zero" tests="0"/>'),
        junit_at(
            tmp_path,
            "bazel-testlogs/web/load_test/test.xml",
            '<testsuite name="load"><testcase classname="load" name="&lt;load&gt;"><error message="exit 7"/>'
            '<properties><property name="rr.scope" value="target"/></properties></testcase></testsuite>',
        ),
    ]
    reqs = (
        "  - {id: REQ-1, title: a, satisfies: [UN-1], verified_by: [{target: //web:probe_test, cases: ['probe::*']}]}\n"
        "  - {id: REQ-2, title: b, satisfies: [UN-1]}\n"
        "  - {id: REQ-3, title: c, satisfies: [UN-1], verified_by: [{target: //web:zero_test, cases: ['zero::*']}]}\n"
        "  - {id: REQ-4, title: d, satisfies: [UN-1], verified_by: [{target: //web:load_test, cases: ['load::x']}]}\n"
    )
    mx = build_matrix(model_at(tmp_path, reqs, config="{attribution: model}"), ingest.collect(paths))
    att = mx.attribution
    assert owners(att) == {"//web:probe_test#probe::first": "REQ-1", "//web:probe_test#probe::second": "REQ-1"}
    # The diagnostic's id is a cross-check only: a tag-mismatch, no ownership.
    (mismatch,) = issues(att, "tag-mismatch")
    assert mismatch.key == CaseKey("//web:probe_test", "probe::first") and mismatch.declared == ("REQ-2",)
    assert mx.status("REQ-2") == UNVERIFIED
    # A non-zero exit after passing cases taints every member of the target.
    assert mx.status("REQ-1") == FAILED and {m.state for m in att.members_of("REQ-1")} == {"error"}
    # Zero tests (an empty suite): the target ran, its claimed cases are missing.
    assert states(att, "REQ-3") == [("//web:zero_test zero::*", "missing")] and mx.status("REQ-3") == INCOMPLETE
    # A load error: no case at all, the member reads error.
    assert states(att, "REQ-4") == [("//web:load_test#load::x", "error")] and mx.status("REQ-4") == FAILED


# --------------------------------------------------------------------------- #
# P11-P14: evidence shapes and third-party producers                          #
# --------------------------------------------------------------------------- #


def test_P11_any_junit_producer_naming_several_ids(tmp_path):
    path = junit_at(
        tmp_path,
        "bazel-testlogs/go/pkg_test/test.xml",
        '<testsuite name="s">'
        '<testcase classname="c" name="attr" requirements="REQ-1,REQ-2"/>'
        '<testcase classname="c" name="repeated"><properties><property name="requirement" value="REQ-1"/>'
        '<property name="requirement" value="REQ-2"/></properties></testcase>'
        '<testcase classname="c" name="mixed" requirement="REQ-1"><properties>'
        '<property name="requirement" value="REQ-2"/></properties></testcase>'
        '<testcase classname="c" name="plural"><properties><property name="requirements" value="REQ-1 REQ-2"/>'
        "</properties></testcase>"
        '<testcase classname="c" name="tags [rr:REQ-1] [rr:REQ-2]"/>'
        '<testcase classname="c" name="tag list [rr:REQ-1,REQ-2]"/>'
        '<testcase classname="c" name="one"><properties><property name="requirement" value="REQ-1"/>'
        "</properties></testcase>"
        "</testsuite>",
    )
    mx = build_matrix(model_at(tmp_path), ingest.collect([path]))
    got = quarantines(mx.attribution)
    for name in ("attr", "repeated", "mixed", "plural", "tags", "tag list"):
        assert got.pop(f"//go:pkg_test#c::{name}") == (MULTI_TAG, ("REQ-1", "REQ-2")), name
    assert got == {} and owners(mx.attribution) == {"//go:pkg_test#c::one": "REQ-1"}
    assert mx.status("REQ-1") == INVALID and mx.status("REQ-2") == INVALID


def test_P12_suite_level_properties_are_not_inherited(tmp_path):
    path = junit_at(
        tmp_path,
        "bazel-testlogs/s/suite_test/test.xml",
        '<testsuite name="s"><properties><property name="requirement" value="REQ-1"/>'
        '<property name="level" value="hil"/></properties>'
        '<testcase classname="c" name="a"/><testcase classname="c" name="b"/></testsuite>',
    )
    mx = build_matrix(model_at(tmp_path), ingest.collect([path]))
    att = mx.attribution
    assert owners(att) == {} and mx.status("REQ-1") == UNVERIFIED
    assert {r.level for r in att.cases.values()} == {"hil"}  # a level still reaches the cases
    assert issues(att, "suite-level-requirement")[0].declared == ("REQ-1",)


def test_P13_records_naming_several_ids(tmp_path):
    path = write(
        tmp_path,
        "evidence/bench.rr.yaml",
        """
        target: record:bench
        evidence:
          - {name: two, status: passed, requirements: [REQ-1, REQ-2]}
          - {name: one, status: passed, requirement: REQ-3}
          - {name: legacy, status: passed, requirements: [REQ-4]}
          - {name: elsewhere, status: passed, requirement: REQ-5, target: "record:other"}
        """,
    )
    mx = build_matrix(model_at(tmp_path), ingest.collect([path]))
    assert quarantines(mx.attribution) == {"record:bench#two": (MULTI_TAG, ("REQ-1", "REQ-2"))}
    assert owners(mx.attribution) == {
        "record:bench#one": "REQ-3",
        "record:bench#legacy": "REQ-4",
        "record:other#elsewhere": "REQ-5",
    }


def test_P14_third_party_ingestors_filling_requirements(tmp_path):
    class OtherIngestor(ingest.Ingestor):
        name = "p14-third-party"
        suffixes = (".p14",)

        def ingest(self, path):
            with pytest.deprecated_call():
                both = TestCase("both", "passed", classname="t", target="//t:p14_test")
                both.requirements = ["REQ-1", "REQ-2"]  # the deprecated alias, as 0.2 ingestors wrote it
                one = TestCase("one", "passed", classname="t", target="//t:p14_test", requirements=["REQ-3"])
            return [both, one]

    ingest.register(OtherIngestor())
    try:
        ev = ingest.collect([write(tmp_path, "r.p14", "x")], only=["p14-third-party"])
    finally:
        ingest._REGISTRY.pop("p14-third-party", None)
    assert [c.declared for c in ev.cases] == [("REQ-1", "REQ-2"), ("REQ-3",)]  # declared: tags, no owner field
    mx = build_matrix(model_at(tmp_path), ev)
    assert quarantines(mx.attribution) == {"//t:p14_test#t::both": (MULTI_TAG, ("REQ-1", "REQ-2"))}
    assert owners(mx.attribution) == {"//t:p14_test#t::one": "REQ-3"}


# --------------------------------------------------------------------------- #
# P15-P20: claims                                                             #
# --------------------------------------------------------------------------- #


def test_P15_two_requirements_naming_one_whole_target(tmp_path):
    reqs = (
        "  - {id: REQ-1, title: a, satisfies: [UN-1], verified_by: [//web:clocksync_test]}\n"
        "  - {id: REQ-2, title: b, satisfies: [UN-1], verified_by: "
        "[{target: //web:clocksync_test, whole: true, reason: one run}]}\n"
    )
    model = model_at(tmp_path, reqs)
    (shared,) = static(model, "shared-case")
    assert "REQ-2 and REQ-1 both claim the whole target //web:clocksync_test" in shared.message
    ev = evidence(*(tc(n, target="//web:clocksync_test", classname="clocksync") for n in ("offset", "drift")))
    mx = build_matrix(model, ev)
    assert quarantines(mx.attribution) == {
        "//web:clocksync_test#clocksync::drift": (ATTRIBUTION_CONFLICT, ("REQ-1", "REQ-2")),
        "//web:clocksync_test#clocksync::offset": (ATTRIBUTION_CONFLICT, ("REQ-1", "REQ-2")),
    }
    assert mx.status("REQ-1") == INVALID and mx.status("REQ-2") == INVALID and owners(mx.attribution) == {}
    # The per-key gap lists each claim's origin.
    (gap,) = [g for g in mx.gaps if g.kind == ATTRIBUTION_CONFLICT and g.entity.endswith("offset")]
    assert "is claimed by REQ-1 (" in gap.message and "requirements.yaml:3) and REQ-2 (" in gap.message
    assert gap.message.endswith("it verifies neither until it has one owner")


def test_P16_overlapping_patterns(tmp_path):
    reqs = (
        "  - {id: REQ-1, title: a, satisfies: [UN-1], verified_by: [{target: //w:t, cases: ['c::a*']}]}\n"
        "  - {id: REQ-2, title: b, satisfies: [UN-1], verified_by: [{target: //w:t, cases: ['c::*b']}]}\n"
    )
    model = model_at(tmp_path, reqs)
    (shared,) = static(model, "shared-case")
    assert "e.g. 'c::ab'" in shared.message  # the exact static witness
    mx = build_matrix(model, evidence(*(tc(n, target="//w:t") for n in ("ab", "ax", "xb"))))
    assert quarantines(mx.attribution) == {"//w:t#c::ab": (ATTRIBUTION_CONFLICT, ("REQ-1", "REQ-2"))}
    assert owners(mx.attribution) == {"//w:t#c::ax": "REQ-1", "//w:t#c::xb": "REQ-2"}


def test_P17_whole_and_pattern_claims_on_one_target(tmp_path):
    reqs = (
        "  - {id: REQ-1, title: a, satisfies: [UN-1], verified_by: [{target: //w:t, whole: true, reason: r}]}\n"
        "  - {id: REQ-2, title: b, satisfies: [UN-1], verified_by: [{target: //w:t, cases: ['c::a']}]}\n"
    )
    model = model_at(tmp_path, reqs)
    assert static(model, "shared-case")
    mx = build_matrix(model, evidence(tc("a", target="//w:t"), tc("b", target="//w:t")))
    assert quarantines(mx.attribution) == {"//w:t#c::a": (ATTRIBUTION_CONFLICT, ("REQ-1", "REQ-2"))}
    assert owners(mx.attribution) == {"//w:t#c::b": "REQ-1"}


def test_P18_one_target_spelled_differently(tmp_path):
    reqs = (
        "  - {id: REQ-1, title: a, satisfies: [UN-1], verified_by: ['//p']}\n"
        "  - {id: REQ-2, title: b, satisfies: [UN-1], verified_by: [{target: '@@//p:p', cases: ['c::*']}]}\n"
        "  - {id: REQ-3, title: c, satisfies: [UN-1], verified_by: [{target: '@splanc//q:t', cases: ['*']}]}\n"
        "  - {id: REQ-4, title: d, satisfies: [UN-1], verified_by: [{target: '@foo//x:y', cases: ['*']}]}\n"
        "  - {id: REQ-5, title: e, satisfies: [UN-1], verified_by: [{target: 'p:t', cases: ['*']}]}\n"
    )
    model = model_at(tmp_path, reqs, config="{main_repo: splanc}")
    (shared,) = static(model, "shared-case")
    assert "//p:p" in shared.message
    assert static(model, "bad-target")  # 'p:t' is no label
    paths = [
        junit_at(
            tmp_path,
            "bazel-testlogs/p/p/test.xml",
            '<testsuite name="s"><testcase classname="c" name="x"/></testsuite>',
        ),
        junit_at(
            tmp_path,
            "bazel-testlogs/q/t/test.xml",
            '<testsuite name="s"><testcase classname="c" name="y"/></testsuite>',
        ),
        junit_at(
            tmp_path,
            "bazel-testlogs/external/foo~/x/y/test.xml",
            '<testsuite name="s"><testcase classname="c" name="z"/></testsuite>',
        ),
    ]
    mx = build_matrix(model, ingest.collect(paths))
    assert quarantines(mx.attribution) == {"//p:p#c::x": (ATTRIBUTION_CONFLICT, ("REQ-1", "REQ-2"))}
    assert owners(mx.attribution) == {"//q:t#c::y": "REQ-3", "@foo//x:y#c::z": "REQ-4"}


def test_P19_model_and_tag_disagree_and_hybrid_tags(tmp_path):
    reqs = "  - {id: REQ-1, title: a, satisfies: [UN-1], verified_by: [{target: //w:t, cases: ['c::a']}]}\n" + "".join(
        f"  - {{id: REQ-{i}, title: r, satisfies: [UN-1]}}\n" for i in (2, 3)
    )
    ev = evidence(tc("a", target="//w:t", declared=("REQ-2",)), tc("b", target="//w:t", declared=("REQ-3",)))
    model_mode = build_matrix(model_at(tmp_path, reqs, config="{attribution: model}"), ev).attribution
    assert owners(model_mode) == {"//w:t#c::a": "REQ-1"}  # the model wins; the tag owns nothing
    assert [i.key for i in issues(model_mode, "tag-mismatch")] == [CaseKey("//w:t", "c::a")]
    assert [i.declared for i in issues(model_mode, "unclaimed-tag")] == [("REQ-3",)]
    hybrid = build_matrix(model_at(tmp_path / "h", reqs), ev).attribution
    assert owners(hybrid) == {"//w:t#c::a": "REQ-1", "//w:t#c::b": "REQ-3"}  # a tag only fills unclaimed keys
    assert hybrid.via[CaseKey("//w:t", "c::a")] == "model" and hybrid.via[CaseKey("//w:t", "c::b")] == "tag"


def test_P20_cross_kind_claims_share_one_namespace(tmp_path):
    text = (
        "user_needs: [{id: UN-1, title: n, validated_by: [{target: //u:study, cases: ['*']}]}]\n"
        "requirements:\n"
        "  - {id: REQ-1, title: a, satisfies: [UN-1], verified_by: [{target: //u:study, cases: ['s::a']}]}\n"
        "  - {id: REQ-2, title: b, satisfies: [UN-1]}\n"
        "mitigations: [{id: MIT-1, title: m, mitigates: [RISK-1], implemented_by: [REQ-2], "
        "verified_by: [{target: //u:study, cases: ['s::c']}]}]\n"
        "risks: [{id: RISK-1, title: k}]\n"
        "test_methods: [{id: TM-1, title: t, level: hil}]\n"
    )
    model, _ = read_model(write(tmp_path, "m.yaml", text))
    assert len(static(model, "shared-case")) == 2  # UN-1 vs REQ-1, UN-1 vs MIT-1
    ev = evidence(
        *(tc(n, target="//u:study", classname="s") for n in ("a", "b", "c")),
        tc("risky", target="//u:other", declared=("RISK-1",)),
        tc("method", target="//u:other", declared=("TM-1",)),
    )
    mx = build_matrix(model, ev)
    att = mx.attribution
    assert quarantines(att) == {
        "//u:study#s::a": (ATTRIBUTION_CONFLICT, ("REQ-1", "UN-1")),
        "//u:study#s::c": (ATTRIBUTION_CONFLICT, ("MIT-1", "UN-1")),
    }
    assert owners(att) == {"//u:study#s::b": "UN-1"}
    assert mx.status("UN-1") == INVALID and mx.status("REQ-1") == INVALID and mx.status("MIT-1") == INVALID
    # Risks and test methods own nothing: their tags are misdirected evidence.
    assert {i.declared for i in issues(att, "misdirected-evidence")} == {("RISK-1",), ("TM-1",)}
    assert {g.entity for g in mx.gaps if g.kind == "misdirected-evidence"} == {"RISK-1", "TM-1"}
    # ... and cannot hold claims (an unknown field is an error).
    risky, _ = read_model(
        write(tmp_path, "r.yaml", "risks: [{id: RISK-1, title: k, verified_by: [{target: //u:study, cases: ['*']}]}]\n")
    )
    assert any(i.code == "unknown-field" and i.severity == "error" for i in validate(risky))
    assert risky.claims() == []


# --------------------------------------------------------------------------- #
# P21-P23: one case, many observations; one code, many targets               #
# --------------------------------------------------------------------------- #


def test_P21_one_case_ingested_several_times(tmp_path):
    def report(rel, cases):
        rows = "".join(
            f'<testcase classname="c" name="{n}">'
            + (f'<properties><property name="requirement" value="{r}"/></properties>' if r else "")
            + ('<failure message="boom"/>' if s == "failed" else "")
            + "</testcase>"
            for n, s, r in cases
        )
        junit_at(tmp_path, rel, f'<testsuite name="s">{rows}</testsuite>')

    report("bazel-testlogs/a/t/test.xml", [("x", "passed", "REQ-1")])
    report("bazel-testlogs/a/t/test_attempts/attempt_1.xml", [("x", "failed", "REQ-1")])
    report("bazel-testlogs/a/r/run_1_of_2/test.xml", [("y", "passed", "REQ-2")])
    report("bazel-testlogs/a/r/run_2_of_2/test.xml", [("y", "failed", "REQ-2")])
    report("bazel-testlogs/a/s/shard_1_of_2/test.xml", [("p", "passed", "REQ-3")])
    report("bazel-testlogs/a/s/shard_2_of_2/test.xml", [("q", "passed", "REQ-3")])
    report("ev/testlogs/a/t/test.xml", [("x", "passed", "REQ-1")])  # rr_evidence next to bazel-testlogs
    report("hitl/bazel-testlogs/a/t/test.xml", [("x", "passed", "REQ-1")])  # the hitl lane's root
    report("bazel-testlogs/a/u/test.xml", [("z", "passed", "REQ-4")])
    report("hitl/bazel-testlogs/a/u/test.xml", [("z", "passed", "REQ-5")])  # two roots disagree: fail closed
    mx = build_matrix(model_at(tmp_path), ingest.collect([str(tmp_path)]))
    att = mx.attribution
    assert owners(att) == {"//a:r#c::y": "REQ-2", "//a:s#c::p": "REQ-3", "//a:s#c::q": "REQ-3", "//a:t#c::x": "REQ-1"}
    x = att.cases[CaseKey("//a:t", "c::x")]
    assert x.status == "passed" and x.flaky and x.attempts == 2 and len(x.sources) == 4
    assert [m.key.path for m in att.members_of("REQ-1")] == ["c::x"]  # one key, one member
    assert mx.status("REQ-1") == UNDER_VERIFIED  # a retry-masked pass (flaky: under-verify)
    assert mx.status("REQ-2") == FAILED  # every run must pass
    assert quarantines(att) == {"//a:u#c::z": (MULTI_TAG, ("REQ-4", "REQ-5"))}
    counted_once(mx)


_CRASH_CLAIMS = {
    "bare": '"//p:t"',
    "whole": "{target: //p:t, whole: true, reason: r}",
    "glob": "{target: //p:t, cases: ['c::*']}",
    "literal": "{target: //p:t, cases: ['c::a', 'c::b']}",
}


def _crashed_repetition(slot):
    """c::a and c::b pass in the first shard or run; the second crashed before
    writing JUnit, so Bazel generated its test.xml (a failed synthetic result)."""
    return evidence(
        tc("a", source=f"bazel-testlogs/p/t/{slot}_1_of_2/test.xml"),
        tc("b", source=f"bazel-testlogs/p/t/{slot}_1_of_2/test.xml"),
        tc(
            "p/t",
            "error",
            classname="",
            source=f"bazel-testlogs/p/t/{slot}_2_of_2/test.xml",
            message="exited with error code 139",
            properties={"rr.synthetic": "true"},
        ),
    )


@pytest.mark.parametrize("slot", ["shard", "run"])
@pytest.mark.parametrize("claim", sorted(_CRASH_CLAIMS))
def test_P21_a_crashed_shard_or_run_beside_per_case_results_fails_the_target(tmp_path, slot, claim):
    """The worst run wins: a repetition that crashed (Bazel's generated result)
    taints the target, so no claim on it reads VERIFIED from the repetitions
    that did report (v0.2.1 gave FAILED; it must not silently pass)."""
    reqs = f"  - {{id: REQ-1, title: a, satisfies: [UN-1], verified_by: [{_CRASH_CLAIMS[claim]}]}}\n"
    mx = build_matrix(model_at(tmp_path, reqs), _crashed_repetition(slot))
    att = mx.attribution
    run = att.targets["//p:t"]
    assert run.tainted and not run.synthetic_only
    assert run.taint_message == f"p/t ({slot}_2_of_2): exited with error code 139"
    assert CaseKey("//p:t", "[target]") not in att.cases  # a crash is no case
    assert {m.state for m in att.members_of("REQ-1")} == {"error"}
    assert mx.status("REQ-1") == FAILED
    assert f"tainted: p/t ({slot}_2_of_2)" in att.members_of("REQ-1")[0].reason
    assert not any(g.kind == "unattributed-failure" for g in mx.gaps)  # the failure is REQ-1's
    counted_once(mx)


@pytest.mark.parametrize("slot", ["shard", "run"])
def test_P21_a_crashed_repetition_fails_a_locked_set(tmp_path, slot):
    """With a lock, c::c (which ran only in the crashed repetition) is not merely
    missing: the target is tainted, so the set reads FAILED, not INCOMPLETE."""
    reqs = "  - {id: REQ-1, title: a, satisfies: [UN-1], verified_by: [{target: //p:t, cases: ['c::*']}]}\n"
    lock = rr_lock.Lock(tuple(rr_lock.LockEntry("//p:t", f"c::{n}", "REQ-1") for n in "abc"))
    mx = build_matrix(model_at(tmp_path, reqs), _crashed_repetition(slot), lock=lock)
    assert states(mx.attribution, "REQ-1") == [
        ("//p:t#c::a", "error"),
        ("//p:t#c::b", "error"),
        ("//p:t#c::c", "error"),
    ]
    assert mx.status("REQ-1") == FAILED


def test_P21_a_passing_or_lone_synthetic_result_is_unchanged(tmp_path):
    """Only a *failed* synthetic result beside per-case results is a crash; a
    lone one is still the target's single case (FAILED or VERIFIED by it)."""
    reqs = "  - {id: REQ-1, title: a, satisfies: [UN-1], verified_by: [{target: //p:t, cases: ['c::*']}]}\n"
    ok = evidence(
        tc("a", source="bazel-testlogs/p/t/shard_1_of_2/test.xml"),
        tc("p/t", classname="", source="bazel-testlogs/p/t/shard_2_of_2/test.xml", properties={"rr.synthetic": "true"}),
    )
    mx = build_matrix(model_at(tmp_path, reqs), ok)
    assert not mx.attribution.targets["//p:t"].tainted and mx.status("REQ-1") == VERIFIED
    alone = evidence(tc("p/t", "error", classname="", source="bazel-testlogs/p/t/test.xml",
                        properties={"rr.synthetic": "true"}))  # fmt: skip
    mx = build_matrix(model_at(tmp_path / "alone", reqs), alone)
    assert not mx.attribution.targets["//p:t"].tainted and mx.status("REQ-1") == FAILED


def test_P22_duplicate_names_in_one_run(tmp_path):
    path = junit_at(
        tmp_path,
        "bazel-testlogs/web/dup_test/test.xml",
        '<testsuite name="s"><testcase classname="dup" name="same"><properties>'
        '<property name="requirement" value="REQ-1"/></properties></testcase>'
        '<testcase classname="dup" name="same"><properties><property name="requirement" value="REQ-1"/>'
        '</properties><failure message="boom"/></testcase></testsuite>',
    )
    mx = build_matrix(model_at(tmp_path), ingest.collect([path]))
    att = mx.attribution
    (member,) = att.members_of("REQ-1")
    assert member.state == "failed" and member.result.duplicate and mx.status("REQ-1") == FAILED
    assert [i.key for i in issues(att, "duplicate-case")] == [CaseKey("//web:dup_test", "dup::same")]
    assert {g.kind for g in mx.gaps} >= {"duplicate-case", "failed"}


def test_P23_same_test_code_in_two_targets(tmp_path):
    fx = {"rr.file": "pi/hitl/harness/fx_bench.py"}
    reqs = (
        "  - {id: REQ-1, title: a, satisfies: [UN-1], verified_by: [{target: //h:fx, cases: ['*']}]}\n"
        "  - {id: REQ-2, title: b, satisfies: [UN-1], verified_by: [{target: //h:fx_jit, cases: ['*']}]}\n"
        "  - {id: REQ-3, title: c, satisfies: [UN-1], verified_by: [{target: //h:led, whole: true, reason: r}]}\n"
        "  - {id: REQ-4, title: d, satisfies: [UN-1], verified_by: [{target: //h:led_jit, whole: true, reason: r}]}\n"
        "  - {id: REQ-5, title: e, satisfies: [UN-1], verified_by: [{target: //x:one, cases: ['*']}]}\n"
        "  - {id: REQ-6, title: f, satisfies: [UN-1], verified_by: [{target: //x:two, cases: ['*']}]}\n"
    )
    model = model_at(tmp_path, reqs, config="{variants: [[//h:led, //h:led_jit]]}")
    assert static(model, SAME_CODE)  # a declared variant group is a static error
    ev = evidence(
        tc("bench", classname="fx", target="//h:fx", properties=fx),  # file identity: report time only
        tc("bench", classname="fx", target="//h:fx_jit", properties=fx),
        tc("led", target="//h:led", properties={"rr.synthetic": "true"}),
        tc("led_jit", target="//h:led_jit", properties={"rr.synthetic": "true"}),
        tc("t", target="//x:one"),  # equal paths, no source file to compare
        tc("t", target="//x:two"),
    )
    mx = build_matrix(model, ev)
    att = mx.attribution
    assert quarantines(att) == {
        "//h:fx#fx::bench": (SAME_CODE, ("REQ-1",)),
        "//h:fx_jit#fx::bench": (SAME_CODE, ("REQ-2",)),
        "//h:led#[target]": (SAME_CODE, ("REQ-3",)),
        "//h:led_jit#[target]": (SAME_CODE, ("REQ-4",)),
    }
    assert [mx.status(f"REQ-{i}") for i in (1, 2, 3, 4)] == [INVALID] * 4
    assert (
        "runs the same test code as //h:fx_jit#fx::bench (owned by REQ-2)"
        in att.quarantine_of(CaseKey("//h:fx", "fx::bench")).detail
    )
    (fallback,) = issues(att, "same-path-multiple-owners")
    assert fallback.entities == ("REQ-5", "REQ-6") and owners(att) == {"//x:one#c::t": "REQ-5", "//x:two#c::t": "REQ-6"}
    # One owner for both: the same code verifies one requirement, fine.
    one = model_at(tmp_path / "one", "  - {id: REQ-1, title: a, satisfies: [UN-1], verified_by: "
                   "[{target: //h:fx, cases: ['*']}, {target: //h:fx_jit, cases: ['*']}]}\n")  # fmt: skip
    assert build_matrix(one, ev).status("REQ-1") == VERIFIED


@pytest.mark.parametrize(
    "second",
    ["./pi/h/fx_bench.py", "pi/h//fx_bench.py", "pi/h/../h/fx_bench.py", "/home/ci/splanc/pi/h/fx_bench.py"],
)
@pytest.mark.parametrize("via", ["property", "attribute"])
def test_P23_same_code_however_its_file_is_spelled(tmp_path, monkeypatch, second, via):
    """``./x``, ``x//y``, ``a/../b`` or the absolute path (a HITL harness started
    outside the workspace root records it) are the same source file."""
    monkeypatch.delenv("BUILD_WORKSPACE_DIRECTORY", raising=False)
    reqs = (
        "  - {id: REQ-1, title: a, satisfies: [UN-1], verified_by: [{target: //h:fx_bench, cases: ['fx.e2e::run']}]}\n"
        "  - {id: REQ-2, title: b, satisfies: [UN-1], verified_by: [{target: //h:fx_bench_jit, cases: ['fx.e2e::run']}]}\n"
    )

    def case(target, file):
        kw = {"properties": {"rr.file": file}} if via == "property" else {"file": file}
        return tc("run", classname="fx.e2e", target=target, **kw)

    mx = build_matrix(model_at(tmp_path, reqs), evidence(case("//h:fx_bench", "pi/h/fx_bench.py"),
                                                          case("//h:fx_bench_jit", second)))  # fmt: skip
    att = mx.attribution
    assert quarantines(att) == {
        "//h:fx_bench#fx.e2e::run": (SAME_CODE, ("REQ-1",)),
        "//h:fx_bench_jit#fx.e2e::run": (SAME_CODE, ("REQ-2",)),
    }
    assert mx.status("REQ-1") == mx.status("REQ-2") == INVALID and not att.owner


def test_P23_differing_files_at_one_path_still_warn(tmp_path):
    """Two recorded files that do not resolve to one (another checkout's
    absolute path, say) may still be one source: same-path-multiple-owners."""
    reqs = (
        "  - {id: REQ-1, title: a, satisfies: [UN-1], verified_by: [{target: //h:a, cases: ['*']}]}\n"
        "  - {id: REQ-2, title: b, satisfies: [UN-1], verified_by: [{target: //h:b, cases: ['*']}]}\n"
    )
    ev = evidence(
        tc("run", target="//h:a", properties={"rr.file": "pi/h/fx_bench.py"}),
        tc("run", target="//h:b", properties={"rr.file": "/other/checkout/pi/h/fx_bench_copy.py"}),
    )
    att = build_matrix(model_at(tmp_path, reqs), ev).attribution
    assert not att.quarantined and owners(att) == {"//h:a#c::run": "REQ-1", "//h:b#c::run": "REQ-2"}
    (warning,) = issues(att, "same-path-multiple-owners")
    assert warning.entities == ("REQ-1", "REQ-2") and "/other/checkout/pi/h/fx_bench_copy.py" in warning.message


# --------------------------------------------------------------------------- #
# P24-P31: rollups, the lock, editors, annotations, configuration, the API   #
# --------------------------------------------------------------------------- #


def test_P24_rollups_propagate_verdicts_never_cases(tmp_path):
    reqs = (
        "  - {id: REQ-1, title: system, satisfies: [UN-1]}\n"
        "  - {id: REQ-2, title: a, refines: [REQ-1], verified_by: [{target: //w:t, cases: ['c::a']}]}\n"
        "  - {id: REQ-3, title: b, refines: [REQ-1], verified_by: [{target: //w:t, cases: ['c::b']}]}\n"
        "  - {id: REQ-4, title: parent with claims, satisfies: [UN-1], verified_by: [{target: //w:t, cases: ['c::p']}]}\n"
        "  - {id: REQ-5, title: child, refines: [REQ-4], verified_by: [{target: //w:t, cases: ['c::k']}]}\n"
    )
    extra = (
        "mitigations: [{id: MIT-1, title: m, mitigates: [RISK-1], implemented_by: [REQ-1]}]\n"
        "risks: [{id: RISK-1, title: k}]\n"
    )
    model = model_at(tmp_path, reqs, extra=extra)
    assert [i.entity for i in validate(model) if i.code == "parent-with-claims"] == ["REQ-4"]
    mx = build_matrix(model, evidence(*(tc(n, target="//w:t") for n in ("a", "b", "p", "k"))))
    v = mx.verdicts
    assert (
        v["REQ-1"].status == VERIFIED
        and v["REQ-1"].basis == "derived"
        and v["REQ-1"].derived_from == ["REQ-2", "REQ-3"]
    )
    assert v["REQ-1"].members == () and v["REQ-1"].evidence == []  # a parent never lists a child's cases
    assert v["REQ-4"].basis == "own+derived" and [m.key.path for m in v["REQ-4"].members] == ["c::p"]
    assert (
        v["UN-1"].status == VALIDATED and v["UN-1"].basis == "derived" and v["UN-1"].derived_from == ["REQ-1", "REQ-4"]
    )
    assert (v["MIT-1"].basis, v["MIT-1"].derived_from) == ("derived", ["REQ-1"])
    assert (v["RISK-1"].basis, v["RISK-1"].derived_from) == ("derived", ["MIT-1"])
    counted_once(mx)
    # INVALID rolls up like FAILED: the parent fails, it is not INVALID itself.
    bad = build_matrix(model, evidence(tc("a", target="//w:t", declared=("REQ-2", "REQ-3")), tc("b", target="//w:t")))
    assert bad.status("REQ-2") == INVALID and bad.status("REQ-1") == FAILED and bad.status("UN-1") == FAILED


def test_P25_hand_edits_to_the_lock(tmp_path):
    reqs = (
        "  - {id: REQ-1, title: a, satisfies: [UN-1], verified_by: [{target: //w:t, cases: ['c::a']}]}\n"
        "  - {id: REQ-2, title: b, satisfies: [UN-1]}\n"
    )
    write(
        tmp_path,
        "model/verification.rrlock",
        "schema: rules_requirements/verification-lock/v1\ncases:\n  //w:t:\n    c::a: REQ-2\n    c::free: REQ-2\n",
    )
    model = model_at(tmp_path, reqs, config="{attribution: model, sets_lock: verification.rrlock}")
    assert static(model, "lock-owner-changed")  # statically: c::a is locked to REQ-2, claimed by REQ-1
    mx = build_matrix(model, evidence(tc("a", target="//w:t"), tc("free", target="//w:t")))
    att = mx.attribution
    # The lock never creates ownership: the claim decides, and the unclaimed
    # c::free stays unowned although the lock names an owner for it.
    assert owners(att) == {"//w:t#c::a": "REQ-1"}
    assert states(att, "REQ-2") == [("//w:t#c::a", "moved"), ("//w:t#c::free", "moved")]
    assert mx.status("REQ-2") == INCOMPLETE and mx.status("REQ-1") == VERIFIED
    assert {i.key.path for i in issues(att, "lock-owner-changed")} == {"c::a", "c::free"}
    # The file cannot name two owners for a case.
    lock_head = "schema: rules_requirements/verification-lock/v1\ncases:\n  //w:t:\n"
    with pytest.raises(rr_lock.LockError, match="exactly one owner"):
        rr_lock.parse_lock(lock_head + "    c::a: [REQ-1, REQ-2]\n")
    with pytest.raises(rr_lock.LockError, match="duplicate key"):
        rr_lock.parse_lock(lock_head + "    c::a: REQ-1\n    c::a: REQ-2\n")
    with pytest.raises(rr_lock.LockError, match="not one id"):
        rr_lock.parse_lock(lock_head + "    c::a: REQ-1, REQ-2\n")


def test_P26_editor_and_hand_edits_go_through_attribution(tmp_path):
    from rules_requirements.server.workspace import Workspace

    write(
        tmp_path,
        "req/model.yaml",
        "user_needs: [{id: UN-1, title: n}]\nrequirements:\n"
        "  - {id: REQ-1, title: a, satisfies: [UN-1], verified_by: [{target: //w:t, cases: ['c::*']}]}\n"
        "  - {id: REQ-2, title: b, satisfies: [UN-1], verified_by: [{target: //w:t, cases: ['c::a']}]}\n",
    )
    junit_at(
        tmp_path,
        "logs/bazel-testlogs/w/t/test.xml",
        '<testsuite name="s"><testcase classname="c" name="a"/></testsuite>',
    )
    ws = Workspace(root=str(tmp_path), model_paths=["req"], evidence_paths=["logs"])
    snap = ws.snapshot()
    assert [i.code for i in snap.issues if i.severity == "error"] == ["shared-case"]  # what model_test fails on
    assert snap.matrix.attribution is not None
    assert snap.matrix.status("REQ-1") == INVALID and snap.matrix.status("REQ-2") == INVALID


def test_P27_source_annotations_are_documentation(tmp_path):
    refs = [
        Reference(("REQ-1",), "verifies", "tests/test_x.py", 3, symbol="def test_x"),
        Reference(("REQ-1", "REQ-2"), "verifies", "tests/test_y.py", 9),
    ]
    mx = build_matrix(model_at(tmp_path), evidence(tc("x", target="//t:x")), references=refs)
    assert [r.path for r in mx.verdicts["REQ-1"].verified_in] == ["tests/test_x.py", "tests/test_y.py"]
    assert mx.verdicts["REQ-1"].members == () and mx.status("REQ-1") == UNVERIFIED and owners(mx.attribution) == {}


def test_P28_configuration_cannot_switch_the_rule_off(tmp_path):
    for name in ("shared-case", "same-code-multiple-owners", "lock-owner-changed"):
        errors = []
        parse_config({"rules": {name: "off"}}, errors)
        assert errors and "always an error" in errors[0], name
    for name in ("multi-tag", "attribution-conflict"):
        errors = []
        parse_config({"rules": {name: "off"}}, errors)
        assert errors and "not a rule" in errors[0], name
    assert Config(rules={"shared-case": "off"}).rule("shared-case") == "error"
    # Every configurable rule off: attribution quarantines all the same.
    rules = "{" + ", ".join(f"{name}: 'off'" for name in DEFAULT_RULES) + "}"
    reqs = (
        "  - {id: REQ-1, title: a, satisfies: [UN-1], verified_by: [{target: //w:t, cases: ['*']}]}\n"
        "  - {id: REQ-2, title: b, satisfies: [UN-1], verified_by: [{target: //w:t, cases: ['c::a']}]}\n"
        "  - {id: REQ-3, title: c, satisfies: [UN-1]}\n"
    )
    model = model_at(tmp_path, reqs, config=f"{{rules: {rules}}}")
    ev = evidence(tc("a", target="//w:t"), tc("m", target="//x:t", declared=("REQ-2", "REQ-3")))
    mx = build_matrix(model, ev)
    assert quarantines(mx.attribution) == {
        "//w:t#c::a": (ATTRIBUTION_CONFLICT, ("REQ-1", "REQ-2")),
        "//x:t#c::m": (MULTI_TAG, ("REQ-2", "REQ-3")),
    }
    # --strict only escalates warnings; the hard errors stay errors either way.
    assert {i.severity for i in validate(model, strict=True) if i.code == "shared-case"} == {"error"}


def test_P29_python_api_cannot_store_an_owner(tmp_path):
    with pytest.deprecated_call():
        case = TestCase("t", "passed", classname="c", target="//a:t", requirements=("REQ-1", "REQ-2"))
    assert case.declared == ("REQ-1", "REQ-2") and not hasattr(case, "owner")
    hand_built = Evidence(cases=[case])  # not even through Evidence.add
    mx = build_matrix(model_at(tmp_path), hand_built)
    att = mx.attribution
    assert quarantines(att) == {"//a:t#c::t": (MULTI_TAG, ("REQ-1", "REQ-2"))}
    with pytest.raises(TypeError):
        att.owner[CaseKey("//a:t", "c::t")] = "REQ-1"  # read-only
    with pytest.raises(dataclasses.FrozenInstanceError):
        att.owner = {}  # type: ignore[misc]
    with pytest.raises(TypeError):
        att.members["REQ-1"] = ()  # type: ignore[index]
    params = inspect.signature(build_matrix).parameters
    assert list(params)[:4] == ["model", "evidence", "current_build", "references"]
    assert params["lock"].kind is inspect.Parameter.KEYWORD_ONLY
    # build_matrix recomputes every time: a later edit of the evidence changes nothing stored.
    case.declared = ("REQ-1",)
    assert owners(build_matrix(model_at(tmp_path), hand_built).attribution) == {"//a:t#c::t": "REQ-1"}


def test_P29_one_id_in_two_sections_is_refused(tmp_path):
    """A requirement X-1 and a mitigation X-1 would share one verification set
    (the requirement's verdict overwritten): validate() errors, attribute()
    refuses, and Model.with_entity cannot build it."""
    config = '{id_pattern: "(?:{prefix}|X)-\\\\d+"}'
    reqs = "  - {id: X-1, title: req, satisfies: [UN-1], verified_by: [{target: //p:t, cases: ['c::a']}]}\n"
    extra = (
        "risks: [{id: RISK-1, title: k, severity: low, likelihood: rare}]\n"
        "mitigations: [{id: MIT-1, title: m, mitigates: [RISK-1], implemented_by: [X-1]}]\n"
    )
    model = model_at(tmp_path, reqs, config=config, extra=extra)
    assert not [i for i in validate(model) if i.severity == "error"]
    mitigation = Mitigation(
        "X-1",
        "mit",
        mitigates=("RISK-1",),
        implemented_by=("X-1",),
        verified_by=(VerifiedBy("//p:t", cases=("c::b",)),),
    )
    with pytest.raises(ValueError, match="X-1 is already a requirement; it cannot also be a mitigation"):
        model.with_entity(mitigation)
    both = dataclasses.replace(model, mitigations={**model.mitigations, "X-1": mitigation})
    (dup,) = static(both, "duplicate-id")
    assert dup.severity == "error" and "X-1 is defined as a requirement and a mitigation" in dup.message
    ev = evidence(tc("a"), tc("b", "failed"))
    for call in (lambda: attribute(both, ev), lambda: build_matrix(both, ev)):
        with pytest.raises(ValueError, match="X-1 names entities in two sections"):
            call()
    # Replacing an entity in its own section is still fine.
    assert (
        model.with_entity(dataclasses.replace(model.requirements["X-1"], title="new")).requirements["X-1"].title
        == "new"
    )


def _trace_reads(name):
    """Where trace.py touches ``name`` (an attribute or a name), except the
    model's own ``requirements`` section."""
    tree = ast.parse(inspect.getsource(trace))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == name:
            base = node.value
            if name == "requirements" and (
                (isinstance(base, ast.Name) and base.id in ("model", "m"))
                or (isinstance(base, ast.Attribute) and base.attr == "model")
            ):
                continue  # Model.requirements: the model's section, not a tag
            found.append(node.lineno)
        elif isinstance(node, ast.Name) and node.id == name:
            found.append(node.lineno)
    return found


def test_P30_no_code_path_in_trace_reads_tags(tmp_path, monkeypatch):
    # trace.py reads nothing but the attribution: no tag, no raw target status.
    for name in ("declared", "requirements", "for_id", "target_status", "suite_declared", "TestCase"):
        assert _trace_reads(name) == [], name
    with pytest.deprecated_call(match="members_of"):
        evidence(tc("t", declared=("REQ-1",))).for_id("REQ-1")
    # build_matrix checks the invariant of the attribution it is handed.
    model = model_at(tmp_path)
    real = rr_attribution.attribute(model, evidence(tc("t", declared=("REQ-1",))))
    broken = dataclasses.replace(real, owner=MappingProxyType({CaseKey("//p:t", "c::t"): "REQ-2"}))
    monkeypatch.setattr(trace, "attribute", lambda *a, **kw: broken)
    with pytest.raises(AttributionInvariantError, match="owned by REQ-2"):
        build_matrix(model, Evidence())


def test_P31_agents_read_members_not_tags(tmp_path):
    from rules_requirements.agents import workflows
    from rules_requirements.agents.workflows import Context, linked_tests

    write(tmp_path, "tests/test_a.py", "def test_owned():\n    pass\n\n\ndef test_multi():\n    pass\n")
    model = model_at(tmp_path)
    ev = evidence(
        tc("test_owned", classname="tests.test_a", target="//t:a", declared=("REQ-1",)),
        tc("test_multi", classname="tests.test_a", target="//t:a", declared=("REQ-1", "REQ-2")),
    )
    ctx = Context(model=model, matrix=build_matrix(model, ev), root=str(tmp_path), references=None, issues=[])
    labels = [label for label, _ in linked_tests(ctx, "REQ-1")]
    assert labels == ["//t:a#tests.test_a::test_owned (tests/test_a.py:1, last result: passed)"]
    assert linked_tests(ctx, "REQ-2") == []  # the quarantined case is no test of either
    assert "for_id" not in inspect.getsource(workflows)


# --------------------------------------------------------------------------- #
# The fuzz: 10,000 seeded random models and evidence                          #
# --------------------------------------------------------------------------- #

FUZZ_SEED = 20261004
FUZZ_TRIALS = 10_000
_ENTS = ("REQ-1", "REQ-2", "REQ-3", "UN-1", "MIT-1")
_NON_OWNERS = ("RISK-1", "TM-1", "REQ-99")
_PATTERNS = (None, "*", "m::*", "m::t*", "m::t1", "m::t2", "n::*", "*::t1", "m::t[*]", "x", "m::t\\*", "*u")
_PATHS = ("m::t1", "m::t2", "m::t[a]", "n::t1", "n::u", "x", "m::t*")
_TARGETS = ("//a:t", "//b:t", "//c:t")
_FILES = ("", "", "f.py", "g.py")


def _fuzz_model(rnd):
    claims = {e: [] for e in _ENTS}
    for _ in range(rnd.randint(0, 6)):
        ent, target, pattern = rnd.choice(_ENTS), rnd.choice(_TARGETS), rnd.choice(_PATTERNS)
        level = rnd.choice(["", "", "sil", "hil"])
        item = (
            VerifiedBy(target, whole=True, reason="r", level=level)
            if pattern is None
            else VerifiedBy(target, cases=(pattern,), level=level)
        )
        claims[ent].append(item)
    refines = ("REQ-1",) if rnd.random() < 0.3 else ()
    reqs = {
        "REQ-1": Requirement("REQ-1", "a", satisfies=("UN-1",), verified_by=tuple(claims["REQ-1"])),
        "REQ-2": Requirement("REQ-2", "b", satisfies=("UN-1",), verified_by=tuple(claims["REQ-2"])),
        "REQ-3": Requirement(
            "REQ-3", "c", refines=refines, method=rnd.choice(["", "hil"]), verified_by=tuple(claims["REQ-3"])
        ),
    }
    config = Config(
        attribution=rnd.choice(["model", "hybrid"]),
        variants=(("//a:t", "//b:t"),) if rnd.random() < 0.3 else (),
        flaky=rnd.choice(["accept", "flag", "under-verify", "fail"]),
        set_consistency=rnd.choice(["off", "warn", "enforce"]),
    )
    return Model(
        config=config,
        user_needs={"UN-1": UserNeed("UN-1", "n", validated_by=tuple(claims["UN-1"]))},
        requirements=reqs,
        mitigations={
            "MIT-1": Mitigation(
                "MIT-1", "m", mitigates=("RISK-1",), implemented_by=("REQ-2",), verified_by=tuple(claims["MIT-1"])
            )
        },
        risks={"RISK-1": Risk("RISK-1", "k")},
        test_methods={"TM-1": rr_model.TestMethod("TM-1", "t", level="hil")},
    )


def _fuzz_evidence(rnd):
    ev = Evidence()
    for target in _TARGETS:
        if rnd.random() < 0.15:
            continue  # the target did not run
        base = "bazel-testlogs/" + target[2:].replace(":", "/")
        if rnd.random() < 0.15:  # Bazel's synthetic result only
            declared = (rnd.choice(_ENTS),) if rnd.random() < 0.2 else ()
            ev.add(
                TestCase(target[4:], rnd.choice(["passed", "error"]), target=target, source=f"{base}/test.xml",
                         declared=declared, properties={"rr.synthetic": "true"})
            )  # fmt: skip
            continue
        for path in rnd.sample(_PATHS, rnd.randint(0, len(_PATHS))):
            classname, _, name = path.rpartition("::")
            declared = tuple(rnd.sample(_ENTS + _NON_OWNERS, rnd.choice([0, 0, 0, 1, 1, 2])))
            props = {"rr.file": rnd.choice(_FILES)}
            artifact = {"sha": rnd.choice(["new", "new", "old"])} if rnd.random() < 0.2 else {}
            status = rnd.choice(["passed"] * 6 + ["failed", "skipped", "error"])
            level = rnd.choice(["", "", "simulation", "hil"])
            ev.add(TestCase(name, status, classname, declared, level, artifact, source=f"{base}/test.xml",
                            target=target, properties=props))  # fmt: skip
            if rnd.random() < 0.1:  # an earlier attempt failed
                ev.add(TestCase(name, "failed", classname, declared, source=f"{base}/test_attempts/attempt_1.xml",
                                target=target, properties=props))  # fmt: skip
            if rnd.random() < 0.05:  # the same key again in this run
                ev.add(TestCase(name, "passed", classname, declared, source=f"{base}/test.xml", target=target))
        if rnd.random() < 0.08:
            ev.add(TestCase("exit-status", "error", source=f"{base}/test.xml", target=target,
                            properties={"rr.scope": "target"}))  # fmt: skip
    return ev


def _fuzz_lock(rnd, ev):
    if rnd.random() >= 0.3:
        return None
    keys = sorted({(c.target, f"{c.classname}::{c.name}" if c.classname else c.name) for c in ev.cases})
    keys += [(rnd.choice(_TARGETS), rnd.choice(_PATHS))]
    chosen = dict.fromkeys(rnd.sample(keys, min(len(keys), rnd.randint(1, 3))))
    return rr_lock.Lock(tuple(rr_lock.LockEntry(t, p, rnd.choice(_ENTS)) for t, p in chosen))


def _pairs(issues_, code):
    """Entity pairs a static check reports (the message names both entities)."""
    return [i.message for i in issues_ if i.code == code]


def test_fuzz_one_owner_invariant_static_witness_and_invalid():
    """10,000 seeded trials (stdlib random): the owner is a function and the
    owned members partition the owned keys; every report-time
    attribution-conflict has a static shared-case witness (and every
    variants same-code quarantine of claimed keys a static
    same-code-multiple-owners one); every entity a quarantine names reads
    INVALID, and nothing else does."""
    rnd = random.Random(FUZZ_SEED)
    seen = {"quarantines": 0, "conflicts": 0, "multi-tag": 0, "same-code": 0, "owned": 0, INVALID: 0}
    for trial in range(FUZZ_TRIALS):
        model = _fuzz_model(rnd)
        ev = _fuzz_evidence(rnd)
        lock = _fuzz_lock(rnd, ev)
        current = rnd.choice([None, {"sha": "new"}])
        mx = build_matrix(model, ev, current_build=current, lock=lock)
        att = mx.attribution
        att.check_invariant()
        counted_once(mx)
        assert not set(att.owner) & {q.key for q in att.quarantined}, trial
        static_issues = None
        for q in att.quarantined:
            seen["quarantines"] += 1
            for ent in q.entities:
                assert mx.status(ent) == INVALID, (trial, q, mx.status(ent))
            if q.code == ATTRIBUTION_CONFLICT:
                seen["conflicts"] += 1
                static_issues = static_issues if static_issues is not None else validate(model)
                for a in q.entities:
                    for b in q.entities:
                        if a < b:
                            assert any(a in m and b in m for m in _pairs(static_issues, "shared-case")), (trial, q)
            elif q.code == MULTI_TAG:
                seen["multi-tag"] += 1
                # Rule (a) wins over (b): every declared verifiable id is named.
                assert {d for d in q.declared if d in _ENTS} <= set(q.entities), (trial, q)
            else:
                seen["same-code"] += 1
        for ent in att.entities:
            quarantined = any(m.state == "quarantined" for m in att.members_of(ent))
            assert (mx.status(ent) == INVALID) == quarantined, (trial, ent)
            seen[INVALID] += quarantined
            if mx.status(ent) == VERIFIED:
                assert all(m.state == "passed" for m in att.members_of(ent)), (trial, ent)
        seen["owned"] += len(att.owner)
    # The generator reaches every kind of outcome.
    assert all(n > 50 for n in seen.values()), seen


# --------------------------------------------------------------------------- #
# Unit behaviour                                                              #
# --------------------------------------------------------------------------- #


def test_resolve_cases_normalizes_targets_and_collects_taint(tmp_path):
    ev = evidence(
        tc("a", target="@@//p:t"),
        tc("b", target="//p:t"),
        tc("exit-status", "error", target="//p:t", properties={"rr.scope": "target"}),
        tc("ok-scope", target="//p:t", properties={"rr.scope": "target"}),  # a passing target-scope result: nothing
        tc("syn", target="@splanc//q", properties={"rr.synthetic": "true"}),
        tc("u", target=""),
    )
    cases, targets, found = resolve_cases(ev, Config(main_repo="splanc"))
    assert sorted(map(str, cases)) == ["//p:t#c::a", "//p:t#c::b", "//q:q#[target]", "suite:unnamed#c::u"]
    assert [c.name for c in targets["//p:t"].taint] == ["exit-status"] and not targets["//q:q"].taint
    assert targets["//q:q"].synthetic_only and not targets["//p:t"].synthetic_only
    assert [i.code for i in found] == ["unscoped-evidence"]


def test_whole_claims_take_the_synthetic_result_only_when_alone(tmp_path):
    reqs = (
        "  - {id: REQ-1, title: a, satisfies: [UN-1], verified_by: [{target: //w:syn, whole: true, reason: r}]}\n"
        "  - {id: REQ-2, title: b, satisfies: [UN-1], verified_by: [{target: //w:syn2, cases: ['*']}]}\n"
        "  - {id: REQ-3, title: c, satisfies: [UN-1], verified_by: [{target: //w:syn3, cases: ['a']}]}\n"
        "  - {id: REQ-4, title: d, satisfies: [UN-1], verified_by: [{target: //w:cases, whole: true, reason: r}]}\n"
    )
    ev = evidence(
        tc("syn", target="//w:syn", classname="", properties={"rr.synthetic": "true"}),
        tc("syn2", target="//w:syn2", classname="", properties={"rr.synthetic": "true"}),
        tc("syn3", "failed", target="//w:syn3", classname="", properties={"rr.synthetic": "true"}),
        tc("x", target="//w:cases"),
        tc("y", target="//w:cases"),
    )
    mx = build_matrix(model_at(tmp_path, reqs), ev)
    att = mx.attribution
    assert owners(att) == {"//w:syn#[target]": "REQ-1", "//w:cases#c::x": "REQ-4", "//w:cases#c::y": "REQ-4"}
    # A selector never matches a synthetic result: a passed one means missing, a failed one error.
    assert states(att, "REQ-2") == [("//w:syn2 *", "missing")] and mx.status("REQ-2") == INCOMPLETE
    assert states(att, "REQ-3") == [("//w:syn3#a", "error")] and mx.status("REQ-3") == FAILED
    assert [i.entities for i in issues(att, "coarse-claim")] == [("REQ-4",)]


def test_a_tag_owns_a_synthetic_result_only_when_it_is_all_its_target_reported(tmp_path):
    ev = evidence(
        tc("lone", target="//w:lone", classname="", declared=("REQ-1",), properties={"rr.synthetic": "true"}),
        tc("mixed", target="//w:mixed", classname="", declared=("REQ-2",), properties={"rr.synthetic": "true"}),
        tc("case", target="//w:mixed", declared=("REQ-3",)),
    )
    att = build_matrix(model_at(tmp_path), ev).attribution
    assert owners(att) == {"//w:lone#[target]": "REQ-1", "//w:mixed#c::case": "REQ-3"}


def test_expected_members_missing_not_run_and_the_nearest_case(tmp_path):
    reqs = (
        "  - {id: REQ-1, title: a, satisfies: [UN-1], verified_by: [{target: //w:t, cases: ['c::offset', 'c::drift']}, "
        "{target: //w:never, cases: ['*']}]}\n"
    )
    mx = build_matrix(model_at(tmp_path, reqs), evidence(tc("offsets", target="//w:t"), tc("drift", target="//w:t")))
    att = mx.attribution
    assert states(att, "REQ-1") == [
        ("//w:never *", "not-run"),
        ("//w:t#c::drift", "passed"),
        ("//w:t#c::offset", "missing"),
    ]
    assert mx.status("REQ-1") == INCOMPLETE
    (missing,) = [g for g in mx.gaps if g.kind == "missing-case"]
    assert "selector 'c::offset' of //w:t matched no case" in missing.message and "'c::offsets'" in missing.message
    (incomplete,) = [g for g in mx.gaps if g.kind == "incomplete"]
    assert incomplete.message == "1/3 passed; 1 missing (//w:t); 1 not run (//w:never)"


def test_member_levels_and_level_mismatch(tmp_path):
    reqs = (
        "  - {id: REQ-1, title: a, satisfies: [UN-1], method: hil, verified_by: "
        "[{target: //w:t, cases: ['c::*'], level: hil}]}\n"
    )
    ev = evidence(tc("own", target="//w:t", level="simulation"), tc("none", target="//w:t"))
    mx = build_matrix(model_at(tmp_path, reqs), ev)
    att = mx.attribution
    assert {m.key.path: m.level for m in att.members_of("REQ-1")} == {"c::own": "simulation", "c::none": "hil"}
    (mismatch,) = issues(att, "level-mismatch")
    assert "provides simulation but REQ-1's claim says hil; counted as simulation" in mismatch.message
    assert mx.status("REQ-1") == VERIFIED and mx.verdicts["REQ-1"].provided == "hil"


@pytest.mark.parametrize("own, claimed", [("simulation", "inspection"), ("inspection", "hil")])
def test_level_mismatch_with_an_unordered_level_counts_the_cases_own(tmp_path, own, claimed):
    """An unordered level has no rank: on a mismatch the case's own level counts,
    so a simulation test does not meet an inspection demand because its
    selector says inspection (nor an inspection record a hil one)."""
    reqs = (
        f"  - {{id: REQ-1, title: a, satisfies: [UN-1], method: {claimed}, verified_by: "
        f"[{{target: //p:t, cases: ['c::a'], level: {claimed}}}]}}\n"
    )
    mx = build_matrix(model_at(tmp_path, reqs), evidence(tc("a", level=own)))
    (member,) = mx.attribution.members_of("REQ-1")
    assert member.level == own and mx.status("REQ-1") == UNDER_VERIFIED
    (mismatch,) = issues(mx.attribution, "level-mismatch")
    assert mismatch.message.endswith(f"counted as {own}")


def test_multi_tag_wins_over_attribution_conflict(tmp_path):
    """A case that declares REQ-3 and REQ-4 and that REQ-1 and REQ-2 both claim is
    a multi-tag (rule a before b): it names all four, and all four read INVALID."""
    reqs = REQS.replace("{id: REQ-1, title: r1, satisfies: [UN-1]}", "{id: REQ-1, title: r1, satisfies: [UN-1], "
                        "verified_by: [{target: //w:t, cases: ['c::*']}]}").replace(
        "{id: REQ-2, title: r2, satisfies: [UN-1]}", "{id: REQ-2, title: r2, satisfies: [UN-1], "
        "verified_by: [{target: //w:t, cases: ['c::m']}]}")  # fmt: skip
    mx = build_matrix(model_at(tmp_path, reqs), evidence(tc("m", target="//w:t", declared=("REQ-3", "REQ-4"))))
    att = mx.attribution
    assert quarantines(att) == {"//w:t#c::m": (MULTI_TAG, ("REQ-1", "REQ-2", "REQ-3", "REQ-4"))}
    assert [mx.status(f"REQ-{i}") for i in (1, 2, 3, 4)] == [INVALID] * 4
    assert {m.via for m in att.members_of("REQ-3")} == {"tag"} and {m.via for m in att.members_of("REQ-1")} == {"model"}


def test_unlocked_member_is_one_issue_and_one_gap_per_unlocked_key(tmp_path):
    reqs = "  - {id: REQ-1, title: a, satisfies: [UN-1], verified_by: [{target: //w:t, cases: ['c::*']}]}\n"
    lock = rr_lock.Lock((rr_lock.LockEntry("//w:t", "c::a", "REQ-1"),), "verification.rrlock")
    mx = build_matrix(model_at(tmp_path, reqs), evidence(tc("a", target="//w:t"), tc("b", target="//w:t")), lock=lock)
    (unlocked,) = issues(mx.attribution, "unlocked-member")
    assert unlocked.key == CaseKey("//w:t", "c::b") and unlocked.entities == ("REQ-1",)
    assert "not in the lock verification.rrlock" in unlocked.message
    assert [(g.kind, g.entity) for g in mx.gaps if g.kind == "unlocked-member"] == [("unlocked-member", "REQ-1")]
    assert mx.status("REQ-1") == VERIFIED  # an addition is reviewed, not a failure


@pytest.mark.parametrize("mode", ["model", "hybrid"])
def test_report_time_lock_stale(tmp_path, mode):
    """attribution: model — a lock entry no claim of its owner selects is lock-stale
    at report time too (not only statically); hybrid mode tolerates it (tag-owned)."""
    reqs = "  - {id: REQ-1, title: a, satisfies: [UN-1], verified_by: [{target: //w:t, cases: ['c::a']}]}\n"
    lock = rr_lock.Lock(
        (rr_lock.LockEntry("//w:t", "c::a", "REQ-1"), rr_lock.LockEntry("//w:t", "c::old", "REQ-1")),
        "verification.rrlock",
    )
    mx = build_matrix(model_at(tmp_path, reqs, config=f"{{attribution: {mode}}}"), evidence(tc("a", target="//w:t")),
                      lock=lock)  # fmt: skip
    stale = issues(mx.attribution, "lock-stale")
    if mode == "hybrid":
        assert stale == []
        return
    (only,) = stale
    assert only.key == CaseKey("//w:t", "c::old") and only.entities == ("REQ-1",) and only.severity == "error"
    assert ("lock-stale", "REQ-1") in {(g.kind, g.entity) for g in mx.gaps}


def test_multi_tag_names_only_verifiable_ids_and_claimants(tmp_path):
    reqs = "  - {id: REQ-1, title: a, satisfies: [UN-1], verified_by: [{target: //w:t, cases: ['*']}]}\n"
    extra = "risks: [{id: RISK-1, title: k}]\n"
    ev = evidence(tc("m", target="//w:t", declared=("RISK-1", "REQ-99", "UN-1")))
    att = build_matrix(model_at(tmp_path, reqs, extra=extra), ev).attribution
    (q,) = att.quarantined
    assert q.code == MULTI_TAG and q.entities == ("REQ-1", "UN-1") and q.declared == ("RISK-1", "REQ-99", "UN-1")
    assert [i.code for i in att.issues if i.key == q.key] == ["misdirected-evidence", "unknown-id"]


def test_check_invariant_catches_a_hand_built_attribution(tmp_path):
    model = model_at(tmp_path)
    att = attribute(model, evidence(tc("a", declared=("REQ-1",)), tc("b", declared=("REQ-1", "REQ-2"))))
    att.check_invariant()
    key = CaseKey("//p:t", "c::a")
    member = att.members_of("REQ-1")[0]
    two = dict(att.members)
    two["REQ-2"] = (*att.members["REQ-2"], dataclasses.replace(member, entity="REQ-2"))
    cases = [
        (dataclasses.replace(att, members=MappingProxyType(two)), "owned member of REQ-1 and of REQ-2"),
        (dataclasses.replace(att, owner=MappingProxyType({key: ("REQ-1", "REQ-2")})), "not one verifiable"),
        (
            dataclasses.replace(att, quarantined=(*att.quarantined, Quarantine(key, MULTI_TAG, ("REQ-3",), "x"))),
            "quarantined (multi-tag) and owned",
        ),
        (
            dataclasses.replace(att, members=MappingProxyType({**att.members, "REQ-2": ()})),
            "names REQ-2, which has no quarantined member",
        ),
        (
            dataclasses.replace(att, members=MappingProxyType({**att.members, "REQ-1": ()})),
            "owned by REQ-1 but no member of it",
        ),
    ]
    for broken, expected in cases:
        with pytest.raises(AttributionInvariantError, match=re.escape(expected)):
            broken.check_invariant()


@pytest.mark.parametrize("state", ["passed", "failed", "skipped", "error"])
@pytest.mark.parametrize("with_result", [False, True])
def test_check_invariant_reads_ownership_from_the_state(tmp_path, state, with_result):
    """A forged member that counts toward REQ-2 (by its state) for a case REQ-1
    owns breaks the invariant, with or without a result: verdict_from_members
    reads only the state, so REQ-2 would read VERIFIED (or FAILED) by it."""
    reqs = "  - {id: REQ-1, title: a, satisfies: [UN-1], verified_by: [{target: //p:t, cases: ['c::a']}]}\n"
    model = model_at(tmp_path, reqs)
    att = attribute(model, evidence(tc("a")))
    key = CaseKey("//p:t", "c::a")
    result = att.cases[key] if with_result else None
    forged = Member("REQ-2", key, "c::a", "model", state, "simulation", target="//p:t", result=result)
    broken = dataclasses.replace(att, members=MappingProxyType({**att.members, "REQ-2": (forged,)}))
    with pytest.raises(AttributionInvariantError, match="is a member of REQ-2 but owned by REQ-1"):
        broken.check_invariant()
    if not with_result:
        with pytest.raises(AttributionInvariantError, match=r"owned \(\w+\) member of REQ-2 without its result"):
            broken.check_invariant()
    # Pseudo-members of an absent case stay exempt.
    absent = Member("REQ-2", CaseKey("//p:t", "c::gone"), "c::gone", "model", "error", target="//p:t")
    dataclasses.replace(att, members=MappingProxyType({**att.members, "REQ-2": (absent,)})).check_invariant()


def test_attribution_reads(tmp_path):
    reqs = "  - {id: REQ-1, title: a, satisfies: [UN-1], verified_by: [{target: //w:t, cases: ['*']}]}\n"
    att = build_matrix(model_at(tmp_path, reqs), evidence(tc("a", target="//w:t"))).attribution
    key = CaseKey("//w:t", "c::a")
    assert att.owner_of(key) == "REQ-1" and att.quarantine_of(key) is None and att.members_of("NOPE-1") == ()
    assert att.entities == ("UN-1", "REQ-1")
    assert [c.entity for c in att.claimed_by[key]] == ["REQ-1"]
    (member,) = att.members_of("REQ-1")
    assert isinstance(member, Member) and member.to_dict() == {
        "case": "//w:t#c::a",
        "target": "//w:t",
        "selector": "*",
        "via": "model",
        "state": "passed",
        "level": "simulation",
    }
    assert att.cases[key].to_dict()["case"] == "//w:t#c::a"
    assert isinstance(att, Attribution) and att.mode == "hybrid"
