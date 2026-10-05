# SPDX-License-Identifier: AGPL-3.0-or-later
"""Case identity: keys, paths, execution dimensions and folding by key."""

import pytest
from conftest import junit, write

from rules_requirements import ingest
from rules_requirements.case_keys import (
    SYNTHETIC_PATH,
    CaseKey,
    RunDims,
    case_path,
    file_of,
    index_cases,
    is_synthetic,
    is_unscoped,
    key_of,
    name_tags,
    pseudo_target,
    run_dims_from_path,
    target_of,
    workspace_relative,
)
from rules_requirements.ingest import TestCase


def test_case_key_string_form_and_parse():
    key = CaseKey("//web:clocksync_test", "clocksync::offset # of samples > 3")
    assert str(key) == "//web:clocksync_test#clocksync::offset # of samples > 3"
    # The first '#' separates: the path keeps any later one.
    assert CaseKey.parse(str(key)) == key
    assert CaseKey("//a:b", "x") < CaseKey("//a:b", "y") < CaseKey("//a:c", "a")
    assert CaseKey("//a:b", SYNTHETIC_PATH).synthetic
    for bad in ("//a:b", "#x", "//a:b#"):
        with pytest.raises(ValueError, match="not a case key"):
            CaseKey.parse(bad)


def test_case_path_is_canonical():
    assert case_path("pkg.mod", "test_x[a-b]") == "pkg.mod::test_x[a-b]"
    assert case_path("", "  lone  ") == "lone"
    # NFC: a decomposed é and a composed one are the same case.
    assert case_path("", "café") == case_path("", "café")
    # Internal whitespace and '::' / ' > ' stay; nothing is split again.
    assert case_path("probe > parent", "a  b::c") == "probe > parent::a  b::c"


def test_name_tags_never_rename_a_case():
    assert case_path("go", "TestX/sub [rr:PR-1] ok") == case_path("go", "TestX/sub ok") == "go::TestX/sub ok"
    assert case_path("", "[rr:PR-1] starts") == "starts"
    assert case_path("", "ends [rr:PR-1]") == "ends"
    assert name_tags("a [rr:PR-1] b [rr:PR-2, PR-3]") == ["PR-1", "PR-2", "PR-3"]
    assert name_tags("no tags [here]") == []


def test_pseudo_targets_carry_no_separator():
    assert pseudo_target("suite:", "a#b ") == "suite:a_b"
    assert pseudo_target("record:", "") == "record:unnamed"
    assert is_unscoped("suite:x") and not is_unscoped("record:x") and not is_unscoped("//a:b")


def test_target_of_each_kind_of_evidence():
    assert target_of(TestCase("t", "passed", target="//a:b")) == "//a:b"
    assert target_of(TestCase("t", "passed", source="ev/panel_inspection.rr.yaml")) == "record:panel_inspection"
    assert target_of(TestCase("t", "passed", source="out/report.xml", suite="Codec")) == "suite:Codec"
    assert target_of(TestCase("t", "passed", source="out/report.xml")) == "suite:report"


def test_synthetic_result_is_the_target():
    case = TestCase("web/x_test_/x_test", "passed", target="//web:x_test", properties={"rr.synthetic": "true"})
    assert is_synthetic(case)
    assert key_of(case) == CaseKey("//web:x_test", SYNTHETIC_PATH)
    assert key_of(TestCase("t", "passed", classname="c", target="//a:b")) == CaseKey("//a:b", "c::t")


def test_run_dims_from_path():
    assert run_dims_from_path("bazel-testlogs/p/t/test.xml") == RunDims()
    dims = run_dims_from_path("bazel-testlogs/p/t/run_2_of_3/shard_1_of_4/test_attempts/attempt_1.xml")
    assert dims == RunDims(shard=1, shards=4, run=2, runs=3, attempt=1)
    assert run_dims_from_path("p/t/shard_2_of_2/test.xml").attempt == 0
    # A sharded test run several times: Bazel puts both in one directory name.
    dims = run_dims_from_path("bazel-testlogs/p/t/shard_1_of_2_run_3_of_4/test_attempts/attempt_2.xml")
    assert dims == RunDims(shard=1, shards=2, run=3, runs=4, attempt=2)
    assert run_dims_from_path("p/t/shard_1_of_2run_3_of_4/test.xml") == RunDims()


