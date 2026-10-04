# SPDX-License-Identifier: AGPL-3.0-or-later
import json
import os

import pytest
from conftest import junit, write

from rules_requirements import ingest
from rules_requirements.case_keys import CaseKey, index_cases, is_target_scope, key_of
from rules_requirements.ingest import IngestIssue, Ingestor, TestCase, apply_properties, split_ids
from rules_requirements.ingest.junit import JUnitIngestor, target_from_path
from rules_requirements.ingest.libtest import merge_trace, parse_libtest


@pytest.mark.parametrize(
    "path, label",
    [
        ("/x/bazel-testlogs/pkg/sub/name/test.xml", "//pkg/sub:name"),
        ("/x/bazel-out/k8-fastbuild/testlogs/pkg/name/test.xml", "//pkg:name"),
        ("/x/bazel-testlogs/name/test.xml", "//:name"),
        ("/x/bazel-testlogs/pkg/name/shard_2_of_4/test.xml", "//pkg:name"),
        ("/x/bazel-testlogs/pkg/name/run_1_of_3/test.xml", "//pkg:name"),
        ("/x/bazel-testlogs/external/repo+/pkg/name/test.xml", "@repo+//pkg:name"),
        ("/x/reports/results.xml", ""),
        ("C:\\x\\bazel-testlogs\\pkg\\name\\test.xml", "//pkg:name"),
        # Targets whose names merely start like a run level are targets.
        ("bazel-testlogs/pkg/run_tests/test.xml", "//pkg:run_tests"),
        ("bazel-testlogs/tools/shard_test/test.xml", "//tools:shard_test"),
        ("bazel-testlogs/pkg/attempt_test/test.xml", "//pkg:attempt_test"),
        ("bazel-testlogs/run_all/test.xml", "//:run_all"),
        ("bazel-testlogs/web/run_smoke_test/test_attempts/attempt_1.xml", "//web:run_smoke_test"),
        # A package with a testlogs directory: the leftmost testlogs root wins.
        ("bazel-out/k8-fastbuild/testlogs/x/testlogs/y_test/test.xml", "//x/testlogs:y_test"),
        ("/x/bazel-testlogs/x/testlogs/y_test/shard_1_of_2_run_1_of_2/test.xml", "//x/testlogs:y_test"),
        # rr_evidence's output tree (<name>/testlogs) and a copy of it.
        ("bazel-out/k8-fastbuild/bin/r/ev/testlogs/hitl/flash_test/test.xml", "//hitl:flash_test"),
        ("ev/testlogs/hitl/flash_test/test.xml", "//hitl:flash_test"),
        # An ancestor directory merely named testlogs, above a real rr_evidence
        # testlogs tree and with no recognised bazel root: the innermost
        # testlogs wins (origin/main's behaviour), not the leftmost one.
        ("/tmp/testlogs/proj/bazel-bin/ev/testlogs/hitl/flash_test/test.xml", "//hitl:flash_test"),
        ("bazel-out/k8-fastbuild/bin/x/testlogs/ev/testlogs/hitl/flash_test/test.xml", "//hitl:flash_test"),
    ],
)
def test_target_from_path(path, label):
    assert target_from_path(path) == label


def test_junit_dialects(tmp_path):
    xml = """<?xml version="1.0"?>
    <testsuite name="top">
      <properties><property name="level" value="hil"/><property name="artifact.fw" value="1.2"/></properties>
      <testcase classname="c" name="a" time="0.5">
        <properties>
          <property name="requirement" value="REQ-1"/>
          <property name="requirements" value="REQ-2, REQ-3,REQ-1"/>
          <property name="owner" value="qa"/>
        </properties>
      </testcase>
      <testcase classname="c" name="b" requirements="REQ-4" level="sil" custom="x" time="bad"/>
      <testcase classname="c" name="c" status="notrun"/>
      <testcase classname="c" name="d"><failure>long text</failure></testcase>
      <testcase classname="c" name="e"><error message="kaput"/></testcase>
      <testsuite name="nested"><testcase name="f"><skipped/></testcase></testsuite>
    </testsuite>"""
    path = write(tmp_path, "bazel-testlogs/pkg/t/test.xml", xml)
    cases = {c.name: c for c in JUnitIngestor().ingest(path)}
    a = cases["a"]
    assert a.declared == ("REQ-1", "REQ-2", "REQ-3")  # every id: a multi-tag case
    assert a.level == "hil" and a.artifact == {"fw": "1.2"}
    assert a.properties == {"owner": "qa"}
    assert a.duration == 0.5 and a.target == "//pkg:t"
    assert a.full_name == "//pkg:t c::a"
    b = cases["b"]
    assert b.declared == ("REQ-4",) and b.level == "sil" and b.properties == {"custom": "x"}
    assert b.duration == 0.0
    assert cases["c"].status == "skipped"
    assert cases["d"].status == "failed" and cases["d"].message == "long text" and cases["d"].is_failure
    assert cases["e"].status == "error" and cases["e"].message == "kaput"
    assert cases["f"].status == "skipped" and cases["f"].full_name == "//pkg:t f"


