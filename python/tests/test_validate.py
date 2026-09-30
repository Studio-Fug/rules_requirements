# SPDX-License-Identifier: AGPL-3.0-or-later
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
    assert all(i.severity == "error" for i in issues)


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