def test_index_cases_sharded_and_repeated(tmp_path):
    """shard_i_of_n_run_k_of_m: one key in two shards is a duplicate; its runs are one slot each."""
    t = "bazel-testlogs/a/shr_test"
    for run in (1, 2):
        _report(
            tmp_path, f"{t}/shard_1_of_2_run_{run}_of_2/test.xml", [("both", "passed", [], ""), ("a", "passed", [], "")]
        )
        _report(tmp_path, f"{t}/shard_2_of_2_run_{run}_of_2/test.xml", [("both", "passed", [], "")])
    _report(tmp_path, f"{t}/shard_1_of_2_run_1_of_2/test_attempts/attempt_1.xml", [("a", "failed", [], "")])
    rows = index_cases(ingest.collect([str(tmp_path)]))
    assert set(rows) == {CaseKey("//a:shr_test", "suite::both"), CaseKey("//a:shr_test", "suite::a")}
    assert rows[CaseKey("//a:shr_test", "suite::both")].duplicate
    a = rows[CaseKey("//a:shr_test", "suite::a")]
    assert not a.duplicate and (a.status, a.flaky, a.attempts) == ("passed", True, 2)


def test_workspace_relative_file():
    assert workspace_relative("/x/bazel-out/k8-fastbuild/bin/pi/t.runfiles/_main/pi/tests/test_a.py") == (
        "pi/tests/test_a.py"
    )
    assert workspace_relative("bazel-out/k8-opt/bin/web/dist-test/a.test.js") == "web/dist-test/a.test.js"
    assert workspace_relative("pi/tests/test_a.py") == "pi/tests/test_a.py"
    case = TestCase("t", "passed", properties={"rr.file": "/r/x.runfiles/ws/pkg/test_a.py"})
    assert file_of(case) == "pkg/test_a.py"
    assert file_of(TestCase("t", "passed")) == ""


@pytest.mark.parametrize(
    "spelling",
    ["pi/h/fx_bench.py", "./pi/h/fx_bench.py", "pi/h//fx_bench.py", "pi/h/../h/fx_bench.py", "pi\\h\\fx_bench.py",
     "/home/ci/splanc/pi/h/fx_bench.py", "/home/ci/splanc//./pi/h/fx_bench.py"],
)  # fmt: skip
def test_workspace_relative_gives_one_spelling_per_source(spelling, monkeypatch):
    """One source file, one code identity (same-code detection compares it)."""
    monkeypatch.setenv("BUILD_WORKSPACE_DIRECTORY", "/home/ci/splanc/")
    assert workspace_relative(spelling) == "pi/h/fx_bench.py"
    assert file_of(TestCase("t", "passed", file=spelling)) == "pi/h/fx_bench.py"
    assert file_of(TestCase("t", "passed", properties={"rr.file": spelling})) == "pi/h/fx_bench.py"


def test_workspace_relative_keeps_what_it_cannot_place(monkeypatch):
    monkeypatch.delenv("BUILD_WORKSPACE_DIRECTORY", raising=False)
    assert workspace_relative("/home/ci/splanc/./pi/h/fx_bench.py") == "/home/ci/splanc/pi/h/fx_bench.py"
    assert workspace_relative("//abs/x.py") == "/abs/x.py"
    assert workspace_relative("../other/x.py") == "../other/x.py"
    assert workspace_relative("./") == workspace_relative("") == workspace_relative("  ") == ""


_BAZEL_GENERATED = """<?xml version="1.0" encoding="UTF-8"?>
<testsuites>
  <testsuite name="{suite}" tests="1" failures="0" errors="0">
    <testcase name="{case}" status="run" duration="0" time="0">{body}</testcase>
      <system-out>
{out} (if the file is not UTF-8, then this may be unreadable):
<![CDATA[ok]]>
      </system-out>
    </testsuite>
</testsuites>
"""


@pytest.mark.parametrize(
    ("suite", "case", "out", "body", "synthetic"),
    [
        ("web/x_test_/x_test", "web/x_test_/x_test", "Generated test.log", "", True),
        ("web/x_test_/x_test", "web/x_test_/x_test", "Generated test.log", '<failure message="exit 1"/>', True),
        ("web/x_test_/x_test", "other", "Generated test.log", "", False),
        ("web/x_test_/x_test", "web/x_test_/x_test", "Captured output", "", False),
    ],
)
def test_bazel_generated_fingerprint(tmp_path, suite, case, out, body, synthetic):
    path = write(
        tmp_path,
        "bazel-testlogs/web/x_test/test.xml",
        _BAZEL_GENERATED.format(suite=suite, case=case, out=out, body=body),
    )
    (only,) = ingest.collect([path]).cases
    assert is_synthetic(only) is synthetic
    assert only.suite == suite
    assert (key_of(only).path == SYNTHETIC_PATH) is synthetic