def test_junit_unparseable_reports_a_target_scope_error(tmp_path):
    (case,) = JUnitIngestor().ingest(write(tmp_path, "bazel-testlogs/p/t/test.xml", "<testsuite><oops"))
    assert case.status == "error" and case.target == "//p:t" and "unreadable" in case.message
    # About the whole run: it taints the target's members, it is no member itself.
    assert case.scope == "target" and is_target_scope(case) and case.declared == ()
    (row,) = index_cases([case]).values()
    assert row.target_scope and row.key == CaseKey("//p:t", "<unreadable>")


def test_testsuites_level_properties_and_attempts(tmp_path):
    xml = (
        '<testsuites><properties><property name="artifact.sha" value="old"/></properties>'
        '<testsuite name="s"><testcase name="a"/></testsuite></testsuites>'
    )
    (case,) = JUnitIngestor().ingest(write(tmp_path, "t.xml", xml))
    assert case.artifact == {"sha": "old"}
    assert target_from_path("/x/bazel-testlogs/pkg/name/test_attempts/attempt_1.xml") == "//pkg:name"


def test_target_with_skipped_cases_is_not_passing_evidence(tmp_path):
    # e.g. the DUT was absent: hardware cases skipped, one host-side case passed
    base = tmp_path / "bazel-testlogs"
    junit(base, "p/t/test.xml", [("sanity", "passed", [], "")] + [(f"hw{i}", "skipped", [], "") for i in range(20)])
    assert ingest.collect([str(base)]).target_status == {"//p:t": "skipped"}


def test_collect_walks_dirs_and_skips_unknown(tmp_path):
    junit(tmp_path, "logs/a/t1/test.xml", [("x", "passed", ["REQ-1"], "")])
    junit(tmp_path, "logs/a/t2/test.xml", [("y", "failed", ["REQ-1"], ""), ("z", "passed", [], "")])
    write(tmp_path, "logs/a/t1/test.log", "running 1 test")
    write(tmp_path, "logs/other.xml", "<project/>")
    ev = ingest.collect([str(tmp_path / "logs"), "", str(tmp_path / "missing")])
    assert len(ev.files) == 2
    assert len(ev.skipped_files) == 2
    assert [c.name for c in ev.for_id("REQ-1")] == ["x", "y"]
    globbed = ingest.collect([str(tmp_path / "logs" / "**" / "test.xml")])
    assert len(globbed.cases) == 3
    assert next(ingest.iter_cases(globbed)).name == "x"


def test_target_status_worst_wins(tmp_path):
    base = tmp_path / "bazel-testlogs"
    junit(base, "p/t/test.xml", [("a", "passed", [], ""), ("b", "skipped", [], ""), ("c", "error", [], "")])
    ev = ingest.collect([str(base)])
    assert ev.target_status == {"//p:t": "error"}


LIBTEST = """
running 4 tests
test parse::rejects_empty ... ok
test parse::accepts_c ... FAILED
test slow::soak ... ignored, needs hardware
test top_level ... ok
test panics - should panic ... ok

failures:

---- parse::accepts_c stdout ----
thread 'parse::accepts_c' panicked at src/lib.rs:3:5:
assertion failed

failures:
    parse::accepts_c

test result: FAILED. 2 passed; 1 failed; 1 ignored
"""


