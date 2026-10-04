# SPDX-License-Identifier: AGPL-3.0-or-later
import json
import os
import shutil
import sys
import unittest
import warnings

import pytest
from conftest import junit, write

# Producer harnesses of other test modules, reused by the producer matrix below.
from test_hooks import _run_pytest
from test_node_runner import needs_node, needs_reporters, run_fixture  # noqa: F401  (a fixture)
from test_rr_case import _run as _run_rr_case
from test_rr_case import binary  # noqa: F401  (a fixture)

from rules_requirements import bazel, cli, ingest, rr
from rules_requirements.case_keys import SYNTHETIC_PATH, CaseKey, declared_of, index_cases, is_target_scope, key_of
from rules_requirements.hooks import unittest as rr_unittest
from rules_requirements.hooks import wrap
from rules_requirements.hooks.checkplan import CheckPlan
from rules_requirements.hooks.junit_writer import JUnitWriter
from rules_requirements.ingest import IngestIssue, Ingestor, TestCase, apply_properties, split_ids
from rules_requirements.ingest.junit import JUnitIngestor, is_testlogs_report, target_from_path
from rules_requirements.ingest.libtest import merge_trace, parse_libtest, trace_ids


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


@pytest.mark.parametrize(
    "rel, data",
    [
        ("test.xml", b""),  # a crash left an empty report
        ("test.xml", b"\x00\x01garbage, not XML"),
        ("shard_1_of_2/test.xml", b"   \n"),
        ("test_attempts/attempt_1.xml", b""),
        ("test.xml", b'<?xml version="1.0"?><html><body>not a test report</body></html>'),
    ],
    ids=["empty", "binary", "blank-shard", "empty-attempt", "foreign-root"],
)
def test_a_testlogs_report_that_cannot_be_read_taints_its_target(tmp_path, rel, data):
    """A file Bazel names as a target's report is never skipped: unreadable, it
    is the target's <unreadable> error, so the target reads tainted, not as
    "did not run"."""
    path = tmp_path / "bazel-testlogs" / "pkg" / "crash_test" / rel
    path.parent.mkdir(parents=True)
    path.write_bytes(data)
    ev = ingest.collect([str(tmp_path / "bazel-testlogs")])
    assert ev.skipped_files == []
    (case,) = ev.cases
    assert (case.target, case.name, case.status, case.scope) == ("//pkg:crash_test", "<unreadable>", "error", "target")
    assert "unreadable JUnit report" in case.message and case.declared == ()


def test_a_testlogs_report_with_a_long_prolog_is_still_read(tmp_path):
    # The root element after more than the 4 KiB sniff window: the failure is kept.
    xml = (
        '<?xml version="1.0"?>\n<!--' + "x" * 5000 + "-->\n"
        '<testsuites><testsuite name="s"><testcase classname="c" name="a"><failure message="boom"/>'
        "</testcase></testsuite></testsuites>"
    )
    ev = ingest.collect([str(write(tmp_path, "bazel-testlogs/pkg/late_test/test.xml", xml))])
    assert [(c.target, c.name, c.status) for c in ev.cases] == [("//pkg:late_test", "a", "failed")]


def test_only_bazel_report_files_skip_the_sniff(tmp_path):
    # Outside a testlogs tree, or for any other file in it, an XML file that
    # is not JUnit is not evidence (an undeclared output, a coverage report).
    assert is_testlogs_report("bazel-testlogs/p/t/test.xml")
    assert is_testlogs_report("bazel-testlogs/p/t/run_1_of_2/test_attempts/attempt_3.xml")
    assert is_testlogs_report("ev/testlogs/p/t/shard_1_of_2_run_1_of_2/test.xml")
    assert not is_testlogs_report("reports/test.xml")
    assert not is_testlogs_report("bazel-testlogs/p/t/test.outputs/test.xml")
    assert not is_testlogs_report("bazel-testlogs/p/t/test.outputs/coverage.xml")
    assert not is_testlogs_report("bazel-testlogs/p/t/test_attempts/notes.xml")
    write(tmp_path, "reports/test.xml", "")
    write(tmp_path, "bazel-testlogs/p/t/test.outputs/coverage.xml", "<coverage/>")
    ev = ingest.collect([str(tmp_path)])
    assert ev.cases == [] and len(ev.skipped_files) == 2


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
    assert [c.name for c in ev.cases if "REQ-1" in c.declared] == ["x", "y"]
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
    assert [c.name for c in ev.cases if "REQ-7" in c.declared] == ["top_level"]
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
        assert next(c for c in ev.cases if "REQ-1" in c.declared).name == "adds"
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


