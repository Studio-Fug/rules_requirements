# SPDX-License-Identifier: AGPL-3.0-or-later
import pytest
from conftest import MODEL, write

from rules_requirements.model import read_model
from rules_requirements.validate import Issue, validate


def issues_for(tmp_path, text):
    m, _ = read_model(write(tmp_path, "m.yaml", text))
    return validate(m)


def codes(issues):
    return sorted({(i.code, i.severity) for i in issues})


def test_valid_model_has_no_issues(model):
    assert validate(model) == []


def test_reference_errors(tmp_path):
    issues = issues_for(
        tmp_path,
        """
        user_needs: [{id: UN-1, title: A}]
        requirements:
          - {id: REQ-1, title: a, satisfies: [UN-1, UN-9], refines: [REQ-1], method: warp-speed}
          - {id: REQ-2, title: b, satisfies: [REQ-1], verified_by: [{target: "//x", level: nope}]}
          - {id: REQ-3, title: c, refines: [REQ-4]}
          - {id: REQ-4, title: d, refines: [REQ-3]}
          - {id: req-5, title: e, satisfies: [UN-1], status: weird}
        risks:
          - {id: RISK-1, title: r, severity: apocalyptic, likelihood: often, mitigated_by: [MIT-2, MIT-9]}
        mitigations:
          - {id: MIT-1, title: m, type: prayer, mitigates: [RISK-1], implemented_by: [REQ-99]}
          - {id: MIT-2, title: m2, mitigates: [], implemented_by: [REQ-1]}
        test_methods:
          - {id: TM-1, title: t}
          - {id: TM-2, title: t2, level: telepathy}
        """,
    )
    text = "\n".join(str(i) for i in issues)
    for fragment in (
        "satisfies unknown user_need UN-9",
        "refines itself",
        "method 'warp-speed' is neither",
        "satisfies REQ-1, which is a requirement, not a user_need",
        "unknown level 'nope'",
        "refines cycle: REQ-3 -> REQ-4 -> REQ-3",
        "req-5: requirement ids must match REQ-\\d+",
        "status must be one of",
        "severity must be one of",
        "likelihood must be one of",
        "mitigated_by unknown mitigation MIT-9",
        "mitigated_by MIT-2, but MIT-2.mitigates does not list RISK-1",
        "MIT-1 mitigates it but is missing from mitigated_by",
        "type must be one of",
        "implemented_by unknown requirement REQ-99",
        "MIT-2: mitigates no risk",
        "TM-1: a test method must declare its level",
        "TM-2: unknown level 'telepathy'",
    ):
        assert fragment in text, fragment
    assert all(i.severity == "error" for i in issues if i.code != "bare-target-reference")


def test_coverage_rules_default_to_errors(tmp_path):
    issues = issues_for(
        tmp_path,
        """
        user_needs: [{id: UN-1, title: lonely}]
        requirements: [{id: REQ-1, title: orphan}]
        risks: [{id: RISK-1, title: uncontrolled}]
        mitigations: [{id: MIT-1, title: promise, mitigates: [RISK-1]}]
        """,
    )
    got = codes(issues)
    assert ("need-unsatisfied", "error") in got
    assert ("requirement-orphan", "error") in got
    assert ("mitigation-unimplemented", "error") in got
    assert ("risk-unmitigated", "error") not in got  # MIT-1 does mitigate it


def test_rules_are_configurable_and_strict_promotes(tmp_path):
    text = """
        config:
          rules: {need-unsatisfied: warning, requirement-orphan: "off", risk-unmitigated: warning}
        user_needs: [{id: UN-1, title: lonely}]
        requirements: [{id: REQ-1, title: orphan}]
        risks: [{id: RISK-1, title: r}]
        """
    issues = issues_for(tmp_path, text)
    assert codes(issues) == [("need-unsatisfied", "warning"), ("risk-unmitigated", "warning")]
    m, _ = read_model(str(tmp_path / "m.yaml"))
    assert {i.severity for i in validate(m, strict=True)} == {"error"}


