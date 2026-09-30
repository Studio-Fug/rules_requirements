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