@pytest.mark.parametrize(
    "build",
    [
        lambda: TestCase("t", "passed", declared=["PR-1, PR-2"]),
        lambda: TestCase("t", "passed", requirements="PR-1 PR-2"),
        lambda: TestCase("t", "passed", declared="PR-1,PR-2"),
        lambda: TestCase("t", "passed", declared=["PR-1", " PR-2 ", "PR-1"]),
    ],
    ids=["comma-in-element", "alias-string", "string", "padded-duplicate"],
)
def test_hand_built_ids_are_normalized(build):
    """However a third-party ingestor or hand-built Evidence spells two ids,
    the case declares two (a multi-tag downstream), never one odd id."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)  # the requirements= alias
        case = build()
    assert case.declared == ("PR-1", "PR-2") and declared_of(case) == ("PR-1", "PR-2")


def test_assigned_ids_are_normalized_too():
    case = TestCase("t", "passed", declared=[" PR-1 "])
    assert case.declared == ("PR-1",)
    with pytest.warns(DeprecationWarning):
        case.requirements = ["PR-1,PR-2"]
    assert case.declared == ("PR-1", "PR-2")
    case.declared = ["PR-3 PR-4", ""]  # a direct assignment, as an ingestor might do
    assert case.declared == ("PR-3", "PR-4")
    case.suite_declared = "PR-5, PR-6"
    assert case.suite_declared == ("PR-5", "PR-6")
    assert TestCase("t", "passed", suite_declared=["PR-7 PR-8"]).suite_declared == ("PR-7", "PR-8")
    # declared_of splits on its own as well (a case object built around __setattr__).
    raw = TestCase("t", "passed")
    object.__setattr__(raw, "declared", ("PR-1,PR-2",))
    assert declared_of(raw) == ("PR-1", "PR-2")


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
    assert [c for c in ev.cases if "REQ-1" in c.declared] == [] and [c for c in ev.cases if "REQ-4" in c.declared] == [
        own
    ]


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


def test_a_nested_case_keys_under_its_parent_whatever_its_own_classname(tmp_path):
    # The rule: a nested case's classname is its parent's path; a classname
    # attribute of its own (some runners repeat the suite's) is ignored.
    xml = """<testsuite name="s">
      <testcase classname="pkg" name="TestA"><testcase classname="other.Module" name="sub"/></testcase>
    </testsuite>"""
    (case,) = JUnitIngestor().ingest(write(tmp_path, "bazel-testlogs/pkg/a_test/test.xml", xml))
    assert key_of(case) == CaseKey("//pkg:a_test", "pkg > TestA::sub")


def test_subtests_in_a_suite_nested_in_a_testcase_are_kept(tmp_path):
    xml = """<testsuites><testsuite name="s">
      <testcase classname="c" name="outer [rr:REQ-5]">
        <testsuite name="inner"><properties><property name="requirement" value="REQ-6"/></properties>
          <testcase classname="k" name="leaf"><failure message="x"/></testcase>
          <testcase name="ok"><properties><property name="requirement" value="REQ-1"/></properties></testcase>
        </testsuite>
      </testcase>
      <testcase classname="c" name="lonely"><failure message="own"/><testsuite name="empty"/></testcase>
    </testsuite></testsuites>"""
    ev = ingest.collect([write(tmp_path, "bazel-testlogs/pkg/nest_test/test.xml", xml)])
    got = {str(key_of(c)): (c.status, c.scope, c.declared, c.suite_declared) for c in ev.cases}
    assert got == {
        # The failing leaf is kept (it used to vanish behind a passing "outer").
        "//pkg:nest_test#c > outer::leaf": ("failed", "case", (), ("REQ-5", "REQ-6")),
        "//pkg:nest_test#c > outer::ok": ("passed", "case", ("REQ-1",), ("REQ-5", "REQ-6")),
        # A <testcase> whose nested suite is empty is an ordinary case.
        "//pkg:nest_test#c::lonely": ("failed", "case", (), ()),
    }


def test_a_requirement_attribute_on_a_suite_is_suite_level(tmp_path):
    xml = '<testsuite name="s" requirement="REQ-1, REQ-2"><testcase classname="c" name="a"/></testsuite>'
    ev = ingest.collect([write(tmp_path, "bazel-testlogs/p/t/test.xml", xml)])
    (case,) = ev.cases
    assert case.declared == () and case.suite_declared == ("REQ-1", "REQ-2")
    assert [(i.code, i.ids) for i in ev.issues] == [("suite-level-requirement", ("REQ-1", "REQ-2"))]


def test_records_target_and_single_requirement(tmp_path):
    path = write(
        tmp_path,
        "evidence/panel_inspection.rr.yaml",
        """
        target: //bench:panel_test
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
    # The document's target (not the record:<stem> fallback) files every entry
    # that names none of its own.
    assert cases["label-legible"].target == "//bench:panel_test"
    assert key_of(cases["label-legible"]) == CaseKey("//bench:panel_test", "label-legible")
    assert key_of(cases["untagged"]) == CaseKey("//bench:panel_test", "untagged")
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


