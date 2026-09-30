# SPDX-License-Identifier: AGPL-3.0-or-later
import pytest
from conftest import MODEL, junit, write

from rules_requirements import ingest
from rules_requirements.config import Config
from rules_requirements.model import load_model
from rules_requirements.trace import (
    FAILED,
    MITIGATED,
    OPEN,
    PARTIAL,
    UNDER_VERIFIED,
    UNVALIDATED,
    UNVERIFIED,
    VALIDATED,
    VERIFIED,
    build_matrix,
    classify,
    is_stale,
    route_for,
)


def matrix_for(tmp_path, model, cases, **kw):
    path = junit(tmp_path, "bazel-testlogs/pkg/t/test.xml", cases)
    return build_matrix(model, ingest.collect([path]), **kw)


@pytest.mark.parametrize(
    "levels, failed, demanded, expected",
    [
        ([], False, "simulation", (UNVERIFIED, "")),
        (["simulation"], True, "simulation", (FAILED, "")),
        (["simulation", "hil"], False, "hil", (VERIFIED, "hil")),
        (["hitl"], False, "hil", (VERIFIED, "hitl")),
        (["analysis"], False, "hil", (UNDER_VERIFIED, "analysis")),
        (["inspection"], False, "inspection", (VERIFIED, "inspection")),
        (["hitl"], False, "inspection", (UNDER_VERIFIED, "hitl")),
        (["inspection"], False, "analysis", (UNDER_VERIFIED, "inspection")),
        (["inspection"], False, "", (VERIFIED, "inspection")),
    ],
)
def test_classify(levels, failed, demanded, expected):
    assert classify(levels, failed, demanded, Config()) == expected


def test_is_stale():
    assert not is_stale({}, {"sha": "a"})
    assert not is_stale({"sha": "a"}, None)
    assert not is_stale({"sha": "a"}, {"sha": "a", "other": "x"})
    assert not is_stale({"board": "C"}, {"sha": "a"})
    assert is_stale({"sha": "old"}, {"sha": "new"})


def test_route_for():
    c = Config()
    assert route_for("simulation", c) == "autonomous"
    assert route_for("sil", c) == "autonomous"
    assert route_for("hil", c) == "human-gate"
    assert route_for("inspection", c) == "human-gate"


def test_rollups_all_green(tmp_path, model):
    m = matrix_for(
        tmp_path,
        model,
        [
            ("heat", "passed", ["REQ-1"], ""),
            ("range", "passed", ["REQ-2"], "sil"),
            ("cutoff_sim", "passed", ["REQ-3"], "simulation"),
            ("cutoff_bench", "passed", ["REQ-3"], "hil"),
        ],
    )
    assert m.status("REQ-3") == VERIFIED
    assert m.verdicts["REQ-3"].provided == "hil"
    assert m.status("UN-1") == VALIDATED and m.status("UN-2") == VALIDATED
    assert m.status("MIT-1") == VERIFIED and m.status("RISK-1") == MITIGATED
    assert m.status("TM-1") == VERIFIED
    assert m.gaps == [] and m.high_open_risks() == [] and m.pyramid_violations() == []
    assert m.module_status() == {"controller": VERIFIED, "interlock": VERIFIED}
    counts = m.counts()
    assert counts["requirements_verified"] == 3 and counts["risks_mitigated"] == 1 and counts["gaps"] == 0


def test_gaps_under_verified_failed_unverified(tmp_path, model):
    m = matrix_for(
        tmp_path,
        model,
        [
            ("heat", "failed", ["REQ-1"], ""),
            ("cutoff_sim", "passed", ["REQ-3"], ""),
            ("ghost", "passed", ["REQ-99"], ""),
            ("skip", "skipped", ["REQ-2"], ""),
        ],
    )
    assert m.status("REQ-1") == FAILED
    assert m.status("UN-1") == FAILED
    assert m.status("REQ-2") == UNVERIFIED and m.status("UN-2") == UNVALIDATED
    assert m.status("REQ-3") == UNDER_VERIFIED
    assert m.status("MIT-1") == PARTIAL and m.status("RISK-1") == PARTIAL
    kinds = {(g.kind, g.entity, g.route) for g in m.gaps}
    assert ("failed", "REQ-1", "autonomous") in kinds
    assert ("unverified", "REQ-2", "autonomous") in kinds
    assert ("under-verified", "REQ-3", "human-gate") in kinds
    assert ("high-risk-open", "RISK-1", "human-gate") in kinds
    assert ("unknown-id", "REQ-99", "autonomous") in kinds
    assert m.unknown_evidence == {"REQ-99": ["//pkg:t suite::ghost"]}
    assert m.module_status()["controller"] == FAILED


def test_pyramid_violation(tmp_path, model):
    m = matrix_for(tmp_path, model, [("bench", "passed", ["REQ-3"], "hil")])
    assert m.status("REQ-3") == VERIFIED
    assert m.pyramid_violations() == ["REQ-3"]
    assert any(g.kind == "pyramid" for g in m.gaps)


