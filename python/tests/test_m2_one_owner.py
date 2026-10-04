# SPDX-License-Identifier: AGPL-3.0-or-later
"""The M2 (v0.3.0) milestone gate: one test case verifies at most one requirement.

Since ingest records every declared id (B2), a case can name two ids through
paths v0.2 never read: ``requirement="REQ-1 REQ-2"`` (whitespace) and an
``[rr:ID]`` name tag next to a property. Only attribution (B3) and a verdict
engine that reads nothing but attribution (B4) turn such a case into a
``multi-tag`` quarantine. Until both have landed, the v0.2 engine in trace.py
still counts that case once for each id: the intermediate tree must never be
released, tagged or shipped on its own.

These tests make that mechanical:

* ``test_multi_tag_cases_verify_no_requirement`` is the milestone regression
  test, with exactly the review's input. It is a strict xfail while trace.py
  has no INVALID status, and a plain test once it has one, so B4 cannot land
  without making it pass (and cannot forget to drop the marker).
* ``test_no_release_without_quarantine`` fails as soon as the package claims
  0.3.0 (or later) while trace.py cannot quarantine.
* ``test_evidence_guide_says_so_while_pending`` keeps the evidence guide
  honest: it carries the interim note exactly while quarantine is pending.
"""

import os
import re

import pytest
from conftest import write

import rules_requirements
from rules_requirements import ingest, trace
from rules_requirements.case_keys import index_cases
from rules_requirements.model import read_model

INVALID = getattr(trace, "INVALID", None)
PENDING = INVALID is None  # B4 (verdicts from attribution) has not landed
INTERIM_MARKER = "<!-- rr:interim multi-tag quarantine pending -->"

# The review's repro (M2 B2 finding 1): two cases, each naming REQ-1 and REQ-2.
XML = (
    '<testsuite name="s">'
    '<testcase classname="c" name="space"><properties>'
    '<property name="requirement" value="REQ-1 REQ-2"/></properties></testcase>'
    '<testcase classname="c" name="tagged [rr:REQ-2]"><properties>'
    '<property name="requirement" value="REQ-1"/></properties></testcase>'
    "</testsuite>"
)


@pytest.fixture
def matrix(tmp_path, model_path):
    model, _ = read_model(model_path)
    assert {"REQ-1", "REQ-2"} <= set(model.requirements)
    ev = ingest.collect([write(tmp_path, "r.xml", XML)])
    return trace.build_matrix(model, ev), ev


def test_ingest_keeps_both_ids_of_each_case(matrix):
    """What B2 itself guarantees: neither case loses an id (each is a multi-tag)."""
    _, ev = matrix
    assert [r.declared for r in index_cases(ev).values()] == [("REQ-1", "REQ-2"), ("REQ-1", "REQ-2")]


@pytest.mark.xfail(PENDING, strict=True, reason="multi-tag quarantine lands with attribution (M2 B3+B4)")
def test_multi_tag_cases_verify_no_requirement(matrix):
    mx, _ = matrix
    for rid in ("REQ-1", "REQ-2"):
        verdict = mx.verdicts[rid]
        assert verdict.status == INVALID, (rid, verdict.status)
        # Neither case counts as passing evidence of either requirement.
        assert not [e for e in verdict.evidence if e.status == "passed"], (rid, verdict.evidence)


def _version(text):
    return tuple(int(x) for x in re.findall(r"\d+", text)[:3])


def test_no_release_without_quarantine():
    if PENDING:
        assert _version(rules_requirements.__version__) < (0, 3, 0), (
            "rules_requirements claims 0.3.0 but trace.py cannot quarantine a multi-tag case: "
            "B2's ingest must not ship without attribution (B3) and the verdicts that read it (B4)"
        )


def test_evidence_guide_says_so_while_pending():
    guide = os.path.join(os.path.dirname(__file__), "..", "..", "docs", "guides", "evidence.md")
    if not os.path.exists(guide):  # not in the Bazel runfiles: the Python suite checks it
        pytest.skip("docs/guides/evidence.md is not available here")
    with open(guide, encoding="utf-8") as fh:
        text = fh.read()
    assert (INTERIM_MARKER in text) == PENDING