def test_ingest_records_the_enclosing_suite(tmp_path):
    path = junit(tmp_path, "r.xml", [("t", "passed", ["REQ-1"], "")])
    (case,) = ingest.collect([path]).cases
    assert case.suite == "s"
    assert key_of(case) == CaseKey("suite:s", "suite::t")


def test_test_case_positional_fields_are_unchanged():
    """Third-party ingestors may build TestCase positionally up to properties;
    fields added in v0.2 come after it."""
    case = TestCase("n", "passed", "cls", ("R-1",), "unit", {}, "", 0.5, "r.xml", "//a:b", {"k": "v"})
    assert case.properties == {"k": "v"} and case.target == "//a:b" and case.suite == ""


def _report(tmp_path, rel, cases):
    return junit(tmp_path, rel, cases)


def test_index_cases_folds_attempts_runs_shards_and_roots(tmp_path):
    t = "bazel-testlogs/pkg/t"
    _report(tmp_path, f"{t}/test.xml", [("ok", "passed", ["REQ-1"], ""), ("retry", "passed", ["REQ-2"], "")])
    _report(tmp_path, f"{t}/test_attempts/attempt_1.xml", [("retry", "failed", ["REQ-2"], "")])
    r = "bazel-testlogs/pkg/r"
    _report(tmp_path, f"{r}/run_1_of_2/test.xml", [("rep", "passed", [], "")])
    _report(tmp_path, f"{r}/run_2_of_2/test.xml", [("rep", "failed", [], "")])
    s = "bazel-testlogs/pkg/s"
    _report(tmp_path, f"{s}/shard_1_of_2/test.xml", [("a", "passed", [], ""), ("both", "passed", [], "")])
    _report(tmp_path, f"{s}/shard_2_of_2/test.xml", [("b", "passed", [], ""), ("both", "skipped", [], "")])
    _report(tmp_path, "hitl/bazel-testlogs/pkg/t/test.xml", [("ok", "error", ["REQ-3"], "")])
    _report(tmp_path, "bazel-testlogs/pkg/d/test.xml", [("dup", "passed", [], ""), ("dup", "failed", [], "")])
    rows = index_cases(ingest.collect([str(tmp_path)]))

    ok = rows[CaseKey("//pkg:t", "suite::ok")]
    assert ok.status == "error"  # two evidence roots: worst-of
    assert ok.declared == ("REQ-1", "REQ-3") and len(ok.sources) == 2
    retry = rows[CaseKey("//pkg:t", "suite::retry")]
    assert (retry.status, retry.flaky, retry.attempts) == ("passed", True, 2)
    assert rows[CaseKey("//pkg:r", "suite::rep")].status == "failed"  # every run must pass
    assert {k.path for k in rows if k.target == "//pkg:s"} == {"suite::a", "suite::b", "suite::both"}
    both = rows[CaseKey("//pkg:s", "suite::both")]
    assert both.duplicate and both.status == "skipped"
    assert not rows[CaseKey("//pkg:s", "suite::a")].duplicate
    dup = rows[CaseKey("//pkg:d", "suite::dup")]
    assert dup.duplicate and dup.status == "failed"
    # Sorted by key, one row each.
    assert list(rows) == sorted(rows, key=lambda k: (k.target, k.path))


def test_index_cases_without_a_final_report_uses_the_last_attempt(tmp_path):
    t = "bazel-testlogs/pkg/t/test_attempts"
    _report(tmp_path, f"{t}/attempt_1.xml", [("x", "failed", [], "")])
    _report(tmp_path, f"{t}/attempt_2.xml", [("x", "passed", [], "")])
    (row,) = index_cases(ingest.collect([str(tmp_path)]).cases).values()
    assert (row.status, row.flaky, row.attempts) == ("passed", True, 2)