def test_staleness(tmp_path, model):
    cases = [
        ("bench", "passed", ["REQ-3"], "hil", [("artifact.sha", "old")]),
        ("sim", "passed", ["REQ-3"], "simulation", [("artifact.sha", "old")]),
    ]
    fresh = matrix_for(tmp_path, model, cases, current_build={"sha": "old"})
    assert fresh.status("REQ-3") == VERIFIED and not fresh.verdicts["REQ-3"].stale
    stale = matrix_for(tmp_path, model, cases, current_build={"sha": "new"})
    v = stale.verdicts["REQ-3"]
    assert v.status == UNDER_VERIFIED and v.stale and v.provided == "hil"
    assert [g.kind for g in stale.gaps if g.entity == "REQ-3"] == ["stale"]
    mixed = matrix_for(
        tmp_path,
        model,
        cases + [("sim2", "passed", ["REQ-3"], "simulation", [("artifact.sha", "new")])],
        current_build={"sha": "new"},
    )
    assert mixed.status("REQ-3") == UNDER_VERIFIED and not mixed.verdicts["REQ-3"].stale


def test_verified_by_targets(tmp_path):
    path = write(
        tmp_path,
        "m.yaml",
        """
        user_needs: [{id: UN-1, title: n}]
        requirements:
          - {id: REQ-1, title: r, satisfies: [UN-1], method: hil, verified_by: ["//pkg:t", {target: "//pkg:hw", level: hil}, "//pkg:missing"]}
        """,
    )
    model = load_model(path)
    junit(tmp_path, "bazel-testlogs/pkg/t/test.xml", [("a", "passed", [], "")])
    junit(tmp_path, "bazel-testlogs/pkg/hw/test.xml", [("b", "passed", [], "")])
    m = build_matrix(model, ingest.collect([str(tmp_path / "bazel-testlogs")]))
    v = m.verdicts["REQ-1"]
    assert v.status == VERIFIED and v.provided == "hil"
    assert [(e.name, e.kind, e.level) for e in v.evidence] == [
        ("//pkg:hw", "target", "hil"),
        ("//pkg:t", "target", "simulation"),
    ]
    assert not v.pyramid_violation


def test_refinement_rollup(tmp_path):
    path = write(
        tmp_path,
        "m.yaml",
        """
        user_needs: [{id: UN-1, title: n}]
        requirements:
          - {id: REQ-1, title: system, satisfies: [UN-1]}
          - {id: REQ-2, title: sw a, refines: [REQ-1]}
          - {id: REQ-3, title: sw b, refines: [REQ-1]}
          - {id: REQ-4, title: parent with own evidence, satisfies: [UN-1]}
          - {id: REQ-5, title: child, refines: [REQ-4]}
        """,
    )
    model = load_model(path)
    only_a = matrix_for(tmp_path, model, [("a", "passed", ["REQ-2"], ""), ("p", "passed", ["REQ-4"], "")])
    assert only_a.status("REQ-1") == PARTIAL
    assert only_a.status("REQ-4") == PARTIAL  # own evidence ok, child unverified
    both = matrix_for(
        tmp_path,
        model,
        [(n, "passed", [r], "") for n, r in (("a", "REQ-2"), ("b", "REQ-3"), ("p", "REQ-4"), ("c", "REQ-5"))],
    )
    assert both.status("REQ-1") == VERIFIED and both.status("REQ-4") == VERIFIED
    assert both.status("UN-1") == VALIDATED
    assert not any(g.entity == "REQ-1" for g in both.gaps)
    bad_child = matrix_for(tmp_path, model, [("p", "passed", ["REQ-4"], ""), ("c", "failed", ["REQ-5"], "")])
    assert bad_child.status("REQ-4") == FAILED
    partial = [g for g in only_a.gaps if g.entity == "REQ-1"]
    assert [g.kind for g in partial] == ["partial"]


def test_direct_validation_evidence_on_needs(tmp_path, model):
    m = matrix_for(
        tmp_path,
        model,
        [("heat", "passed", ["REQ-1"], ""), ("usability", "failed", ["UN-1"], "inspection")],
    )
    assert m.status("UN-1") == FAILED
    assert [e.name for e in m.verdicts["UN-1"].evidence] == ["suite::usability"]


def test_risk_without_mitigation_is_open(tmp_path):
    path = write(
        tmp_path,
        "m.yaml",
        "config: {rules: {risk-unmitigated: warning}}\nrisks: [{id: RISK-1, title: r, severity: critical}]\n",
    )
    m = build_matrix(load_model(path), ingest.Evidence())
    assert m.status("RISK-1") == OPEN
    assert m.high_open_risks() == ["RISK-1"]


def test_annotations_feed_implementation_links(tmp_path, model):
    from rules_requirements.annotations import Reference

    refs = [
        Reference(("REQ-1",), "implements", "src/ctl.py", 3, symbol="def heat"),
        Reference(("REQ-1",), "verifies", "tests/test_ctl.py", 9),
        Reference(("REQ-404",), "implements", "src/x.py", 1),
    ]
    m = build_matrix(model, ingest.Evidence(), references=refs)
    assert [r.path for r in m.verdicts["REQ-1"].implemented_in] == ["src/ctl.py"]
    assert [r.path for r in m.verdicts["REQ-1"].verified_in] == ["tests/test_ctl.py"]
    no_impl = {g.entity for g in m.gaps if g.kind == "no-implementation"}
    assert no_impl == {"REQ-2", "REQ-3"}


def test_model_text_is_reused(tmp_path):
    assert "REQ-3" in MODEL
