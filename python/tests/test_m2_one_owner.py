# SPDX-License-Identifier: AGPL-3.0-or-later
"""The M2 (v0.3.0) milestone gate: one test case verifies at most one requirement.

Since ingest records every declared id (B2), a case can name two ids through
paths v0.2 never read: ``requirement="REQ-1 REQ-2"`` (whitespace) and an
``[rr:ID]`` name tag next to a property. Attribution (B3) turns such a case
into a ``multi-tag`` quarantine, and the verdicts (B4) read nothing but
attribution, so the case counts for neither id and both read INVALID.

These tests keep it that way:

* ``test_multi_tag_cases_verify_no_requirement`` is the milestone regression
  test, with exactly the review's input (a strict xfail until B4 landed).
* ``test_no_release_without_quarantine`` fails as soon as the package claims
  0.3.0 (or later) while trace.py cannot quarantine.
* ``test_evidence_guide_says_so_while_pending`` keeps the evidence guide
  honest: it carried an interim note exactly while quarantine was pending.
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


def test_multi_tag_cases_verify_no_requirement(matrix):
    mx, _ = matrix
    assert not PENDING, "trace.py has no INVALID status: multi-tag cases would count for every id they name"
    for rid in ("REQ-1", "REQ-2"):
        verdict = mx.verdicts[rid]
        assert verdict.status == INVALID, (rid, verdict.status)
        # Neither case counts as passing evidence of either requirement.
        assert not [e for e in verdict.evidence if e.status == "passed"], (rid, verdict.evidence)
        assert [m.state for m in verdict.members] == ["quarantined", "quarantined"], verdict.members
    # Both cases are quarantined multi-tags that own nothing.
    att = mx.attribution
    assert [(q.code, q.entities) for q in att.quarantined] == [("multi-tag", ("REQ-1", "REQ-2"))] * 2
    assert dict(att.owner) == {}


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