def test_risk_acceptability(tmp_path):
    base = MODEL.replace("project:\n  name: Thermostat", "config: {acceptable_risk_score: 4}\nproject: {name: T}")
    issues = issues_for(tmp_path, base)
    assert issues == []  # residual high(4) x rare(1) = 4 is acceptable
    issues = issues_for(tmp_path, base.replace("residual_likelihood: rare", "residual_likelihood: likely"))
    assert codes(issues) == [("risk-unacceptable", "warning")]
    assert "residual risk score 16 (high x likely)" in str(issues[0])


def test_issue_str_and_sorting(tmp_path):
    issues = issues_for(
        tmp_path,
        "config: {rules: {requirement-orphan: warning}}\nrequirements: [{id: REQ-1, title: x}]\nuser_needs: [{id: UN-1, title: y}]\n",
    )
    assert [i.severity for i in issues] == ["error", "warning"]
    assert str(issues[0]).endswith("error: [need-unsatisfied] UN-1: no requirement satisfies this need")
    assert str(Issue("error", "shape", "boom")) == "error: [shape] boom"


def test_parse_errors_become_shape_issues(tmp_path):
    issues = issues_for(tmp_path, "user_needs: [{id: UN-1}]\n")
    assert ("shape", "error") in codes(issues)


# --- claims: a test case verifies at most one requirement --------------------------

HEAD = """
user_needs: [{id: UN-1, title: n}]
risks: [{id: RISK-1, title: r}]
"""


def reqs(*items, config=""):
    """A model whose requirements REQ-1.. have the given verified_by items."""
    lines = ["requirements:"]
    for i, vb in enumerate(items, 1):
        lines.append(f"  - {{id: REQ-{i}, title: r{i}, satisfies: [UN-1], verified_by: {vb}}}")
    lines.append("mitigations: [{id: MIT-1, title: m, mitigates: [RISK-1], implemented_by: [REQ-1]}]")
    return (f"config: {config}\n" if config else "") + HEAD + "\n".join(lines) + "\n"


def only(issues, code):
    return [i for i in issues if i.code == code]


def test_disjoint_selectors_are_valid(tmp_path):
    issues = issues_for(
        tmp_path,
        reqs(
            '[{target: //web:clocksync_test, cases: ["clocksync::offset*"]}]',
            '[{target: //web:clocksync_test, cases: ["clocksync::bestSample*", "clocksync::drift"]}]',
        ),
    )
    assert issues == []


def test_shared_case_names_both_ids_both_patterns_a_witness_and_both_locations(tmp_path):
    issues = issues_for(
        tmp_path,
        reqs(
            '[{target: //web:clocksync_test, cases: ["clocksync::offset*"]}]',
            '[{target: //web:clocksync_test, cases: ["clocksync::*"]}]',
        ),
    )
    (issue,) = only(issues, "shared-case")
    assert issue.severity == "error" and issue.entity == "REQ-2"
    assert issue.location.line == 5
    assert str(issue).endswith(
        "m.yaml:5: error: [shared-case] REQ-2 and REQ-1 both claim cases of //web:clocksync_test "
        "('clocksync::*' vs 'clocksync::offset*'), e.g. 'clocksync::offset' "
        f"(REQ-1 claims it at {tmp_path / 'm.yaml'}:4). "
        "A test case verifies at most one requirement: narrow one selector."
    )


def test_literal_selectors_conflict_only_when_equal(tmp_path):
    issues = issues_for(tmp_path, reqs("[{target: //a:t, cases: [m::x]}]", "[{target: //a:t, cases: [m::x]}]"))
    assert "e.g. 'm::x'" in only(issues, "shared-case")[0].message
    assert issues_for(tmp_path, reqs("[{target: //a:t, cases: [m::x]}]", "[{target: //a:t, cases: [m::y]}]")) == []


def test_whole_versus_pattern(tmp_path):
    issues = issues_for(
        tmp_path,
        reqs(
            "[{target: //a:t, whole: true, reason: one binary}]",
            '[{target: //a:t, cases: ["m::t*"]}]',
        ),
    )
    (issue,) = only(issues, "shared-case")
    assert "both claim cases of //a:t ('m::t*' vs the whole target), e.g. 'm::t'" in issue.message
    assert "replace the whole-target claim with selectors" in issue.message