@pytest.mark.parametrize(
    "rec, ids",
    [
        ({"requirement": "REQ-1"}, ["REQ-1"]),
        ({"requirements": ["REQ-1", "REQ-2"]}, ["REQ-1", "REQ-2"]),
        ({"requirements": "REQ-1,REQ-2"}, ["REQ-1,REQ-2"]),  # a string under the plural key
        ({"requirement": ["REQ-1", "REQ-2"]}, ["REQ-1", "REQ-2"]),  # a list under the singular key
        ({"requirement": "REQ-1", "requirements": ["REQ-2"]}, ["REQ-1", "REQ-2"]),
        ({"requirement": 7}, ["7"]),
        ({"requirement": "", "requirements": None}, []),
        ({"test": "x"}, []),
    ],
)
def test_trace_ids_reads_every_shape(rec, ids):
    """Fail closed: a trace line naming two ids, in whatever shape, is never read
    as one id or none (each shape below reaches merge_trace as a multi-tag)."""
    assert trace_ids(rec) == ids
    (case,) = merge_trace([TestCase("t", "passed", classname="m")], json.dumps({"test": "m::t", **rec}))
    assert len(case.declared) == len(set(split_ids(",".join(ids))))


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


# --- Every producer M1 ships, read back the v0.3 way -------------------------
#
# Each producer's real output is placed where Bazel would put it
# (bazel-testlogs/<pkg>/<name>/...) and folded by key. The invariants checked
# for every producer: a case declares ids only as tags (never owners), a case
# its producer tagged once declares exactly that one id, whole-run results are
# target-scope or synthetic (never members), and the key does not depend on
# tags or execution dimensions.


def _testlogs(tmp_path, label, xml_source, rel="test.xml"):
    """Copy a report to bazel-testlogs/<pkg>/<name>/<rel>; returns the testlogs root."""
    pkg, name = label.lstrip("/").split(":")
    dest = tmp_path / "bazel-testlogs" / pkg / name / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(str(xml_source), str(dest))
    return tmp_path / "bazel-testlogs"


def _rows(*roots):
    return {str(k): r for k, r in index_cases(ingest.collect([str(r) for r in roots])).items()}


@needs_reporters
def test_producer_node_reporter(run_fixture, tmp_path):  # noqa: F811
    diag = run_fixture("diagnostics")
    fail = run_fixture("failures")
    _testlogs(tmp_path, "//tests/node:diagnostics_test", diag.xml)
    rows = _rows(_testlogs(tmp_path, "//tests/node:failures_test", fail.xml))
    raw = rows["//tests/node:diagnostics_test#diagnostics::raw diagnostics"]
    assert raw.declared == ("REQ-1",) and raw.level == "hil"
    assert raw.file == "tests/node/diagnostics.test.cjs" and raw.line > 0
    # The verifies helper refused a second id inside the test: one id, a failed case.
    second = rows["//tests/node:diagnostics_test#diagnostics::verifies refuses a second id"]
    assert second.declared == ("REQ-3",) and second.status == "failed"
    assert rows["//tests/node:diagnostics_test#diagnostics > after a sibling's diagnostics::second"].declared == ()
    hooks = rows["//tests/node:failures_test#failures > before hook fails::<hooks>"]
    assert hooks.target_scope and hooks.status == "error" and hooks.declared == ()
    assert all(len(r.declared) <= 1 for r in rows.values())


