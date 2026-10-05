# SPDX-License-Identifier: AGPL-3.0-or-later
"""Verdicts over verification sets: trace.build_matrix reads the attribution only.

Each requirement is verified by its set — the cases it owns, the cases its
literal selectors and the lock expect, and the quarantined cases naming it —
and :func:`~rules_requirements.trace.verdict_from_members` applies the first
rule that holds: INVALID, FAILED, UNVERIFIED, INCOMPLETE, UNDER-VERIFIED,
VERIFIED.
"""

import pytest
from conftest import MODEL, junit, write

from rules_requirements import ingest
from rules_requirements.attribution import CaseKey, CaseResult, Member
from rules_requirements.config import Config
from rules_requirements.ingest import Evidence, TestCase
from rules_requirements.model import load_model, read_model
from rules_requirements.trace import (
    FAILED,
    INCOMPLETE,
    INVALID,
    MITIGATED,
    OPEN,
    PARTIAL,
    UNDER_VERIFIED,
    UNVERIFIED,
    VALIDATED,
    VERIFIED,
    build_matrix,
    classify,
    find_gaps,
    is_stale,
    route_for,
    verdict_from_members,
)


def matrix_for(tmp_path, model, cases, **kw):
    path = junit(tmp_path, "bazel-testlogs/pkg/t/test.xml", cases)
    return build_matrix(model, ingest.collect([path]), **kw)


def matrix_of_testlogs(tmp_path, model, cases, **kw):
    """Like matrix_for, over the whole testlogs tree (test.xml plus its test_attempts)."""
    junit(tmp_path, "bazel-testlogs/pkg/t/test.xml", cases)
    return build_matrix(model, ingest.collect([str(tmp_path / "bazel-testlogs")]), **kw)


def kinds(m, entity=None):
    return [g.kind for g in m.gaps if entity is None or g.entity == entity]


def model_text(tmp_path, text, name="m.yaml"):
    model, _ = read_model(write(tmp_path, name, text))
    assert not model.parse_errors, model.parse_errors
    return model


def member(state, level="simulation", *, stale=False, flaky=False, artifact=None, key="c::a", target="//t:t"):
    k = CaseKey(target, key)
    result = None
    if state in ("passed", "failed", "error", "skipped"):
        stamp = dict(artifact or {})
        result = CaseResult(k, state, artifact=stamp, artifacts=(stamp,) if stamp else ())
    return Member("REQ-1", k, "*", "model", state, level, stale, flaky, result, target)


# --------------------------------------------------------------------------- #
# Classification helpers (unchanged)                                          #
# --------------------------------------------------------------------------- #


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


# --------------------------------------------------------------------------- #
# verdict_from_members: the first rule that holds                             #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "states, expected",
    [
        ([], UNVERIFIED),
        (["not-run", "not-run"], UNVERIFIED),
        (["passed", "passed"], VERIFIED),
        (["passed", "quarantined", "failed"], INVALID),  # quarantine beats everything
        (["passed", "error", "missing"], FAILED),  # a failure beats an incomplete set
        (["passed", "missing"], INCOMPLETE),
        (["passed", "not-run"], INCOMPLETE),
        (["passed", "skipped"], INCOMPLETE),
        (["passed", "moved"], INCOMPLETE),
        (["not-run", "skipped"], INCOMPLETE),
    ],
)
def test_verdict_from_members_rules(states, expected):
    assert verdict_from_members([member(s) for s in states], "simulation", Config()).status == expected


def test_incomplete_provides_the_best_level_so_far_and_verified_the_best_of_the_set():
    c = Config()
    incomplete = verdict_from_members([member("passed", "hil"), member("missing")], "hil", c)
    assert (incomplete.status, incomplete.provided) == (INCOMPLETE, "hil")
    verified = verdict_from_members([member("passed", "simulation"), member("passed", "hitl")], "hil", c)
    assert (verified.status, verified.provided) == (VERIFIED, "hitl")
    low = verdict_from_members([member("passed", "simulation")], "hil", c)
    assert (low.status, low.provided, low.reasons) == (UNDER_VERIFIED, "simulation", ("level",))


@pytest.mark.parametrize(
    "policy, expected, reasons",
    [
        ("accept", VERIFIED, ()),
        ("flag", VERIFIED, ()),
        ("under-verify", UNDER_VERIFIED, ("flaky",)),
        ("fail", FAILED, ()),
    ],
)
def test_flaky_policies(policy, expected, reasons):
    got = verdict_from_members([member("passed", flaky=True), member("passed")], "simulation", Config(flaky=policy))
    assert (got.status, got.reasons, got.flaky) == (expected, reasons, True)


