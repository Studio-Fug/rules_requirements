# SPDX-License-Identifier: AGPL-3.0-or-later
import pytest

from rules_requirements import config as cfg
from rules_requirements.config import Config, parse_config


def test_defaults():
    c = Config()
    assert c.prefix(cfg.REQUIREMENT) == "REQ"
    assert c.id_regex(cfg.REQUIREMENT).match("REQ-0001")
    assert not c.id_regex(cfg.REQUIREMENT).match("REQ-1a")
    assert c.kind_of("MIT-3") == cfg.MITIGATION
    assert c.kind_of("NOPE-1") is None
    assert c.rank("hil") > c.rank("simulation")
    assert c.rank("inspection") is None
    assert c.risk_score("high", "possible") == 4 * 3
    assert c.risk_score("bogus", "possible") is None


def test_any_id_regex_prefers_longest_prefix_and_word_boundaries():
    c = parse_config({"prefixes": {"requirement": "PR", "risk": "PRISK"}}, [])
    rx = c.any_id_regex()
    assert rx.findall("PR-1, PRISK-2 and XPR-3 and PR-4a") == ["PR-1", "PRISK-2"]


def test_parse_config_custom():
    errors: list[str] = []
    c = parse_config(
        {
            "prefixes": {"requirements": "PR"},
            "levels": ["unit", {"name": "bench"}, {"name": "review", "ordered": False}],
            "default_level": "unit",
            "default_provided_level": "unit",
            "autonomous_max_level": "unit",
            "pyramid_min_level": "bench",
            "pyramid_cheap_levels": ["unit"],
            "acceptable_risk_score": 6,
            "rules": {"need-unsatisfied": "warning"},
            "annotation_patterns": [r"Requirements:\s*([A-Z0-9,\s-]+)"],
        },
        errors,
    )
    assert errors == []
    assert c.prefix(cfg.REQUIREMENT) == "PR"
    assert c.level_names() == ("unit", "bench", "review")
    assert c.rank("review") is None
    assert c.rule("need-unsatisfied") == "warning"


@pytest.mark.parametrize(
    "raw, fragment",
    [
        ({"bogus": 1}, "unknown key"),
        ({"prefixes": {"widget": "W"}}, "unknown entity kind"),
        ({"prefixes": {"requirement": "UN"}}, "must be distinct"),
        ({"id_pattern": r"\d+"}, "must contain"),
        ({"id_pattern": r"{prefix}-(\d+"}, "invalid regex"),
        ({"levels": []}, "at least one level"),
        ({"levels": [{"description": "x"}]}, "needs a name"),
        ({"default_level": "nope"}, "not a defined level"),
        ({"pyramid_min_level": "nope"}, "is not a level"),
        ({"pyramid_cheap_levels": ["nope"]}, "not a defined level"),
        ({"high_severities": ["nope"]}, "not a defined severity"),
        ({"acceptable_risk_score": "high"}, "must be an integer"),
        ({"rules": {"nope": "error"}}, "unknown rule"),
        ({"rules": {"need-unsatisfied": "loud"}}, "must be one of"),
        ({"annotation_patterns": ["no-group"]}, "capture group"),
        ({"annotation_patterns": ["(bad"]}, "invalid regex"),
    ],
)
def test_parse_config_errors(raw, fragment):
    errors: list[str] = []
    parse_config(raw, errors)
    assert any(fragment in e for e in errors), errors


def test_parse_config_not_a_mapping():
    errors: list[str] = []
    assert parse_config(["x"], errors) == Config()  # type: ignore[arg-type]
    assert errors == ["config: must be a mapping"]


def test_unquoted_off_is_off():
    # YAML 1.1 reads `unknown-field: off` as false.
    errors: list[str] = []
    config = parse_config({"rules": {"unknown-field": False}}, errors)
    assert not errors and config.rules["unknown-field"] == "off"


def test_attribution_keys():
    errors: list[str] = []
    c = parse_config(
        {
            "attribution": "model",
            "main_repo": "splanc",
            "sets_lock": "verification.rrlock",
            "flaky": "flag",
            "set_consistency": False,  # an unquoted YAML `off`
            "variants": [["//p:a", "@splanc//p:b"], ["//q:a", "//q:b", "//q:c"]],
            "rules": {"bare-target-reference": "error", "glob-selector": "warning", "lock-stale": "off"},
        },
        errors,
    )
    assert errors == []
    assert (c.attribution, c.main_repo, c.sets_lock, c.flaky, c.set_consistency) == (
        "model",
        "splanc",
        "verification.rrlock",
        "flag",
        "off",
    )
    assert c.variants == (("//p:a", "@splanc//p:b"), ("//q:a", "//q:b", "//q:c"))
    assert c.variant_groups() == (("//p:a", "//p:b"), ("//q:a", "//q:b", "//q:c"))
    assert c.rule("bare-target-reference") == "error" and c.rule("lock-stale") == "off"


def test_attribution_defaults():
    c = Config()
    assert (c.attribution, c.main_repo, c.sets_lock, c.flaky, c.set_consistency, c.variants) == (
        "hybrid",
        "",
        "",
        "under-verify",
        "warn",
        (),
    )
    assert c.rule("bare-target-reference") == "warning" and c.rule("glob-selector") == "off"
    assert c.rule("lock-stale") == "error"
    for hard in cfg.HARD_ERRORS:
        assert hard not in cfg.DEFAULT_RULES and c.rule(hard) == "error"


def test_hard_errors_stay_errors_even_when_a_config_object_says_off():
    # parse_config refuses such keys; a Config built in Python must not get around that
    c = Config(rules={hard: "off" for hard in cfg.HARD_ERRORS})
    assert {c.rule(hard) for hard in cfg.HARD_ERRORS} == {"error"}
    assert Config(rules={"glob-selector": "error"}).rule("glob-selector") == "error"  # ordinary rules do follow


@pytest.mark.parametrize(
    "raw, fragment",
    [
        ({"attribution": "tags"}, "config.attribution: must be one of"),
        ({"flaky": "ignore"}, "config.flaky: must be one of"),
        ({"set_consistency": "strict"}, "config.set_consistency: must be one of"),
        ({"main_repo": "@splanc"}, "config.main_repo"),
        ({"main_repo": 3}, "config.main_repo"),
        ({"sets_lock": ""}, "config.sets_lock"),
        ({"variants": "//a:b"}, "config.variants: must be a list"),
        ({"variants": [["//a:b"]]}, "at least two targets"),
        ({"variants": [["//a:b", "//a:b"]]}, "lists a target twice"),
        ({"variants": [["//a:a", "//a"]]}, "lists a target twice"),
        ({"variants": [["//a:b", "//a:c"], ["//a:c", "//a:d"]]}, "merge the two groups"),
        ({"variants": [[1, 2]]}, "must be a list of target labels"),
        ({"rules": {"shared-case": "off"}}, "'shared-case' is always an error and cannot be configured"),
        ({"rules": {"bad-target": "warning"}}, "'bad-target' is always an error"),
        ({"rules": {"same-code-multiple-owners": "off"}}, "always an error"),
        ({"rules": {"multi-tag": "off"}}, "report-time quarantine, not a rule"),
        ({"rules": {"attribution-conflict": "warning"}}, "report-time quarantine, not a rule"),
    ],
)
def test_attribution_key_errors(raw, fragment):
    errors: list[str] = []
    parse_config(raw, errors)
    assert any(fragment in e for e in errors), errors