def test_producer_rr_case_h(binary, tmp_path):  # noqa: F811
    _, xml = _run_rr_case(binary, tmp_path)
    rows = _rows(_testlogs(tmp_path, "//fw:rr_case_fixture", xml))
    check = rows["//fw:rr_case_fixture#fixture::check_fails"]
    assert check.status == "failed" and check.declared == ("REQ-2",)
    assert rows["//fw:rr_case_fixture#fixture::passes"].declared == ("REQ-1",)
    # rr_case.h refuses "REQ-1,REQ-2" itself (RR-E104): an error case with no id.
    assert rows["//fw:rr_case_fixture#fixture::comma_id"].declared == ()
    assert all(len(r.declared) <= 1 for r in rows.values())
    assert not any(r.target_scope or r.synthetic for r in rows.values())
    # RR_CASE definitions also write their source (file/line attributes).
    (tmp_path / "registry").mkdir()
    _, xml = _run_rr_case(binary, tmp_path / "registry", RR_FIXTURE_FORM="registry")
    rows = _rows(_testlogs(tmp_path / "registry", "//fw:rr_case_registry", xml))
    tagged = next(r for k, r in rows.items() if k.endswith("::registered_tagged"))
    assert tagged.declared == ("REQ-9",) and tagged.file.endswith("rr_case_fixture.cc") and tagged.line > 0


def test_producer_junit_writer_and_checkplan(tmp_path):
    report = JUnitWriter("hitl_e2e", default_level="hitl", file="pi/hitl/harness/hitl_e2e.py")
    plan = CheckPlan(
        report,
        {"flash_boot": ("ble_advertising",), "websocket_checks": ("ws_connect", "rename")},
        tags={"flash_boot.ble_advertising": "REQ-13", "websocket_checks.rename": "REQ-35"},
    )
    with pytest.raises(RuntimeError), plan.run():
        plan.setup_done()
        with plan.step("flash_boot"), plan.check("ble_advertising"):
            pass
        with plan.step("websocket_checks"):
            raise RuntimeError("DUT rebooted")  # a device failure
    with pytest.warns(DeprecationWarning):
        report.add("legacy_two_ids", ["REQ-1", "REQ-2"])  # the deprecated list form
    xml = tmp_path / "e2e.xml"
    report.write(str(xml))
    rows = _rows(_testlogs(tmp_path, "//pi/hitl/harness:e2e_netstack", xml))
    t = "//pi/hitl/harness:e2e_netstack#"
    ble = rows[t + "hitl_e2e.flash_boot::ble_advertising"]
    assert (ble.status, ble.declared, ble.level) == ("passed", ("REQ-13",), "hitl")
    assert ble.file == "pi/hitl/harness/hitl_e2e.py"
    rename = rows[t + "hitl_e2e.websocket_checks::rename"]
    assert rename.status == "failed" and rename.declared == ("REQ-35",)  # "not reached": its own one id
    assert rows[t + "hitl_e2e.websocket_checks::ws_connect"].declared == ()
    # A list of two ids is written, and read as two declared ids: a multi-tag case.
    assert rows[t + "hitl_e2e::legacy_two_ids"].declared == ("REQ-1", "REQ-2")


def test_producer_pytest_plugin(tmp_path):
    (tmp_path / "test_mod.py").write_text(
        "import pytest\n"
        "@pytest.mark.rr('REQ-2', level='sil')\n"
        "@pytest.mark.parametrize('x', ['a', 'b'])\n"
        "def test_x(x):\n    pass\n"
        "@pytest.mark.rr('REQ-3', 'REQ-4')\n"
        "def test_two():\n    pass\n"
        "def test_untagged():\n    pass\n"
    )
    proc, out, _ = _run_pytest(tmp_path)
    assert proc.returncode == 0, out
    rows = _rows(_testlogs(tmp_path, "//pkg:mod_test", tmp_path / "out.xml"))
    assert rows["//pkg:mod_test#test_mod::test_x[a]"].declared == ("REQ-2",)
    assert rows["//pkg:mod_test#test_mod::test_x[b]"].level == "sil"
    assert rows["//pkg:mod_test#test_mod::test_two"].declared == ("REQ-3", "REQ-4")  # multi-tag
    assert rows["//pkg:mod_test#test_mod::test_untagged"].declared == ()