def test_whole_versus_whole_and_legacy_refs_are_never_grandfathered(tmp_path):
    # P15: two requirements naming one target the old way
    issues = issues_for(tmp_path, reqs("[//web:pinhole_test]", "[{target: //web:pinhole_test, level: sil}]"))
    (issue,) = only(issues, "shared-case")
    assert "REQ-2 and REQ-1 both claim the whole target //web:pinhole_test" in issue.message
    assert "give the target to one of them" in issue.message
    assert len(only(issues, "bare-target-reference")) == 2


def test_spellings_of_one_target_are_one_target(tmp_path):
    # P18: @@//, @<main_repo>//, //p and canonical decorations normalize before comparing
    issues = issues_for(
        tmp_path,
        reqs(
            '[{target: "@splanc//web", cases: ["a::*"]}]',
            '[{target: "@@//web:web", cases: ["a::b"]}]',
            "[{target: //web:web, whole: true, reason: r}]",
            config="{main_repo: splanc}",
        ),
    )
    assert len(only(issues, "shared-case")) == 3


def test_cross_kind_claims_share_one_namespace(tmp_path):
    # P20: a user need's validated_by and a mitigation's verified_by compete with requirements
    text = (
        HEAD.replace(
            "user_needs: [{id: UN-1, title: n}]",
            'user_needs: [{id: UN-1, title: n, validated_by: [{target: "record:study", cases: ["*"]}]}]',
        )
        + "requirements:\n"
        + '  - {id: REQ-1, title: r, satisfies: [UN-1], verified_by: [{target: "record:study", cases: [t1]}]}\n'
        + "mitigations:\n"
        + "  - {id: MIT-1, title: m, mitigates: [RISK-1], implemented_by: [REQ-1],"
        + ' verified_by: [{target: //a:t, cases: ["*"]}]}\n'
        + '  - {id: MIT-2, title: m2, mitigates: [RISK-1], implemented_by: [REQ-1], verified_by: ["//a:t"]}\n'
    )
    issues = only(issues_for(tmp_path, text), "shared-case")
    pairs = {frozenset(e for e in ("UN-1", "REQ-1", "MIT-1", "MIT-2") if f"{e} " in i.message) for i in issues}
    assert pairs == {frozenset({"UN-1", "REQ-1"}), frozenset({"MIT-1", "MIT-2"})}
    assert any("e.g. 't1'" in i.message for i in issues)


def test_redundant_selectors_within_one_entity_are_a_warning(tmp_path):
    issues = issues_for(tmp_path, reqs('[{target: //a:t, cases: ["m::*", "m::x"]}, //a:t]'))
    red = only(issues, "redundant-selector")
    assert red and all(i.severity == "warning" for i in red)
    assert "REQ-1: 'm::*' and 'm::x' of //a:t overlap, e.g. 'm::x'; one of them is redundant" in [
        i.message for i in red
    ]
    assert not only(issues, "shared-case")


def test_variants_make_two_targets_one_test_code(tmp_path):
    config = "{variants: [[//h:fx_bench_netstack, //h:fx_bench_jit_netstack]]}"
    issues = issues_for(
        tmp_path,
        reqs(
            "[{target: //h:fx_bench_netstack, whole: true, reason: script}]",
            "[{target: //h:fx_bench_jit_netstack, whole: true, reason: script}]",
            config=config,
        ),
    )
    (issue,) = only(issues, "same-code-multiple-owners")
    assert issue.severity == "error"
    assert "REQ-2 claims the whole target of //h:fx_bench_jit_netstack and REQ-1 claims the whole target of " in (
        issue.message
    )
    assert "run the same test code (config.variants)" in issue.message
    # one owner for both variants is fine; disjoint case names are fine
    assert not only(
        issues_for(
            tmp_path,
            reqs(
                "[{target: //h:fx_bench_netstack, whole: true, reason: s}, "
                "{target: //h:fx_bench_jit_netstack, whole: true, reason: s}]",
                config=config,
            ),
        ),
        "same-code-multiple-owners",
    )
    assert not only(
        issues_for(
            tmp_path,
            reqs(
                '[{target: //h:fx_bench_netstack, cases: ["fx::a*"]}]',
                '[{target: //h:fx_bench_jit_netstack, cases: ["fx::jit_*"]}]',
                config=config,
            ),
        ),
        "same-code-multiple-owners",
    )