def test_one_stale_member_under_verifies_the_whole_set():
    # Stricter than 0.2's "fresh evidence wins": the set holds on the current build or not at all.
    got = verdict_from_members([member("passed", stale=True), member("passed")], "simulation", Config())
    assert (got.status, got.stale, got.reasons) == (UNDER_VERIFIED, True, ("stale",))


@pytest.mark.parametrize("policy, expected", [("off", VERIFIED), ("warn", VERIFIED), ("enforce", INCOMPLETE)])
def test_set_consistency(policy, expected):
    ms = [member("passed", artifact={"dut": "a"}, key="c::a"), member("passed", artifact={"dut": "b"}, key="c::b")]
    got = verdict_from_members(
        [*ms, member("passed", key="c::unstamped")], "simulation", Config(set_consistency=policy)
    )
    assert got.status == expected
    assert got.mixed_builds == (() if policy == "off" else ("dut",))


# --------------------------------------------------------------------------- #
# The matrix                                                                  #
# --------------------------------------------------------------------------- #


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
    assert m.verdicts["REQ-3"].provided == "hil" and m.verdicts["REQ-3"].basis == "own"
    assert m.status("UN-1") == VALIDATED and m.status("UN-2") == VALIDATED
    assert m.status("MIT-1") == VERIFIED and m.status("RISK-1") == MITIGATED
    assert m.status("TM-1") == VERIFIED
    # Tag-owned sets (hybrid mode) without a lock: the one gap is that they are not pinned.
    assert kinds(m) == ["unpinned-sets"] and m.gaps[0].entity == ""
    assert "REQ-1, REQ-2, REQ-3" in m.gaps[0].message
    assert m.high_open_risks() == [] and m.pyramid_violations() == []
    assert m.module_status() == {"controller": VERIFIED, "interlock": VERIFIED}
    counts = m.counts()
    assert counts["requirements_verified"] == 3 and counts["risks_mitigated"] == 1 and counts["gaps"] == 1
    assert counts["requirements_incomplete"] == 0 and counts["requirements_invalid"] == 0
    assert list(counts)[7:10] == ["requirements_unverified", "requirements_incomplete", "requirements_invalid"]


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
    # A skipped member is no pass: the set is INCOMPLETE (0.2: UNVERIFIED).
    assert m.status("REQ-2") == INCOMPLETE and m.status("UN-2") == PARTIAL
    assert m.status("REQ-3") == UNDER_VERIFIED
    assert m.status("MIT-1") == PARTIAL and m.status("RISK-1") == PARTIAL
    found = {(g.kind, g.entity, g.route) for g in m.gaps}
    assert ("failed", "REQ-1", "autonomous") in found
    assert ("incomplete", "REQ-2", "autonomous") in found
    assert ("under-verified", "REQ-3", "human-gate") in found
    assert ("high-risk-open", "RISK-1", "human-gate") in found
    assert ("unknown-id", "REQ-99", "autonomous") in found
    assert m.unknown_evidence == {"REQ-99": ["//pkg:t#suite::ghost"]}
    assert m.module_status()["controller"] == FAILED
    assert m.counts()["requirements_incomplete"] == 1


def test_pyramid_violation(tmp_path, model):
    m = matrix_for(tmp_path, model, [("bench", "passed", ["REQ-3"], "hil")])
    assert m.status("REQ-3") == VERIFIED
    assert m.pyramid_violations() == ["REQ-3"]
    assert "pyramid" in kinds(m)


def test_staleness(tmp_path, model):
    cases = [
        ("bench", "passed", ["REQ-3"], "hil", [("artifact.sha", "old")]),
        ("sim", "passed", ["REQ-3"], "simulation", [("artifact.sha", "old")]),
    ]
    fresh = matrix_for(tmp_path, model, cases, current_build={"sha": "old"})
    assert fresh.status("REQ-3") == VERIFIED and not fresh.verdicts["REQ-3"].stale
    stale = matrix_for(tmp_path, model, cases, current_build={"sha": "new"})
    v = stale.verdicts["REQ-3"]
    assert v.status == UNDER_VERIFIED and v.stale and v.provided == "hil" and v.reasons == ("stale",)
    assert kinds(stale, "REQ-3") == ["stale"]
    # 0.2 let fresh evidence win; a set must now hold on the current build as a whole.
    mixed = matrix_for(
        tmp_path,
        model,
        cases + [("sim2", "passed", ["REQ-3"], "simulation", [("artifact.sha", "new")])],
        current_build={"sha": "new"},
    )
    v = mixed.verdicts["REQ-3"]
    assert v.status == UNDER_VERIFIED and v.stale and v.mixed_builds == ("sha",)
    assert kinds(mixed, "REQ-3") == ["stale", "mixed-builds"]