def test_producer_unittest_hook(tmp_path):
    class Hooked(unittest.TestCase):
        @rr.verifies("REQ-5", level="inspection")
        def test_a(self):
            pass

        def test_b(self):
            self.fail("nope")

    xml = tmp_path / "unit.xml"
    rr_unittest.run(unittest.defaultTestLoader.loadTestsFromTestCase(Hooked), str(xml), "unit", verbosity=0)
    rows = _rows(_testlogs(tmp_path, "//pkg:unit_test", xml))
    (a,) = [r for k, r in rows.items() if k.endswith("Hooked::test_a")]
    (b,) = [r for k, r in rows.items() if k.endswith("Hooked::test_b")]
    assert a.declared == ("REQ-5",) and a.level == "inspection" and a.status == "passed"
    assert b.declared == () and b.status == "failed"


def test_producer_googletest_with_rr_gtest(tmp_path):
    """googletest's own XML for rr_gtest.h: RecordProperty in a test lands on the
    case (the 0.2 `requirements` name, a comma list for several ids); in
    SetUpTestSuite it lands on the suite, which no case inherits any more."""
    xml = write(
        tmp_path,
        "bazel-testlogs/tests/integration/gtest_hook_test/test.xml",
        """<?xml version="1.0" encoding="UTF-8"?>
        <testsuites tests="3" failures="0" disabled="0" errors="0" time="0." name="AllTests">
          <testsuite name="Interlock" tests="2" failures="0" disabled="0" skipped="0" errors="0" time="0.">
            <testcase name="CutsHeaterAtLimit" file="tests/integration/gtest_hook_test.cc" line="4"
                      status="run" result="completed" time="0." classname="Interlock">
              <properties><property name="requirements" value="REQ-3"/></properties>
            </testcase>
            <testcase name="TwoIds" file="tests/integration/gtest_hook_test.cc" line="10"
                      status="run" result="completed" time="0." classname="Interlock">
              <properties><property name="requirements" value="REQ-3,REQ-1"/></properties>
            </testcase>
          </testsuite>
          <testsuite name="SuiteLevel" tests="1" failures="0" disabled="0" skipped="0" errors="0" time="0.">
            <properties><property name="requirements" value="REQ-3"/></properties>
            <testcase name="Inherits" file="tests/integration/gtest_hook_test.cc" line="24"
                      status="run" result="completed" time="0." classname="SuiteLevel"/>
            <testcase name="Disabled" status="notrun" result="suppressed" time="0." classname="SuiteLevel"/>
          </testsuite>
        </testsuites>""",
    )
    ev = ingest.collect([xml])
    rows = {str(k): r for k, r in index_cases(ev).items()}
    t = "//tests/integration:gtest_hook_test#"
    cut = rows[t + "Interlock::CutsHeaterAtLimit"]
    assert cut.declared == ("REQ-3",) and (cut.file, cut.line) == ("tests/integration/gtest_hook_test.cc", 4)
    assert rows[t + "Interlock::TwoIds"].declared == ("REQ-3", "REQ-1")  # multi-tag
    assert rows[t + "SuiteLevel::Inherits"].declared == ()  # suite level: not inherited
    assert rows[t + "SuiteLevel::Disabled"].status == "skipped"
    assert [(i.code, i.scope, i.ids) for i in ev.issues] == [("suite-level-requirement", "SuiteLevel", ("REQ-3",))]


_FAKE_LIBTEST = """#!/usr/bin/env python3
import json, os, sys
trace = open(os.environ["RR_TRACE_FILE"], "a")
mode = sys.argv[1] if len(sys.argv) > 1 else ""
if mode == "silent":
    print("thread 'main' panicked before any test ran")
    sys.exit(101)
# What the shipped crate writes (rr::trace_line: a "requirements" list), and
# the single "requirement" form of 0.3.
lines = [
    {"test": "parse::rejects_empty", "requirements": ["REQ-4"], "level": "sil"},
    {"test": "parse::two_calls", "requirements": ["REQ-4"]},
    {"test": "parse::two_calls", "requirements": ["REQ-1"]},
    {"test": "parse::two_ids", "requirements": ["REQ-4", "REQ-1"]},
    {"test": "parse::single", "requirement": "REQ-7"},
]
if mode == "crash":
    lines.append({"test": "parse::dies", "requirements": ["REQ-6"]})
for rec in lines:
    trace.write(json.dumps(rec) + "\\n")
trace.flush()
print("running 4 tests")
print("test parse::rejects_empty ... ok")
print("test parse::two_calls ... ok")
print("test parse::two_ids ... ok")
print("test parse::single ... ok")
if mode == "crash":
    print("test parse::dies ... ", flush=True)
    import resource
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))  # no core file in the working directory
    os.abort()
print("test result: ok. 4 passed; 0 failed")
sys.exit(3 if mode == "leak" else 0)
"""