def test_libtest_parse_and_trace():
    cases = {f"{c.classname}::{c.name}": c for c in parse_libtest(LIBTEST, target="//r:t")}
    assert cases["parse::rejects_empty"].status == "passed"
    failed = cases["parse::accepts_c"]
    assert failed.status == "failed" and "assertion failed" in failed.message
    assert cases["slow::soak"].status == "skipped" and cases["slow::soak"].message == "needs hardware"
    assert cases["::top_level"].classname == ""
    assert cases["::panics"].status == "passed"
    trace = "\n".join(
        [
            json.dumps({"test": "parse::rejects_empty", "requirements": ["REQ-1"], "level": "sil"}),
            json.dumps({"test": "top_level", "requirements": ["REQ-2"], "artifact": {"k": "v"}}),
            json.dumps({"test": "unknown::test", "requirements": ["REQ-9"]}),
            "not json",
            "",
        ]
    )
    merge_trace(list(cases.values()), trace)
    assert cases["parse::rejects_empty"].declared == ("REQ-1",)
    assert cases["parse::rejects_empty"].level == "sil"
    assert cases["::top_level"].artifact == {"k": "v"}


def test_libtest_ingestor_reads_sidecar(tmp_path):
    path = write(tmp_path, "run.libtest.txt", LIBTEST)
    write(tmp_path, "run.rrtrace.jsonl", json.dumps({"test": "top_level", "requirements": ["REQ-7"]}) + "\n")
    ev = ingest.collect([path])
    assert [c.name for c in ev.for_id("REQ-7")] == ["top_level"]
    alone = write(tmp_path, "solo/x.libtest", LIBTEST)
    assert len(ingest.collect([alone]).cases) == 5


def test_records_ingestor(tmp_path):
    yaml_path = write(
        tmp_path,
        "signoff.rr.yaml",
        """
        evidence:
          - name: label-legible
            requirements: REQ-12
            level: inspection
            artifact: {board_rev: C}
            properties: {signed_by: J. Doe}
          - name: weird
            status: exploded
          - name: planned-not-signed
            requirements: [REQ-13]
          - not-a-mapping
        """,
    )
    json_path = write(
        tmp_path, "bench.rr.json", json.dumps([{"requirements": ["REQ-1"], "status": "failed", "duration": 2}])
    )
    ev = ingest.collect([yaml_path, json_path])
    by_name = {c.name: c for c in ev.cases}
    lbl = by_name["label-legible"]
    assert lbl.declared == ("REQ-12",) and lbl.level == "inspection"
    assert lbl.artifact == {"board_rev": "C"} and lbl.properties == {"signed_by": "J. Doe"}
    assert by_name["weird"].status == "error" and "no valid status" in by_name["weird"].message
    assert by_name["planned-not-signed"].status == "error"
    assert by_name["record-1"].status == "failed" and by_name["record-1"].duration == 2


class TapIngestor(Ingestor):
    name = "tap"
    suffixes = (".tap",)

    def ingest(self, path):
        with open(path) as fh:
            for line in fh:
                if line.startswith(("ok", "not ok")):
                    ok = line.startswith("ok")
                    name = line.split("-", 1)[1].split("#")[0].strip()
                    reqs = tuple(line.split("# rr:")[1].split()) if "# rr:" in line else ()
                    yield TestCase(name=name, status="passed" if ok else "failed", declared=reqs, source=path)


def test_custom_ingestor_registration(tmp_path):
    path = write(tmp_path, "r.tap", "TAP version 13\nok 1 - adds # rr: REQ-1\nnot ok 2 - subtracts\n")
    assert ingest.collect([path]).cases == []  # not registered yet
    ingest.register(TapIngestor())
    try:
        ev = ingest.collect([path])
        assert [(c.name, c.status) for c in ev.cases] == [("adds", "passed"), ("subtracts", "failed")]
        assert ev.for_id("REQ-1")[0].name == "adds"
        only = ingest.collect([path], only=["junit"])
        assert only.cases == []
    finally:
        ingest._REGISTRY.pop("tap", None)


