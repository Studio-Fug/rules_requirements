# SPDX-License-Identifier: AGPL-3.0-or-later
"""The published JSON Schema agrees with the loader on valid and invalid shapes."""

import json
import os

import pytest
from conftest import MODEL

from rules_requirements._vendor import yaml

jsonschema = pytest.importorskip("jsonschema")

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _schema():
    for base in (_ROOT, os.environ.get("TEST_SRCDIR", "") + "/_main"):
        path = os.path.join(base, "schema", "rules_requirements.schema.json")
        if os.path.exists(path):
            with open(path) as fh:
                return json.load(fh)
    pytest.skip("schema not available")


def test_schema_accepts_models():
    validator = jsonschema.Draft202012Validator(_schema())
    validator.validate(yaml.safe_load(MODEL))
    validator.validate({"kind": "requirement", "id": "REQ-1", "title": "t", "satisfies": "UN-1"})
    validator.validate(
        {"config": {"prefixes": {"requirement": "PR"}, "levels": ["a", {"name": "b", "ordered": False}]}}
    )


@pytest.mark.parametrize(
    "doc",
    [
        {"requirements": [{"id": "REQ-1"}]},
        {"requirements": [{"id": "REQ-1", "title": "t", "satisfes": ["UN-1"]}]},
        {"kind": "requirement", "id": "REQ-1"},
        {"mitigations": [{"id": "MIT-1", "title": "t", "type": "prayer"}]},
        {"config": {"rules": {"need-unsatisfied": "loud"}}},
    ],
)
def test_schema_rejects_bad_shapes(doc):
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(doc, _schema())


def _worksheet_schema():
    for base in (_ROOT, os.environ.get("TEST_SRCDIR", "") + "/_main"):
        path = os.path.join(base, "schema", "worksheet.schema.json")
        if os.path.exists(path):
            with open(path) as fh:
                return json.load(fh)
    pytest.skip("schema not available")


def test_worksheet_schema_accepts_generated_and_decided_worksheets():
    from test_migrate import REPO, fixture_plan

    from rules_requirements import migrate

    validator = jsonschema.Draft202012Validator(_worksheet_schema())
    _, plan = fixture_plan()
    doc = json.loads(migrate.render_json(migrate.worksheet(plan, inputs={"model": ["m"], "evidence": ["e"]})))
    validator.validate(doc)
    validator.validate(migrate.load_worksheet(os.path.join(REPO, "decided.rrplan")))


@pytest.mark.parametrize(
    "owner",
    [["REQ-1", "REQ-2"], "REQ-1, REQ-2", "REQ-1 REQ-2", ""],
)
def test_worksheet_schema_rejects_more_than_one_owner(owner):
    doc = {
        "schema": "rules_requirements/attribution-worksheet/v1",
        "groups": [{"target": "//a:b", "group": "m", "owner": owner, "cases": [{"path": "m::t"}]}],
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(doc, _worksheet_schema())


def test_schema_accepts_claims_and_the_attribution_config():
    validator = jsonschema.Draft202012Validator(_schema())
    validator.validate(
        {
            "config": {
                "attribution": "model",
                "main_repo": "splanc",
                "sets_lock": "verification.rrlock",
                "flaky": "under-verify",
                "set_consistency": False,
                "variants": [["//h:fx_bench_netstack", "//h:fx_bench_jit_netstack"]],
                "rules": {"bare-target-reference": "error", "glob-selector": "off"},
            },
            "user_needs": [
                {"id": "UN-5", "title": "u", "validated_by": [{"target": "record:usability_study", "cases": ["*"]}]}
            ],
            "requirements": [
                {
                    "id": "PR-29",
                    "title": "t",
                    "verified_by": [
                        "//legacy:bare",
                        {"target": "//legacy:mapping", "level": "hil"},
                        {"target": "@@//web:improv_provision_test", "cases": ["improv::retry*"], "level": "sil"},
                        {"target": "@splanc//pi/hitl/tests:hitl_test", "cases": ["a::b", "c::*"]},
                        {"target": "//requirements:model_test", "whole": True, "reason": "one run"},
                    ],
                }
            ],
            "mitigations": [{"id": "MIT-4", "title": "m", "verified_by": [{"target": "//pi:bench", "whole": True}]}],
        }
    )
    validator.validate({"kind": "mitigation", "id": "MIT-1", "title": "m", "verified_by": ["//a:b"]})
    validator.validate({"kind": "user_need", "id": "UN-1", "title": "u", "validated_by": ["record:x"]})


@pytest.mark.parametrize(
    "item",
    [
        {"target": "//a:b", "cases": ["x"], "whole": True},
        {"target": "//a:b", "cases": []},
        {"target": "//a:b", "cases": "x"},
        {"target": "//a:b", "whole": False},
        {"target": "//a:b", "reason": "why"},
        {"target": "//a:b", "cases": ["x"], "ticket": "QA-1"},
        {"target": "a:b", "cases": ["x"]},
        {"cases": ["x"]},
        "pkg:name",
    ],
)
def test_schema_rejects_bad_claim_items(item):
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"requirements": [{"id": "REQ-1", "title": "t", "verified_by": [item]}]}, _schema())


