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


def test_workspace_relative_file():
    assert workspace_relative("/x/bazel-out/k8-fastbuild/bin/pi/t.runfiles/_main/pi/tests/test_a.py") == (
        "pi/tests/test_a.py"
    )
    assert workspace_relative("bazel-out/k8-opt/bin/web/dist-test/a.test.js") == "web/dist-test/a.test.js"
    assert workspace_relative("pi/tests/test_a.py") == "pi/tests/test_a.py"
    case = TestCase("t", "passed", properties={"rr.file": "/r/x.runfiles/ws/pkg/test_a.py"})
    assert file_of(case) == "pkg/test_a.py"
    assert file_of(TestCase("t", "passed")) == ""


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