def test_whole_target_claims_expand_to_their_cases(tmp_path):
    path = write(
        tmp_path,
        "m.yaml",
        """
        user_needs: [{id: UN-1, title: n}]
        requirements:
          - {id: REQ-1, title: r, satisfies: [UN-1], method: hil, verified_by: ["//pkg:t", {target: "//pkg:hw", level: hil}]}
          - {id: REQ-2, title: r, satisfies: [UN-1], verified_by: ["//pkg:missing"]}
        """,
    )
    model = load_model(path)
    junit(tmp_path, "bazel-testlogs/pkg/t/test.xml", [("a", "passed", [], "")])
    junit(tmp_path, "bazel-testlogs/pkg/hw/test.xml", [("b", "passed", [], "")])
    m = build_matrix(model, ingest.collect([str(tmp_path / "bazel-testlogs")]))
    v = m.verdicts["REQ-1"]
    assert v.status == VERIFIED and v.provided == "hil"
    # 0.2's per-target refs ("kind": "target") are gone: each case is a member.
    assert [(e.name, e.target, e.kind, e.level) for e in v.evidence] == [
        ("suite::b", "//pkg:hw", "case", "hil"),
        ("suite::a", "//pkg:t", "case", "simulation"),
    ]
    assert [(m_.selector, m_.via) for m_ in v.members] == [("*whole*", "model")] * 2
    assert not v.pyramid_violation
    # A claimed target that did not run: not-run, so UNVERIFIED with the target named.
    assert m.status("REQ-2") == UNVERIFIED
    (gap,) = [g for g in m.gaps if g.entity == "REQ-2"]
    assert (gap.kind, gap.message) == ("unverified", "not run: //pkg:missing")


def test_claims_on_external_targets_match_their_testlogs(tmp_path):
    # Claims are normalized at parse; bazel-testlogs/external/<repo>~|+/ paths
    # must be looked up in the same spelling (one passing case each).
    path = write(
        tmp_path,
        "m.yaml",
        """
        user_needs: [{id: UN-1, title: n}]
        requirements:
          - {id: REQ-1, title: r, satisfies: [UN-1], verified_by: ["@foo~//p:n"]}
          - {id: REQ-2, title: r, satisfies: [UN-1], verified_by: [{target: "@bar//q:m", whole: true}]}
          - {id: REQ-3, title: r, satisfies: [UN-1], verified_by: ["@@baz+//r:s"]}
        """,
    )
    model = load_model(path)
    junit(tmp_path, "bazel-testlogs/external/foo~/p/n/test.xml", [("a", "passed", [], "")])
    junit(tmp_path, "bazel-testlogs/external/bar+/q/m/test.xml", [("b", "passed", [], "")])
    junit(tmp_path, "bazel-testlogs/external/baz~/r/s/test.xml", [("c", "failed", [], "")])
    m = build_matrix(model, ingest.collect([str(tmp_path / "bazel-testlogs")]))
    assert [m.verdicts[r].status for r in ("REQ-1", "REQ-2", "REQ-3")] == [VERIFIED, VERIFIED, FAILED]
    assert [e.target for e in m.verdicts["REQ-1"].evidence] == ["@foo//p:n"]
    # the failure is REQ-3's, not an unattributed one
    assert [g.kind for g in find_gaps(m) if "failure" in g.kind] == []