def test_bad_targets_selectors_and_levels(tmp_path):
    issues = issues_for(
        tmp_path,
        reqs(
            '[{target: "web:x", cases: [a]}, {target: //a:t, cases: [" padded", "x\\\\y", "[target]", ""]}]',
            "[{target: //a:u, cases: [b], whole: true}, {target: //a:v, cases: [c], level: telepathy}]",
            config="{variants: [[//a:t, ':bad']]}",
        ),
    )
    bad_targets = [i.message for i in only(issues, "bad-target")]
    assert any("REQ-1: verified_by[0]: 'web:x': not an absolute label" in m for m in bad_targets)
    assert any("config.variants: ':bad'" in m for m in bad_targets)
    selectors = " | ".join(i.message for i in only(issues, "bad-selector"))
    for fragment in ("leading or trailing blanks", "must be followed", "claim it with whole: true", "empty selector"):
        assert fragment in selectors
    assert "REQ-2: verified_by //a:u: an item has either cases or whole: true, not both" in selectors
    assert any("unknown level 'telepathy'" in i.message for i in only(issues, "bad-level"))
    assert all(i.severity == "error" for i in issues if i.code in ("bad-target", "bad-selector", "bad-level"))


def test_whole_and_legacy_rules(tmp_path):
    issues = issues_for(tmp_path, reqs("[//a:t, {target: //a:u, level: hil}, {target: //a:v, whole: true}]"))
    assert [i.severity for i in only(issues, "bare-target-reference")] == ["warning", "warning"]
    assert "{target: //a:t, cases: ['*']}" in only(issues, "bare-target-reference")[0].message
    (whole,) = only(issues, "whole-target-reference")
    assert whole.severity == "warning" and "needs a reason" in whole.message
    strict = issues_for(
        tmp_path,
        reqs(
            "[//a:t, {target: //a:v, cases: ['*']}]",
            config="{rules: {bare-target-reference: error, glob-selector: warning}}",
        ),
    )
    assert [i.severity for i in only(strict, "bare-target-reference")] == ["error"]
    assert [i.severity for i in only(strict, "glob-selector")] == ["warning"]
    m, _ = read_model(write(tmp_path, "s.yaml", reqs("[//a:t]")))
    assert {i.severity for i in validate(m, strict=True)} == {"error"}


def test_shared_case_cannot_be_configured_off(tmp_path):
    issues = issues_for(
        tmp_path, reqs("[//a:t]", "[//a:t]", config="{rules: {shared-case: 'off', bare-target-reference: 'off'}}")
    )
    assert only(issues, "shared-case")
    assert any("'shared-case' is always an error" in i.message for i in only(issues, "shape"))


def test_parent_with_claims(tmp_path):
    text = HEAD + (
        "requirements:\n"
        "  - {id: REQ-1, title: parent, satisfies: [UN-1], verified_by: [{target: //a:t, cases: [x]}]}\n"
        "  - {id: REQ-2, title: child, refines: [REQ-1], verified_by: [{target: //a:t, cases: [y]}]}\n"
        "mitigations: [{id: MIT-1, title: m, mitigates: [RISK-1], implemented_by: [REQ-1]}]\n"
    )
    (issue,) = only(issues_for(tmp_path, text), "parent-with-claims")
    assert issue.entity == "REQ-1" and issue.severity == "warning" and "refined by REQ-2" in issue.message


def test_known_targets(tmp_path):
    m, _ = read_model(
        write(
            tmp_path,
            "m.yaml",
            reqs(
                '[{target: "@splanc//web:a_test", cases: [x]}, {target: //web:typo_test, cases: [y]},'
                ' {target: "record:study", cases: [z]}]',
                config="{main_repo: splanc}",
            ),
        )
    )
    issues = validate(m, known_targets=["@@//web:a_test", "//web:b_test"])
    (issue,) = only(issues, "unknown-target")
    assert issue.severity == "error" and "//web:typo_test: no such test target" in issue.message
    assert only(validate(m), "unknown-target") == []


# --- the verification-set lock ------------------------------------------------------

LOCK_HEAD = "schema: rules_requirements/verification-lock/v1\ncases:\n"