def test_broken_entry_point_does_not_block_others(monkeypatch):
    class EP:
        def __init__(self, name, obj):
            self.name, self._obj = name, obj

        def load(self):
            if isinstance(self._obj, Exception):
                raise self._obj
            return self._obj

    class Good(Ingestor):
        name = "good-plugin"

    class EPS(list):
        def select(self, group):
            return self

    import importlib.metadata

    monkeypatch.setattr(
        importlib.metadata, "entry_points", lambda: EPS([EP("bad", ImportError("nope")), EP("good", Good)])
    )
    monkeypatch.setattr(ingest, "_BUILTINS_LOADED", False)
    monkeypatch.setattr(ingest, "PLUGIN_ERRORS", [])
    try:
        names = ingest.ingestors()
        assert "good-plugin" in names and "junit" in names
        assert ingest.PLUGIN_ERRORS == ["bad: ImportError: nope"]
    finally:
        ingest._REGISTRY.pop("good-plugin", None)


def test_load_ingestor_by_spec(tmp_path, monkeypatch):
    mod = tmp_path / "my_ingestors.py"
    mod.write_text(
        "from rules_requirements.ingest import Ingestor\n"
        "class Csv(Ingestor):\n    name = 'csv'\n    suffixes = ('.csv',)\n    def ingest(self, path):\n        return []\n"
        "INGESTOR = Csv()\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        assert ingest.load_ingestor("my_ingestors:Csv").name == "csv"
        assert ingest.load_ingestor("my_ingestors").name == "csv"
        assert "csv" in ingest.ingestors()
    finally:
        ingest._REGISTRY.pop("csv", None)


def test_register_requires_name_and_base_ingest_abstract(tmp_path):
    with pytest.raises(ValueError):
        ingest.register(Ingestor())
    with pytest.raises(NotImplementedError):
        Ingestor().ingest("x")
    assert Ingestor().sniff("x.xml", b"") is False
    assert ingest.ingestor_for(str(tmp_path / "nope.xml")) is None
    assert os.path.exists(tmp_path)


def test_libtest_nocapture_layout():
    text = (
        "running 2 tests\n"
        "test hw::flash_and_boot ... \n"
        "[boot] ESP-ROM:esp32c6\n"
        "[boot] app started\n"
        "ok\n"
        "test hw::soak ... flashing DUT 0x10000\n"
        "FAILED\n"
        "test result: FAILED. 1 passed; 1 failed\n"
    )
    cases = {f"{c.classname}::{c.name}": c.status for c in parse_libtest(text)}
    assert cases == {"hw::flash_and_boot": "passed", "hw::soak": "failed"}


def test_libtest_ignores_result_lines_in_captured_output():
    text = (
        "running 2 tests\n"
        "test ui::compile_fail ... FAILED\n"
        "test parse::ok ... ok\n\n"
        "failures:\n\n"
        "---- ui::compile_fail stdout ----\n"
        "test tests/ui/missing_field.rs ... ok\n"
        "test tests/ui/extra.rs ... mismatch\n\n"
        "failures:\n    ui::compile_fail\n\n"
        "test result: FAILED. 1 passed; 1 failed\n"
    )
    cases = {f"{c.classname}::{c.name}": c for c in parse_libtest(text)}
    assert sorted(cases) == ["parse::ok", "ui::compile_fail"]
    assert "missing_field.rs" in cases["ui::compile_fail"].message


def test_libtest_nocapture_last_verdict_and_failure_list_win():
    text = (
        "running 3 tests\n"
        "test hw::flash_and_boot ... \n"
        "ok\n"  # the test's own step output
        "thread 'hw::flash_and_boot' panicked at src/lib.rs:9:5\n"
        "FAILED\n"
        "test hw::other ... step 1\n"
        "ok\n"
        "test hw::silent ... \n"
        "ok\n"
        "\n"
        "failures:\n"
        "\n"
        "failures:\n"
        "    hw::flash_and_boot\n"
        "    hw::silent\n"
        "\n"
        "test result: FAILED. 1 passed; 2 failed\n"
    )
    cases = {f"{c.classname}::{c.name}": c.status for c in parse_libtest(text)}
    assert cases == {"hw::flash_and_boot": "failed", "hw::other": "passed", "hw::silent": "failed"}


# --- v0.3: ingest records declared ids, never owners -------------------------


def test_declared_is_a_field_and_requirements_a_deprecated_alias():
    # Positional construction is unchanged: the fourth field was `requirements`.
    case = TestCase("n", "passed", "cls", ("R-1",), "unit", {}, "", 0.5, "r.xml", "//a:b", {"k": "v"})
    assert case.declared == ("R-1",)
    with pytest.warns(DeprecationWarning, match="use TestCase.declared"):
        assert case.requirements == ("R-1",)
    with pytest.warns(DeprecationWarning, match="tags, not owners"):
        case.requirements = ["R-2", "R-3"]
    assert case.declared == ("R-2", "R-3")  # a list is stored as a tuple
    with pytest.warns(DeprecationWarning):
        legacy = TestCase("n", "passed", requirements=["R-4"])
    assert legacy.declared == ("R-4",)
    with pytest.raises(TypeError, match="not both"), pytest.warns(DeprecationWarning):
        TestCase("n", "passed", declared=("R-1",), requirements=("R-2",))
    # A bare string is one id, not its characters.
    assert TestCase("n", "passed", declared="R-5").declared == ("R-5",)
    # Ownership is nobody's field: a case has no owner to set.
    assert not any("owner" in name for name in vars(TestCase("n", "passed")))


def test_scope_and_synthetic_accessors():
    assert TestCase("n", "error", properties={"rr.scope": "TARGET"}).scope == "target"
    assert TestCase("n", "passed").scope == "case"
    assert TestCase("n", "passed", properties={"rr.synthetic": "true"}).synthetic
    assert not TestCase("n", "passed").synthetic


@pytest.mark.parametrize(
    ("value", "ids"),
    [
        ("REQ-1", ["REQ-1"]),
        ("REQ-1,REQ-2", ["REQ-1", "REQ-2"]),
        (" REQ-1 , REQ-2 ", ["REQ-1", "REQ-2"]),
        ("REQ-1 REQ-2", ["REQ-1", "REQ-2"]),  # whitespace separates ids too (v0.3)
        ("REQ-1\tREQ-2\nREQ-3", ["REQ-1", "REQ-2", "REQ-3"]),
        (",,", []),
        ("", []),
    ],
)
def test_split_ids_on_commas_and_whitespace(value, ids):
    assert split_ids(value) == ids


def test_apply_properties_gathers_every_id_in_order_without_deciding():
    case = TestCase("probe [rr:REQ-9]", "passed")
    apply_properties(
        case,
        [("requirement", "REQ-2"), ("requirements", "REQ-1 REQ-2"), ("requirement", ""), ("level", "HIL")],
    )
    # Distinct ids, in order, the name tag last: a multi-tag case, for
    # attribution to quarantine. Nothing here picks one of them.
    assert case.declared == ("REQ-2", "REQ-1", "REQ-9")
    assert case.level == "hil"
    apply_properties(case, [("requirement", "REQ-1")])  # idempotent: no duplicates
    assert case.declared == ("REQ-2", "REQ-1", "REQ-9")


@pytest.mark.parametrize(
    ("testcase", "declared"),
    [
        # Every way a JUnit producer can name more than one id (P11): all kept.
        (
            '<testcase name="t"><properties><property name="requirement" value="A-1"/>'
            '<property name="requirement" value="B-2"/></properties></testcase>',
            ("A-1", "B-2"),
        ),
        ('<testcase name="t" requirement="A-1,B-2"/>', ("A-1", "B-2")),
        ('<testcase name="t" requirements="A-1 B-2"/>', ("A-1", "B-2")),
        (
            '<testcase name="t"><properties><property name="requirements" value="A-1, B-2"/></properties></testcase>',
            ("A-1", "B-2"),
        ),
        ('<testcase name="t [rr:A-1] [rr:B-2]"/>', ("A-1", "B-2")),
        ('<testcase name="t [rr:A-1,B-2]"/>', ("A-1", "B-2")),
        (
            '<testcase name="t [rr:A-1]"><properties><property name="requirement" value="B-2"/></properties></testcase>',
            ("B-2", "A-1"),
        ),
        # One id however it is spelled.
        ('<testcase name="t [rr:A-1]"/>', ("A-1",)),
        (
            '<testcase name="t" requirement="A-1"><properties><property name="requirement" value="A-1"/>'
            "</properties></testcase>",
            ("A-1",),
        ),
        ('<testcase name="t"/>', ()),
    ],
)
def test_junit_declared_ids(tmp_path, testcase, declared):
    path = write(tmp_path, "bazel-testlogs/p/t/test.xml", f'<testsuite name="s">{testcase}</testsuite>')
    (case,) = JUnitIngestor().ingest(path)
    assert case.declared == declared
    # Re-tagging never renames a case: the key never carries the tags.
    assert key_of(case) == CaseKey("//p:t", "t")


def test_name_tags_are_declared_for_hand_built_cases_too():
    rows = index_cases([TestCase("probe [rr:PR-1] ok", "passed", target="//a:b")])
    (row,) = rows.values()
    assert row.key == CaseKey("//a:b", "probe ok") and row.declared == ("PR-1",)


def test_suite_level_requirements_are_not_inherited(tmp_path):
    xml = """<testsuites>
      <properties><property name="requirement" value="REQ-9"/><property name="artifact.sha" value="abc"/></properties>
      <testsuite name="Interlock">
        <properties>
          <property name="requirements" value="REQ-1,REQ-3"/>
          <property name="level" value="HIL"/>
          <property name="hostname" value="rig-2"/>
        </properties>
        <testcase classname="Interlock" name="CutsHeater"/>
        <testcase classname="Interlock" name="Own"><properties>
          <property name="requirement" value="REQ-4"/></properties></testcase>
      </testsuite>
    </testsuites>"""
    ev = ingest.collect([write(tmp_path, "bazel-testlogs/fw/interlock_test/test.xml", xml)])
    cut, own = ev.cases
    # level and artifact.* still reach the cases; requirement ids do not (P6/P12).
    assert cut.declared == () and own.declared == ("REQ-4",)
    assert cut.level == own.level == "hil" and cut.artifact == {"sha": "abc"}
    assert "hostname" not in cut.properties
    assert cut.suite_declared == own.suite_declared == ("REQ-9", "REQ-1", "REQ-3")
    # One warning per suite, not per case.
    assert ev.issues == [
        IngestIssue(
            "suite-level-requirement", cut.source, "//fw:interlock_test", "Interlock", ("REQ-9", "REQ-1", "REQ-3")
        )
    ]
    assert "[suite-level-requirement]" in str(ev.issues[0]) and "not inherited" in str(ev.issues[0])
    assert ev.for_id("REQ-1") == [] and ev.for_id("REQ-4") == [own]


def test_nested_testcases_become_a_scope(tmp_path):
    xml = """<testsuite name="go">
      <testcase classname="pkg" name="TestParse [rr:REQ-8]" file="pkg/parse_test.go">
        <properties><property name="level" value="unit"/><property name="requirement" value="REQ-7"/></properties>
        <testcase name="empty" line="12"/>
        <testcase name="unicode"><failure message="bad rune"/></testcase>
        <testcase name="deeper"><testcase name="leaf [rr:REQ-1]"/></testcase>
      </testcase>
      <testcase classname="pkg" name="TestSetup"><error message="setup broke"/>
        <testcase name="never"><skipped/></testcase>
      </testcase>
      <testcase classname="pkg" name="TestAggregate"><failure message="1 subtest failed"/>
        <testcase name="sub"><failure message="boom"/></testcase>
      </testcase>
      <testcase name="TopLevel"><testcase name="child"/></testcase>
    </testsuite>"""
    ev = ingest.collect([write(tmp_path, "bazel-testlogs/pkg/parse_test/test.xml", xml)])
    rows = index_cases(ev)
    by_path = {k.path: r for k, r in rows.items()}
    assert sorted(by_path) == [
        "TopLevel::child",
        "pkg > TestAggregate::sub",
        "pkg > TestParse > deeper::leaf",
        "pkg > TestParse::empty",
        "pkg > TestParse::unicode",
        "pkg > TestSetup::<hooks>",
        "pkg > TestSetup::never",
    ]
    empty = by_path["pkg > TestParse::empty"]
    # The parent's level and source reach its children; its ids (property or
    # name tag) are scope-level, and do not.
    assert empty.status == "passed" and empty.level == "unit" and empty.declared == ()
    assert empty.file == "pkg/parse_test.go" and empty.line == 12
    assert by_path["pkg > TestParse::unicode"].status == "failed"
    assert by_path["pkg > TestParse > deeper::leaf"].declared == ("REQ-1",)
    # A parent failing on its own is target-scope; one failing because a
    # child failed is not reported twice.
    hooks = by_path["pkg > TestSetup::<hooks>"]
    assert hooks.target_scope and hooks.status == "error" and "setup broke" in hooks.message
    assert not any(k.path.startswith("pkg > TestAggregate::<") for k in rows)
    leaf = next(c for c in ev.cases if c.name == "empty")
    assert leaf.suite_declared == ("REQ-7", "REQ-8")
    assert {i.scope for i in ev.issues} == {"go"}


def test_records_target_and_single_requirement(tmp_path):
    path = write(
        tmp_path,
        "evidence/panel_inspection.rr.yaml",
        """
        target: record:panel_inspection
        evidence:
          - name: label-legible
            status: passed
            requirement: REQ-12
          - name: legacy-list
            status: passed
            requirements: [REQ-13]
          - name: two-ids
            status: passed
            requirements: [REQ-13, REQ-14]
          - name: both-keys
            status: passed
            requirement: REQ-1
            requirements: [REQ-2]
          - name: elsewhere
            status: failed
            target: //bench:soak_test
            requirement: [REQ-3]
          - name: untagged
            status: passed
        """,
    )
    cases = {c.name: c for c in ingest.collect([path]).cases}
    assert cases["label-legible"].declared == ("REQ-12",)
    assert cases["legacy-list"].declared == ("REQ-13",)
    assert cases["two-ids"].declared == ("REQ-13", "REQ-14")  # multi-tag downstream
    assert cases["both-keys"].declared == ("REQ-1", "REQ-2")
    assert cases["untagged"].declared == ()
    assert key_of(cases["label-legible"]) == CaseKey("record:panel_inspection", "label-legible")
    assert key_of(cases["elsewhere"]) == CaseKey("//bench:soak_test", "elsewhere")
    # Without a document target, the key's target is record:<file stem>.
    bare = write(tmp_path, "bench.rr.yaml", "evidence:\n  - {name: x, status: passed, requirement: REQ-1}\n")
    (case,) = ingest.collect([bare]).cases
    assert case.target == "" and key_of(case) == CaseKey("record:bench", "x")


def test_rust_trace_lines_single_id_and_legacy_lists():
    cases = parse_libtest(LIBTEST, target="//r:t")
    by_path = {f"{c.classname}::{c.name}": c for c in cases}
    trace = "\n".join(
        [
            json.dumps({"test": "parse::rejects_empty", "requirement": "REQ-1"}),
            json.dumps({"test": "parse::accepts_c", "requirements": ["REQ-1", "REQ-2"]}),  # 0.2 list
            json.dumps({"test": "slow::soak", "requirement": "REQ-3"}),
            json.dumps({"test": "slow::soak", "requirement": "REQ-4"}),  # a second call, another id
            json.dumps(["not", "an", "object"]),
            json.dumps({"test": "top_level", "requirement": ""}),
        ]
    )
    merge_trace(cases, trace)
    assert by_path["parse::rejects_empty"].declared == ("REQ-1",)
    assert by_path["parse::accepts_c"].declared == ("REQ-1", "REQ-2")
    assert by_path["slow::soak"].declared == ("REQ-3", "REQ-4")
    assert by_path["::top_level"].declared == ()


def test_file_and_line(tmp_path):
    xml = (
        '<testsuite name="s">'
        '<testcase classname="c" name="a" file="/x/t.runfiles/_main/pkg/a_test.cc" line="7"/>'
        '<testcase classname="c" name="b" line="nope"><properties>'
        '<property name="rr.file" value="bazel-out/k8-fastbuild/bin/pkg/b_test.py"/></properties></testcase>'
        "</testsuite>"
    )
    a, b = JUnitIngestor().ingest(write(tmp_path, "r.xml", xml))
    assert (a.file, a.line) == ("pkg/a_test.cc", 7)
    assert (b.file, b.line) == ("pkg/b_test.py", 0)
    assert a.properties["rr.file"] == "/x/t.runfiles/_main/pkg/a_test.cc"  # the raw value is kept


def test_a_bare_testcase_report(tmp_path):
    (case,) = JUnitIngestor().ingest(write(tmp_path, "bazel-testlogs/p/t/test.xml", '<testcase name="only"/>'))
    assert key_of(case) == CaseKey("//p:t", "only") and case.status == "passed"