def test_refinement_rollup_with_basis(tmp_path):
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
    assert (only_a.verdicts["REQ-1"].basis, only_a.verdicts["REQ-1"].derived_from) == ("derived", ["REQ-2", "REQ-3"])
    assert (only_a.verdicts["REQ-4"].basis, only_a.verdicts["REQ-4"].derived_from) == ("own+derived", ["REQ-5"])
    both = matrix_for(
        tmp_path,
        model,
        [(n, "passed", [r], "") for n, r in (("a", "REQ-2"), ("b", "REQ-3"), ("p", "REQ-4"), ("c", "REQ-5"))],
    )
    assert both.status("REQ-1") == VERIFIED and both.status("REQ-4") == VERIFIED
    assert both.status("UN-1") == VALIDATED and both.verdicts["UN-1"].derived_from == ["REQ-1", "REQ-4"]
    assert not any(g.entity == "REQ-1" for g in both.gaps)
    assert both.verdicts["REQ-1"].members == ()  # the children's cases are theirs, never the parent's
    bad_child = matrix_for(tmp_path, model, [("p", "passed", ["REQ-4"], ""), ("c", "failed", ["REQ-5"], "")])
    assert bad_child.status("REQ-4") == FAILED
    (failed,) = [g for g in bad_child.gaps if g.entity == "REQ-4"]
    assert (failed.kind, failed.message) == ("failed", "failing refinements: REQ-5")
    assert kinds(only_a, "REQ-1") == ["partial"]


def test_refinements_and_invalid_or_incomplete_children(tmp_path):
    text = (
        "user_needs: [{id: UN-1, title: n}]\nrequirements:\n"
        "  - {id: REQ-1, title: system, satisfies: [UN-1]}\n"
        "  - {id: REQ-2, title: a, refines: [REQ-1]}\n"
        "  - {id: REQ-3, title: b, refines: [REQ-1]}\n"
        "  - {id: REQ-4, title: c, satisfies: [UN-1], verified_by: [{target: //pkg:t, cases: ['suite::own*']}]}\n"
        "  - {id: REQ-5, title: d, refines: [REQ-4]}\n"
    )
    model = model_text(tmp_path, text)
    m = matrix_for(
        tmp_path,
        model,
        [
            ("multi", "passed", ["REQ-2", "REQ-3"], ""),  # quarantined: both children INVALID
            ("kid", "passed", ["REQ-5"], ""),
        ],
    )
    # INVALID rolls up like FAILED (the parent fails, it is not itself named).
    assert [m.status(r) for r in ("REQ-2", "REQ-3", "REQ-1", "UN-1")] == [INVALID, INVALID, FAILED, FAILED]
    # A parent with its own claims needs its own set complete too.
    assert m.status("REQ-5") == VERIFIED and m.status("REQ-4") == INCOMPLETE
    assert m.verdicts["REQ-4"].basis == "own+derived"
    partly = matrix_for(tmp_path, model, [("kid", "skipped", ["REQ-5"], ""), ("own1", "passed", [], "")])
    assert partly.status("REQ-5") == INCOMPLETE and partly.status("REQ-4") == PARTIAL  # INCOMPLETE rolls up as PARTIAL


def test_invalid_entities_and_per_key_gaps(tmp_path, model):
    m = matrix_for(tmp_path, model, [("both", "passed", ["REQ-1", "REQ-2"], ""), ("heat", "passed", ["REQ-1"], "")])
    assert m.status("REQ-1") == INVALID and m.status("REQ-2") == INVALID
    assert m.status("UN-1") == FAILED and m.status("UN-2") == FAILED  # INVALID rolls up like FAILED
    assert m.counts()["requirements_invalid"] == 2
    # Neither id gets the quarantined case as evidence; REQ-1 keeps its own case.
    assert [e.name for e in m.verdicts["REQ-1"].evidence] == ["suite::heat"]
    assert m.verdicts["REQ-2"].evidence == []
    by_kind = {(g.kind, g.entity): g for g in m.gaps}
    multi = by_kind[("multi-tag", "//pkg:t#suite::both")]
    assert "declares REQ-1, REQ-2" in multi.message and "verifies none of them" in multi.message
    assert m.gaps[0] is multi  # quarantines head the queue
    invalid = by_kind[("invalid", "REQ-1")]
    assert invalid.message.startswith("quarantined case(s) name it: //pkg:t#suite::both (multi-tag)")
    assert ("invalid", "REQ-2") in by_kind