def lock_model(tmp_path, lock_text, mode="model"):
    write(tmp_path, "verification.rrlock", lock_text)
    text = reqs(
        '[{target: //web:a_test, cases: ["a::*"]}]',
        "[{target: //req:model_test, whole: true, reason: one run}]",
        config=f"{{attribution: {mode}, sets_lock: verification.rrlock}}",
    )
    return issues_for(tmp_path, text)


def test_a_consistent_lock_is_valid(tmp_path):
    lock = LOCK_HEAD + '  //web:a_test:\n    "a::one": REQ-1\n  "@@//req:model_test":\n    "[target]": REQ-2\n'
    assert lock_model(tmp_path, lock) == []


@pytest.mark.parametrize(
    "lock, fragment",
    [
        (None, "does not exist"),
        ("schema: v0\ncases: {}\n", "schema must be"),
        (LOCK_HEAD + '  //web:a_test:\n    "a::one": [REQ-1, REQ-2]\n', "a case has exactly one owner"),
        (LOCK_HEAD + '  //web:a_test:\n    "a::one": REQ-1\n    "a::one": REQ-2\n', "duplicate key"),
        (LOCK_HEAD + '  //web:a_test:\n    "a::one": REQ-1, REQ-2\n', "is not one id"),
        (LOCK_HEAD + '  //web:a_test: {"a::one": REQ-1}\n  "//web:a_test ": {"a::one": REQ-1}\n', "locked twice"),
        (LOCK_HEAD + '  "web:a": {"a::one": REQ-1}\n', "not an absolute label"),
        (LOCK_HEAD + '  //web:a_test: {"a::one": RISK-1}\n', "which is a risk"),
        (LOCK_HEAD + '  //web:a_test: {"a::one": REQ-9}\n', "which is not defined"),
        ("[1, 2]\n", "one YAML mapping"),
        (LOCK_HEAD + "  //web:a_test: {a: REQ-1}\nextra: 1\n", "unknown key 'extra'"),
    ],
)
def test_lock_invalid(tmp_path, lock, fragment):
    if lock is None:
        issues = issues_for(tmp_path, reqs("[]", config="{sets_lock: missing.rrlock}"))
    else:
        issues = lock_model(tmp_path, lock)
    bad = only(issues, "lock-invalid")
    assert bad and all(i.severity == "error" for i in bad)
    assert any(fragment in i.message for i in bad), [i.message for i in bad]


def test_lock_stale_and_owner_changed_in_model_mode(tmp_path):
    lock = LOCK_HEAD + (
        "  //web:a_test:\n"
        '    "a::one": REQ-2\n'  # REQ-1 claims it now
        '    "b::gone": REQ-1\n'  # no claim of REQ-1 selects it
        "  //req:model_test:\n"
        '    "[target]": REQ-2\n'
    )
    issues = lock_model(tmp_path, lock)
    (changed,) = only(issues, "lock-owner-changed")
    assert changed.severity == "error" and "//web:a_test#a::one is locked to REQ-2 but claimed by REQ-1" in (
        changed.message
    )
    assert changed.location.path.endswith("verification.rrlock") and changed.location.line == 4
    (stale,) = only(issues, "lock-stale")
    assert stale.severity == "error" and "//web:a_test#b::gone is locked to REQ-1" in stale.message
    # a selector never selects the synthetic result
    lock2 = LOCK_HEAD + '  //web:a_test:\n    "[target]": REQ-1\n'
    assert only(lock_model(tmp_path, lock2), "lock-stale")
    # hybrid mode: a tag may own a locked case no claim covers
    assert lock_model(tmp_path, lock, mode="hybrid") == []


def test_cli_validate_known_targets(tmp_path, capsys):
    from rules_requirements import cli

    model = write(tmp_path, "m.yaml", reqs("[{target: //web:typo_test, cases: [y]}]"))
    targets = write(tmp_path, "targets.txt", "//web:a_test\nnot a label\n")
    rc = cli.main(["validate", model, "--known-targets", targets])
    err = capsys.readouterr().err
    assert rc == 1 and "[unknown-target]" in err and "not a label: 'not a label'" in err
    assert cli.main(["validate", model]) == 0