def test_crashed_first_attempt_is_not_a_case(tmp_path):
    """attempt_1 crashed (Bazel's generated report: one [target] error); the
    final test.xml has per-case passes. The final report is authoritative:
    no phantom [target] row, and the passing cases are flaky."""
    t = "bazel-testlogs/a/flaky_test"
    write(
        tmp_path,
        f"{t}/test_attempts/attempt_1.xml",
        _BAZEL_GENERATED.format(
            suite="a/flaky_test", case="a/flaky_test", out="Generated test.log", body='<error message="SIGSEGV"/>'
        ),
    )
    _report(tmp_path, f"{t}/test.xml", [("t1", "passed", [], ""), ("t2", "passed", [], "")])
    rows = index_cases(ingest.collect([str(tmp_path)]))
    assert set(rows) == {CaseKey("//a:flaky_test", "suite::t1"), CaseKey("//a:flaky_test", "suite::t2")}
    for row in rows.values():
        assert (row.status, row.flaky, row.attempts) == ("passed", True, 2)
    # A key only in earlier attempts of another run (no final report there) still counts.
    _report(tmp_path, "bazel-testlogs/a/other_test/test_attempts/attempt_1.xml", [("x", "failed", [], "")])
    assert index_cases(ingest.collect([str(tmp_path)]))[CaseKey("//a:other_test", "suite::x")].status == "failed"


def test_unnamed_case_gets_a_placeholder_path(tmp_path):
    assert case_path("  ", " ") == "[unnamed]"
    key = CaseKey("//a:esc_test", case_path("", ""))
    assert CaseKey.parse(str(key)) == key


def test_case_row_to_dict():
    case = TestCase(
        "t",
        "failed",
        classname="c",
        requirements=("REQ-1",),
        level="hil",
        message="boom\ntrace",
        source="bazel-testlogs/p/t/test.xml",
        target="//p:t",
        properties={"rr.file": "p/test_t.py", "rr.scope": "target"},
    )
    (row,) = index_cases([case]).values()
    assert row.to_dict() == {
        "case": "//p:t#c::t",
        "target": "//p:t",
        "path": "c::t",
        "status": "failed",
        "declared": ["REQ-1"],
        "level": "hil",
        "target_scope": True,
        "file": "p/test_t.py",
        "message": "boom",
        "sources": ["bazel-testlogs/p/t/test.xml"],
    }


def test_targets_named_like_run_levels_keep_their_own_keys(tmp_path):
    """//web:run_smoke_test and //web:run_full_test are two targets, not one //:web."""
    _report(tmp_path, "bazel-testlogs/web/run_smoke_test/test.xml", [("boots", "passed", ["REQ-1"], "")])
    _report(tmp_path, "bazel-testlogs/web/run_full_test/test.xml", [("boots", "failed", ["REQ-2"], "")])
    rows = index_cases(ingest.collect([str(tmp_path)]))
    assert set(rows) == {
        CaseKey("//web:run_smoke_test", "suite::boots"),
        CaseKey("//web:run_full_test", "suite::boots"),
    }
    assert not any(r.duplicate for r in rows.values())


def test_each_shards_generated_result_is_not_a_duplicate(tmp_path):
    """A sharded target that writes no JUnit: Bazel generates a [target]
    result per shard; that is one case, folded worst-of, not a duplicate."""
    t = "bazel-testlogs/app/sharded_smoke_test"
    for shard, body in ((1, ""), (2, '<failure message="exit 1"/>')):
        write(
            tmp_path,
            f"{t}/shard_{shard}_of_2/test.xml",
            _BAZEL_GENERATED.format(
                suite="app/sharded_smoke_test", case="app/sharded_smoke_test", out="Generated test.log", body=body
            ),
        )
    (row,) = index_cases(ingest.collect([str(tmp_path)])).values()
    assert row.key == CaseKey("//app:sharded_smoke_test", SYNTHETIC_PATH)
    assert row.synthetic and not row.duplicate and row.status == "failed"


def test_testcase_file_attribute_is_the_source_when_no_rr_file_says(tmp_path):
    """rr_case.h and googletest write the source as the <testcase>'s file attribute."""
    write(
        tmp_path,
        "bazel-testlogs/fw/codec_test/test.xml",
        '<testsuites><testsuite name="codec">'
        '<testcase classname="codec" name="round_trip" file="fw/codec_test.cc" line="3"/>'
        '<testcase classname="codec" name="own" file="fw/codec_test.cc">'
        '<properties><property name="rr.file" value="fw/other.cc"/></properties></testcase>'
        '<testcase classname="codec" name="none"/>'
        "</testsuite></testsuites>",
    )
    rows = {k.path: r for k, r in index_cases(ingest.collect([str(tmp_path)])).items()}
    assert rows["codec::round_trip"].file == "fw/codec_test.cc"
    assert rows["codec::own"].file == "fw/other.cc"
    assert rows["codec::none"].file == ""