def test_incomplete_gap_routes_by_the_open_members_level(tmp_path):
    text = (
        "requirements:\n"
        "  - {id: REQ-1, title: a, verified_by: [{target: //pkg:t, cases: ['suite::sw']}, "
        "{target: //hitl:e2e, level: hitl, cases: ['e2e::rename']}]}\n"
        "  - {id: REQ-2, title: b, verified_by: [{target: //pkg:t, cases: ['suite::sw2', 'suite::gone']}]}\n"
    )
    model = model_text(tmp_path, "config: {rules: {requirement-orphan: 'off'}}\n" + text)
    m = matrix_for(tmp_path, model, [("sw", "passed", [], ""), ("sw2", "passed", [], "")])
    gaps = {(g.kind, g.entity): g for g in m.gaps}
    hitl = gaps[("incomplete", "REQ-1")]
    assert (hitl.message, hitl.route, hitl.provided) == (
        "1/2 passed; 1 not run (//hitl:e2e)",
        "human-gate",
        "simulation",
    )
    sw = gaps[("incomplete", "REQ-2")]
    assert (sw.message, sw.route) == ("1/2 passed; 1 missing (//pkg:t)", "autonomous")
    assert gaps[("missing-case", "REQ-2")].message.startswith("selector 'suite::gone' of //pkg:t matched no case")


def test_flaky_gap_and_policies_through_the_matrix(tmp_path, model):
    cases = [("retry", "passed", ["REQ-1"], "")]
    junit(tmp_path, "bazel-testlogs/pkg/t/test_attempts/attempt_1.xml", [("retry", "failed", ["REQ-1"], "")])
    default = matrix_of_testlogs(tmp_path, model, cases)
    assert default.status("REQ-1") == UNDER_VERIFIED and "flaky" in kinds(default, "REQ-1")
    flag = model_text(tmp_path, "config: {flaky: flag}\n" + MODEL, "flag.yaml")
    flagged = matrix_of_testlogs(tmp_path, flag, cases)
    assert flagged.status("REQ-1") == VERIFIED and kinds(flagged, "REQ-1") == ["flaky"]
    accept = model_text(tmp_path, "config: {flaky: accept}\n" + MODEL, "accept.yaml")
    assert matrix_of_testlogs(tmp_path, accept, cases).status("REQ-1") == VERIFIED
    fail = model_text(tmp_path, "config: {flaky: fail}\n" + MODEL, "fail.yaml")
    failed = matrix_of_testlogs(tmp_path, fail, cases)
    assert failed.status("REQ-1") == FAILED
    (gap,) = [g for g in failed.gaps if g.entity == "REQ-1"]
    assert gap.message == "passed only on a retry (config flaky: fail): suite::retry"


def test_direct_validation_evidence_on_needs(tmp_path, model):
    m = matrix_for(
        tmp_path,
        model,
        [("heat", "passed", ["REQ-1"], ""), ("usability", "failed", ["UN-1"], "inspection")],
    )
    assert m.status("UN-1") == FAILED and m.verdicts["UN-1"].basis == "own+derived"
    assert [e.name for e in m.verdicts["UN-1"].evidence] == ["suite::usability"]
    assert ("failed", "UN-1") in {(g.kind, g.entity) for g in m.gaps}


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


def test_refinement_does_not_override_the_parents_demand(tmp_path):
    path = write(
        tmp_path,
        "m.yaml",
        """
        user_needs: [{id: UN-1, title: n}]
        requirements:
          - {id: REQ-1, title: system, satisfies: [UN-1], method: hitl}
          - {id: REQ-2, title: sw a, refines: [REQ-1]}
          - {id: REQ-3, title: sw b, refines: [REQ-1]}
        """,
    )
    model = load_model(path)
    sim = [("a", "passed", ["REQ-2"], ""), ("b", "passed", ["REQ-3"], "")]
    m = matrix_for(tmp_path, model, sim)
    assert m.status("REQ-2") == VERIFIED and m.status("REQ-1") == UNDER_VERIFIED
    assert m.verdicts["REQ-1"].provided == "simulation" and m.status("UN-1") == PARTIAL
    # more evidence never makes it worse
    m2 = matrix_for(tmp_path, model, [*sim, ("own", "passed", ["REQ-1"], "")])
    assert m2.status("REQ-1") == UNDER_VERIFIED
    # system-level evidence at the demanded rigor verifies it
    m3 = matrix_for(tmp_path, model, [*sim, ("own", "passed", ["REQ-1"], "hitl")])
    assert m3.status("REQ-1") == VERIFIED
    # children proving at the parent's rigor carry it
    m4 = matrix_for(tmp_path, model, [("a", "passed", ["REQ-2"], "hitl"), ("b", "passed", ["REQ-3"], "hitl")])
    assert m4.status("REQ-1") == VERIFIED and m4.verdicts["REQ-1"].provided == "hitl"
    # ... but not a parent whose own set passed only on a retry
    junit(tmp_path, "bazel-testlogs/pkg/t/test_attempts/attempt_1.xml", [("own", "failed", ["REQ-1"], "hitl")])
    m5 = matrix_of_testlogs(tmp_path, model, [("a", "passed", ["REQ-2"], "hitl"), ("b", "passed", ["REQ-3"], "hitl"),
                                              ("own", "passed", ["REQ-1"], "hitl")])  # fmt: skip
    assert m5.status("REQ-1") == UNDER_VERIFIED and m5.verdicts["REQ-1"].reasons == ("flaky",)