def _wrap(tmp_path, monkeypatch, mode, label="//rust:parse_test"):
    fake = tmp_path / "fake_libtest"
    fake.write_text(_FAKE_LIBTEST)
    fake.chmod(0o755)
    monkeypatch.setenv("TEST_TMPDIR", str(tmp_path / f"tmp_{mode or 'ok'}"))
    (tmp_path / f"tmp_{mode or 'ok'}").mkdir()
    xml = tmp_path / f"{mode or 'ok'}.xml"
    wrap.main(["--target", label, "--junit-xml", str(xml), "--", str(fake), *([mode] if mode else [])])
    return _rows(_testlogs(tmp_path / (mode or "ok"), label, xml))


def test_producer_rust_hook_through_rr_wrap(tmp_path, monkeypatch, capsys):
    rows = _wrap(tmp_path, monkeypatch, "")
    t = "//rust:parse_test#"
    ok = rows[t + "parse::rejects_empty"]
    assert ok.declared == ("REQ-4",) and ok.level == "sil"
    # Two rr::verifies! calls with different ids, or one call with two: every id, a multi-tag case.
    assert rows[t + "parse::two_calls"].declared == ("REQ-4", "REQ-1")
    assert rows[t + "parse::two_ids"].declared == ("REQ-4", "REQ-1")
    assert rows[t + "parse::single"].declared == ("REQ-7",)
    # A run that exits non-zero after passing: the exit status is about the
    # target (taint), never a member.
    leak = _wrap(tmp_path, monkeypatch, "leak")
    (exit_status,) = [r for r in leak.values() if r.key.path.endswith("exit-status")]
    assert exit_status.target_scope and exit_status.status == "error"
    assert exit_status.declared == ()  # not the union of the ids the run traced (B5)
    # The test that was running when the binary died keeps its own one id.
    crash = _wrap(tmp_path, monkeypatch, "crash")
    dies = crash[t + "parse::dies"]
    assert dies.status == "error" and dies.declared == ("REQ-6",) and not dies.target_scope
    # Nothing parseable: whole-run results only (synthetic or target-scope), no member.
    silent = _wrap(tmp_path, monkeypatch, "silent")
    assert silent and all(r.synthetic or r.target_scope for r in silent.values())
    assert all(r.status == "error" for r in silent.values())
    capsys.readouterr()


def test_producer_rr_wrap_junit_format(tmp_path, monkeypatch, capsys):
    runner = tmp_path / "runner"
    runner.write_text(
        "#!/bin/sh\n"
        'mkdir -p "$TEST_TMPDIR/r"\n'
        'printf \'%s\' \'<testsuite name="go"><testcase classname="pkg" name="TestA [rr:REQ-1]">'
        '<testcase name="sub"/></testcase></testsuite>\' > "$TEST_TMPDIR/r/junit.xml"\n'
        'exit "$1"\n'
    )
    runner.chmod(0o755)
    monkeypatch.setenv("TEST_TMPDIR", str(tmp_path))
    rows = {}
    for code in ("0", "3"):
        xml = tmp_path / f"out{code}.xml"
        args = ["--format", "junit", "--junit-in", "$TEST_TMPDIR/r/junit.xml", "--target", "//go:a_test"]
        wrap.main([*args, "--junit-xml", str(xml), "--", str(runner), code])
        rows[code] = _rows(_testlogs(tmp_path / code, "//go:a_test", xml))
    # The nested case is a case under its parent's scope; the parent's tag is not inherited.
    assert rows["0"]["//go:a_test#pkg > TestA::sub"].declared == ()
    assert not any(r.target_scope for r in rows["0"].values())
    assert any(r.target_scope and r.status == "error" for r in rows["3"].values())
    capsys.readouterr()