@pytest.mark.parametrize(
    "config",
    [
        {"rules": {"shared-case": "off"}},
        {"rules": {"multi-tag": "warning"}},
        {"attribution": "tags"},
        {"main_repo": "@splanc"},
        {"variants": [["//a:b"]]},
        {"flaky": "ignore"},
    ],
)
def test_schema_rejects_bad_attribution_config(config):
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"config": config}, _schema())


def test_schema_rejects_claims_on_risks_and_test_methods():
    for doc in (
        {"risks": [{"id": "RISK-1", "title": "r", "verified_by": ["//a:b"]}]},
        {"kind": "test_method", "id": "TM-1", "title": "t", "validated_by": ["//a:b"]},
    ):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(doc, _schema())


def _lock_schema():
    for base in (_ROOT, os.environ.get("TEST_SRCDIR", "") + "/_main"):
        path = os.path.join(base, "schema", "verification_lock.schema.json")
        if os.path.exists(path):
            with open(path) as fh:
                return json.load(fh)
    pytest.skip("schema not available")


LOCK = """
schema: rules_requirements/verification-lock/v1
cases:
  //web:improv_provision_test:
    "improv_provision::provisionViaBle: survives a GATT flake via retry": PR-29
  "@@//requirements:model_test":
    "[target]": PR-25
"""


def test_lock_schema_agrees_with_the_lock_reader():
    from rules_requirements import lock

    validator = jsonschema.Draft202012Validator(_lock_schema())
    validator.validate(yaml.safe_load(LOCK))
    parsed = lock.parse_lock(LOCK)
    assert parsed.owner_of("//requirements:model_test", "[target]") == "PR-25"
    for bad in (
        LOCK.replace(": PR-25", ": [PR-25, PR-13]"),
        LOCK.replace(": PR-25", ": PR-25, PR-13"),
        LOCK.replace("verification-lock/v1", "verification-lock/v0"),
        LOCK.replace("//web:improv_provision_test:", "web:improv_provision_test:"),
        LOCK + "extra: 1\n",
    ):
        with pytest.raises(jsonschema.ValidationError):
            validator.validate(yaml.safe_load(bad))
        with pytest.raises(lock.LockError):
            lock.parse_lock(bad)


def test_schema_forbids_configuring_exactly_the_hard_errors_and_quarantines():
    from rules_requirements import config as cfg

    rules = _schema()["oneOf"][0]["properties"]["config"]["properties"]["rules"]
    assert set(rules["propertyNames"]["not"]["enum"]) == set(cfg.HARD_ERRORS) | set(cfg.QUARANTINE_CODES)


def _label_ok(schema, label):
    doc = {"requirements": [{"id": "REQ-1", "title": "t", "verified_by": [label, {"target": label, "cases": ["x"]}]}]}
    return jsonschema.Draft202012Validator(schema).is_valid(doc)


def test_schema_and_loader_agree_on_labels():
    from test_labels import BAD_LABELS, GOOD_LABELS

    from rules_requirements.labels import try_normalize

    schema = _schema()
    labels = [good for good, _ in GOOD_LABELS] + [want for _, want in GOOD_LABELS] + list(BAD_LABELS)
    disagree = [
        (label, try_normalize(label) is not None)
        for label in labels
        if _label_ok(schema, label) != (try_normalize(label) is not None)
    ]
    assert disagree == []  # (label, what the loader says)
    # the lock schema's target keys use the same pattern
    lock_targets = _lock_schema()["properties"]["cases"]["propertyNames"]["pattern"]
    assert lock_targets == schema["$defs"]["label"]["pattern"]


def test_lock_schema_rejects_padded_case_paths():
    validator = jsonschema.Draft202012Validator(_lock_schema())
    validator.validate(yaml.safe_load(LOCK))
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(yaml.safe_load(LOCK.replace('"[target]"', '" [target]"')))


def _report_schema():
    for base in (_ROOT, os.environ.get("TEST_SRCDIR", "") + "/_main"):
        path = os.path.join(base, "schema", "report.v2.schema.json")
        if os.path.exists(path):
            with open(path) as fh:
                return json.load(fh)
    pytest.skip("schema not available")


def test_report_schema_accepts_generated_reports_and_rejects_a_list_owner():
    """Every report the attribution fuzz generates fits schema/report.v2; a
    case whose owner is a list (two owners) does not."""
    from test_checkreport import _fuzz_reports

    validator = jsonschema.Draft202012Validator(_report_schema())
    doc = None
    for _, doc in _fuzz_reports(300, 99):
        validator.validate(doc)
    owned = next(r for _, d in _fuzz_reports(300, 98) for r in d["cases"] if r["owner"]) if doc else None
    bad = {**doc, "cases": [{**owned, "owner": [owned["owner"], "REQ-2"]}]}
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(bad)


@pytest.mark.parametrize(
    "golden",
    [
        "tests/integration/report.golden.json",
        "tests/integration/rr_case_report.golden.json",
        "examples/thermostat/report.golden.json",
    ],
)
def test_report_schema_accepts_the_checked_in_goldens(golden):
    from rules_requirements import checkreport

    path = os.path.join(_ROOT, golden)
    if not os.path.exists(path):
        pytest.skip(f"{golden} not available")
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    jsonschema.Draft202012Validator(_report_schema()).validate(doc)
    assert checkreport.check_report(doc) == []