def test_refines_cycle_does_not_crash_build_matrix(tmp_path):
    path = write(
        tmp_path,
        "m.yaml",
        "user_needs: [{id: UN-1, title: n}]\nrequirements: [{id: REQ-1, title: a, satisfies: [UN-1], refines: [REQ-2]}, {id: REQ-2, title: b, refines: [REQ-1]}]\n",
    )
    model, _ = read_model(path)
    m = build_matrix(model, ingest.Evidence())
    assert m.status("REQ-1") == UNVERIFIED and m.status("REQ-2") == UNVERIFIED


def test_whole_target_members_can_be_stale(tmp_path):
    path = write(
        tmp_path,
        "m.yaml",
        "user_needs: [{id: UN-1, title: n}]\nrequirements: [{id: REQ-1, title: r, satisfies: [UN-1], method: hil, verified_by: [{target: '//hw:bench', level: hil}]}]\n",
    )
    model = load_model(path)
    junit(tmp_path, "bazel-testlogs/hw/bench/test.xml", [("a", "passed", [], "", [("artifact.sha", "OLD")])])
    ev = ingest.collect([str(tmp_path / "bazel-testlogs")])
    stale = build_matrix(model, ev, current_build={"sha": "NEW"})
    assert stale.status("REQ-1") == UNDER_VERIFIED and stale.verdicts["REQ-1"].stale
    assert build_matrix(model, ev, current_build={"sha": "OLD"}).status("REQ-1") == VERIFIED


def test_failures_outside_requirements_and_misdirected_evidence(tmp_path, model):
    m = matrix_for(
        tmp_path,
        model,
        [
            ("heat", "passed", ["REQ-1"], ""),
            ("usability", "failed", ["UN-1"], ""),
            ("effect", "failed", ["MIT-1"], ""),
            ("risk-tagged", "failed", ["RISK-1"], ""),
            ("method-tagged", "passed", ["TM-1"], ""),
            ("orphan", "error", [], ""),
        ],
    )
    found = {(g.kind, g.entity) for g in m.gaps}
    assert ("failed", "UN-1") in found and ("failed", "MIT-1") in found
    assert ("misdirected-evidence", "RISK-1") in found and ("misdirected-evidence", "TM-1") in found
    # Failures nobody owns: unattributed-failure, and 0.2's untraced-failure alongside.
    unowned = sorted(g.message for g in m.gaps if g.kind == "unattributed-failure")
    assert unowned == [
        "//pkg:t#suite::orphan error and is owned by no requirement",
        "//pkg:t#suite::risk-tagged failed and is owned by no requirement",
    ]
    assert ("untraced-failure", "//pkg:t") in found
    assert m.status("UN-1") == FAILED and m.status("MIT-1") == FAILED and m.status("RISK-1") == FAILED


def test_a_target_scope_failure_nobody_is_affected_by(tmp_path, model):
    junit(
        tmp_path,
        "bazel-testlogs/pkg/t/test.xml",
        [("ok", "passed", [], ""), ("exit-status", "error", [], "", [("rr.scope", "target")])],
    )
    m = build_matrix(model, ingest.collect([str(tmp_path / "bazel-testlogs")]))
    (gap,) = [g for g in m.gaps if g.kind == "unattributed-failure"]
    assert gap.entity == "//pkg:t" and "suite::exit-status" in gap.message and "no member is affected" in gap.message
    assert [g.kind for g in m.gaps if g.kind == "untraced-failure"] == ["untraced-failure"]


