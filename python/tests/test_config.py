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