@pytest.mark.parametrize("failed", [False, True])
def test_producer_bazel_generated_xml(tmp_path, failed):
    """Bazel's generate-xml.sh output (7.x and 8.x) for a target that writes no JUnit."""
    body = '<error message="exited with error code 1"></error>' if failed else ""
    write(
        tmp_path,
        "bazel-testlogs/web/flashEnv_test/test.xml",
        '<?xml version="1.0" encoding="UTF-8"?>\n<testsuites>\n'
        f'  <testsuite name="web/flashEnv_test_/flashEnv_test" tests="1" failures="0" errors="{int(failed)}">\n'
        f'    <testcase name="web/flashEnv_test_/flashEnv_test" status="run" duration="1" time="1">{body}</testcase>\n'
        "      <system-out>\nGenerated test.log (if the file is not UTF-8, then this may be unreadable):\n"
        '<![CDATA[exec ${PAGER:-/usr/bin/less} "$0" || exit 1\n]]>\n      </system-out>\n'
        "    </testsuite>\n</testsuites>\n",
    )
    (row,) = _rows(tmp_path / "bazel-testlogs").values()
    assert row.key == CaseKey("//web:flashEnv_test", SYNTHETIC_PATH)
    assert row.synthetic and not row.target_scope and row.declared == ()
    assert row.status == ("error" if failed else "passed")


def test_producer_rr_evidence(tmp_path, monkeypatch):
    """bazel.py's rr_evidence runner: a [target] result for a test without JUnit,
    and a target-scope exit status after a passing report."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "bin").mkdir()
    for name, src in (
        ("plain", "print('ok')\n"),
        (
            "leaky",
            "import os, sys\n"
            "open(os.environ['XML_OUTPUT_FILE'],'w').write('<testsuite>"
            '<testcase name="ok"><properties><property name="requirement" value="REQ-1"/></properties></testcase>'
            '<testcase name="ok2"><properties><property name="requirement" value="REQ-2"/></properties></testcase>'
            "</testsuite>')\n"
            "sys.exit(23)\n",
        ),
    ):
        exe = tmp_path / "bin" / name
        exe.write_text("#!" + sys.executable + "\n" + src)
        exe.chmod(0o755)
    out = tmp_path / "ev" / "testlogs"
    bazel.main(
        ["run-tests", "--out", str(out), "--test", "//p:plain=bin/plain=_main", "--test", "//p:leaky=bin/leaky=_main"]
    )
    rows = _rows(out)
    assert rows["//p:plain#" + SYNTHETIC_PATH].synthetic
    assert rows["//p:leaky#ok"].status == "passed" and not rows["//p:leaky#ok"].target_scope
    assert rows["//p:leaky#ok"].declared == ("REQ-1",) and rows["//p:leaky#ok2"].declared == ("REQ-2",)
    (exit_status,) = [r for k, r in rows.items() if k.endswith("exit-status")]
    assert exit_status.target_scope and exit_status.status == "error"
    assert exit_status.declared == ()  # not REQ-1 and REQ-2 (B5)


def test_producer_rr_case_cli(capsys, tmp_path, monkeypatch):
    """`rr case` (shell harnesses): each call appends one case to the target's report."""
    out = tmp_path / "out.xml"
    monkeypatch.setenv("TEST_TARGET", "//bench:smoke_test")
    monkeypatch.chdir(tmp_path)
    calls = [
        ["--name", "boots", "--requirement", "REQ-1", "--level", "hitl", "--file", "bench/smoke.sh"],
        ["--name", "banner [rr:REQ-2]", "--status", "failed", "--message", "no banner"],
        ["--name", "untagged", "--classname", "bench.misc"],
        ["--name", "two", "--requirement", "REQ-1,REQ-2"],  # refused (RR-E104): nothing appended
    ]
    codes = [cli.main(["case", "--out", str(out), *argv]) for argv in calls]
    assert codes == [0, 0, 0, 2]
    capsys.readouterr()
    rows = _rows(_testlogs(tmp_path, "//bench:smoke_test", out))
    t = "//bench:smoke_test#"
    assert set(rows) == {t + "smoke_test::boots", t + "smoke_test::banner", t + "bench.misc::untagged"}
    boots = rows[t + "smoke_test::boots"]
    assert (boots.status, boots.declared, boots.level, boots.file) == ("passed", ("REQ-1",), "hitl", "bench/smoke.sh")
    assert rows[t + "smoke_test::banner"].declared == ("REQ-2",) and rows[t + "smoke_test::banner"].status == "failed"
    assert rows[t + "bench.misc::untagged"].declared == ()
    assert not any(r.synthetic or r.target_scope for r in rows.values())