def test_failed_hardware_requirement_routes_to_a_human(tmp_path, model):
    m = matrix_for(tmp_path, model, [("bench", "failed", ["REQ-3"], "hil")])
    (gap,) = [g for g in m.gaps if g.kind == "failed"]
    assert gap.entity == "REQ-3" and gap.route == "human-gate"


def test_no_pyramid_violation_without_physical_evidence(tmp_path, model):
    m = matrix_for(tmp_path, model, [("sil-only", "passed", ["REQ-3"], "sil")])
    assert m.status("REQ-3") == UNDER_VERIFIED and m.pyramid_violations() == []


def test_one_unstamped_case_does_not_freshen_a_stale_target(tmp_path):
    path = write(
        tmp_path,
        "m.yaml",
        "user_needs: [{id: UN-1, title: n}]\nrequirements: [{id: REQ-1, title: r, satisfies: [UN-1], method: hil, verified_by: [{target: '//hw:bench', level: hil}]}]\n",
    )
    model = load_model(path)
    junit(
        tmp_path,
        "bazel-testlogs/hw/bench/test.xml",
        [("cutoff", "passed", [], "", [("artifact.sha", "OLD")]), ("sanity", "passed", [], "")],
    )
    ev = ingest.collect([str(tmp_path / "bazel-testlogs")])
    assert build_matrix(model, ev, current_build={"sha": "NEW"}).verdicts["REQ-1"].stale
    assert build_matrix(model, ev, current_build={"sha": "OLD"}).status("REQ-1") == VERIFIED
    assert build_matrix(model, ev).status("REQ-1") == VERIFIED  # no reference build: never stale


def test_the_configured_lock_pins_the_sets(tmp_path):
    lock = "schema: rules_requirements/verification-lock/v1\ncases:\n  //pkg:t:\n    suite::a: REQ-1\n    suite::b: REQ-1\n"
    write(tmp_path, "req/verification.rrlock", lock)
    model = model_text(
        tmp_path,
        "config: {sets_lock: verification.rrlock}\nuser_needs: [{id: UN-1, title: n}]\n"
        "requirements: [{id: REQ-1, title: r, satisfies: [UN-1]}]\n",
        "req/m.yaml",
    )
    m = matrix_for(tmp_path, model, [("a", "passed", ["REQ-1"], "")])  # b was deleted
    assert m.attribution.lock is not None and m.status("REQ-1") == INCOMPLETE
    assert kinds(m, "REQ-1") == ["incomplete", "missing-case"]
    assert "unpinned-sets" not in kinds(m)
    # An explicit lock wins; a lock that cannot be read is an error, never ignored silently.
    write(tmp_path, "req/verification.rrlock", "cases: [nope]\n")
    broken = matrix_for(tmp_path, model, [("a", "passed", ["REQ-1"], "")])
    assert broken.attribution.lock is None and "lock-invalid" in kinds(broken)
    assert "unpinned-sets" in kinds(broken)


def test_no_lock_reads_no_lock_whatever_the_config_names(tmp_path):
    """``lock=NO_LOCK`` (rr report --no-lock) is "not pinned", unlike an empty
    Lock (which pins every set to nothing) and unlike None (the configured one)."""
    from rules_requirements.lock import NO_LOCK, Lock

    lock = "schema: rules_requirements/verification-lock/v1\ncases:\n  //pkg:t:\n    suite::b: REQ-1\n"
    write(tmp_path, "req/verification.rrlock", lock)
    model = model_text(
        tmp_path,
        "config: {sets_lock: verification.rrlock}\nuser_needs: [{id: UN-1, title: n}]\n"
        "requirements: [{id: REQ-1, title: r, satisfies: [UN-1]}]\n",
        "req/m.yaml",
    )
    unpinned = matrix_for(tmp_path, model, [("a", "passed", ["REQ-1"], "")], lock=NO_LOCK)
    assert unpinned.attribution.lock is None and unpinned.status("REQ-1") == VERIFIED
    assert kinds(unpinned) == ["unpinned-sets"]
    configured = matrix_for(tmp_path, model, [("a", "passed", ["REQ-1"], "")])
    assert configured.status("REQ-1") == INCOMPLETE and "unpinned-sets" not in kinds(configured)
    empty = matrix_for(tmp_path, model, [("a", "passed", ["REQ-1"], "")], lock=Lock())
    assert "unlocked-member" in kinds(empty) and "unpinned-sets" not in kinds(empty)
    # Carry-over (d): NO_LOCK is a flag, not an identity; a copy or a pickled
    # round trip of it still reads no lock (it used to pin every set to nothing).
    import copy
    import pickle

    from rules_requirements.lock import is_no_lock

    for clone in (copy.copy(NO_LOCK), copy.deepcopy(NO_LOCK), pickle.loads(pickle.dumps(NO_LOCK))):
        assert clone is not NO_LOCK and is_no_lock(clone) and clone == NO_LOCK
        again = matrix_for(tmp_path, model, [("a", "passed", ["REQ-1"], "")], lock=clone)
        assert again.attribution.lock is None and kinds(again) == ["unpinned-sets"]
    assert not is_no_lock(Lock()) and not is_no_lock(None) and Lock() != NO_LOCK


def test_tags_are_read_through_the_attribution_only(tmp_path, model):
    # A hand-built evidence object, cases added without Evidence.add.
    ev = Evidence(cases=[TestCase("t", "passed", classname="c", declared=("REQ-1",), target="//a:t")])
    m = build_matrix(model, ev)
    assert m.attribution is not None and dict(m.attribution.owner) == {CaseKey("//a:t", "c::t"): "REQ-1"}
    assert m.status("REQ-1") == VERIFIED
    assert ev.target_status == {}  # not maintained here, and no verdict reads it


def test_nearest_case_hints_are_bounded(tmp_path):
    from rules_requirements.trace import _Nearest

    cases = [TestCase(f"test_{i}", "passed", classname=f"mod{i % 3}", target="//a:t") for i in range(400)]
    reqs = "".join(
        f"  - {{id: REQ-{i}, title: r, verified_by: [{{target: //a:t, cases: ['mod0::tst_{i}']}}]}}\n"
        for i in range(1, 6)
    )
    model = model_text(tmp_path, "config: {rules: {requirement-orphan: 'off'}}\nrequirements:\n" + reqs)
    m = build_matrix(model, Evidence(cases=cases))
    hints = [g.message for g in m.gaps if g.kind == "missing-case"]
    assert len(hints) == 5 and all("; nearest existing case: 'mod0::test_" in h for h in hints)
    nearest = _Nearest(m.attribution)
    nearest.left = 1
    assert nearest("//a:t", "mod1::test_1x") == "mod1::test_1"
    assert nearest("//a:t", "mod1::test_1x") is None  # the budget is spent: no more difflib work
    assert nearest("//a:none", "x") is None


def test_counts_test_cases_once_per_resolved_case(tmp_path, model):
    """Retried attempts, a target-scope exit status and a duplicate are
    observations, not test cases: P21 merges them, and counts() agrees."""
    ev = Evidence()
    for case in (
        TestCase("x", "failed", "c", source="bazel-testlogs/p/t/test_attempts/attempt_1.xml", target="//p:t"),
        TestCase("x", "passed", "c", source="bazel-testlogs/p/t/test.xml", target="//p:t"),
        TestCase(
            "exit-status",
            "passed",
            source="bazel-testlogs/p/t/test.xml",
            target="//p:t",
            properties={"rr.scope": "target"},
        ),
        TestCase("y", "passed", "c", source="bazel-testlogs/p/t/test.xml", target="//p:t"),
        TestCase("y", "passed", "c", source="bazel-testlogs/p/t/test.xml", target="//p:t"),
    ):
        ev.add(case)
    m = build_matrix(model, ev)
    assert len(ev.cases) == 5 and m.counts()["test_cases"] == len(m.attribution.cases) == 2


def test_a_need_or_mitigation_verdict_is_a_rollup_of_its_own_set(tmp_path):
    """Decided: an INCOMPLETE own set makes a user need (or mitigation) PARTIAL,
    not INCOMPLETE; the set's incomplete gap is still raised."""
    model = model_text(
        tmp_path,
        "user_needs: [{id: UN-1, title: n, validated_by: [{target: //u:t, cases: ['u::*']}]}]\n"
        "requirements: [{id: REQ-1, title: r, satisfies: [UN-1]}]\n",
    )
    ev = Evidence()
    ev.add(TestCase("study", "passed", "u", target="//u:t"))
    ev.add(TestCase("later", "skipped", "u", target="//u:t"))
    m = build_matrix(model, ev)
    assert m.status("UN-1") == PARTIAL and m.verdicts["UN-1"].basis == "own+derived"
    assert ("incomplete", "UN-1") in {(g.kind, g.entity) for g in m.gaps}