@needs_node
def test_producer_node_without_a_reporter(run_fixture, tmp_path):  # noqa: F811
    """rr_node_test on a Node without --test-reporter (< 20, forced here): one
    synthetic [target] result, never a member of a case claim."""
    ok = run_fixture("nesting", env={"RR_NODE_TEST_PLAIN": "1"})
    rows = _rows(_testlogs(tmp_path / "ok", "//tests/node:nesting_test", ok.xml))
    assert list(rows) == ["//tests/node:nesting_test#" + SYNTHETIC_PATH]
    (row,) = rows.values()
    assert row.synthetic and not row.target_scope and row.status == "passed" and row.declared == ()
    bad = run_fixture("load_error", env={"RR_NODE_TEST_PLAIN": "1"})
    (row,) = _rows(_testlogs(tmp_path / "bad", "//tests/node:load_error_test", bad.xml)).values()
    assert row.key.path == SYNTHETIC_PATH and row.status == "failed" and row.declared == ()


def _write_report(path, cases):
    """Write a JUnitWriter report of ``(name, status, id)`` cases to ``path``."""
    w = JUnitWriter("suite", file="")
    for name, status, req in cases:
        w.add(name, req, status=status)
    os.makedirs(os.path.dirname(str(path)), exist_ok=True)
    w.write(str(path))


def test_sharded_and_runs_per_test_layouts_and_attempts(tmp_path):
    root = tmp_path / "bazel-testlogs"
    # --test_sharding: shards are unioned.
    for shard, cases in ((1, [("a", "passed", "REQ-1")]), (2, [("b", "passed", "REQ-2")])):
        _write_report(root / "pkg" / "sharded_test" / f"shard_{shard}_of_2" / "test.xml", cases)
    # --runs_per_test=3: the worst run wins.
    for run, status in ((1, "passed"), (2, "failed"), (3, "passed")):
        _write_report(root / "pkg" / "rep_test" / f"run_{run}_of_3" / "test.xml", [("r", status, "REQ-3")])
    # sharded and repeated: shard_i_of_n_run_k_of_m.
    for shard in (1, 2):
        for run in (1, 2):
            status = "failed" if (shard, run) == (2, 2) else "passed"
            dims = f"shard_{shard}_of_2_run_{run}_of_2"
            _write_report(root / "pkg" / "both_test" / dims / "test.xml", [(f"s{shard}", status, None)])
    # --flaky_test_attempts: the final test.xml wins; an earlier failure makes it flaky.
    t = root / "pkg" / "flaky_test"
    _write_report(t / "test_attempts" / "attempt_1.xml", [("f", "failed", "REQ-4"), ("g", "passed", "REQ-5")])
    _write_report(t / "test.xml", [("f", "passed", "REQ-4"), ("g", "passed", "REQ-6")])
    rows = _rows(root)
    assert rows["//pkg:sharded_test#suite::a"].declared == ("REQ-1",)
    assert rows["//pkg:sharded_test#suite::b"].status == "passed"
    assert not rows["//pkg:sharded_test#suite::a"].duplicate
    assert rows["//pkg:rep_test#suite::r"].status == "failed"
    assert rows["//pkg:both_test#suite::s1"].status == "passed" and rows["//pkg:both_test#suite::s2"].status == "failed"
    f = rows["//pkg:flaky_test#suite::f"]
    assert (f.status, f.flaky, f.attempts, f.declared) == ("passed", True, 2, ("REQ-4",))
    # Attempts that declare different ids for one key: the union, fail-closed (multi-tag).
    assert set(rows["//pkg:flaky_test#suite::g"].declared) == {"REQ-5", "REQ-6"}
    # Execution dimensions are not identity: one key per case.
    assert sorted(k for k in rows if k.startswith("//pkg:flaky_test#")) == [
        "//pkg:flaky_test#suite::f",
        "//pkg:flaky_test#suite::g",
    ]
