# SPDX-License-Identifier: AGPL-3.0-or-later
"""``rr check-report``: the one-owner partition re-proved from the report JSON alone.

It accepts every report the seeded fuzz of test_attribution generates, and
rejects each way a report could claim that one test case verifies two
requirements (a key injected under a second entity, a list owner, an owned
quarantined case, a named entity that is not INVALID, counts that disagree).
"""

import copy
import json
import random

import pytest
from test_attribution import _fuzz_evidence, _fuzz_lock, _fuzz_model

from rules_requirements import checkreport, report
from rules_requirements.trace import build_matrix

TRIALS = 5000
SEED = 20261005


def _fuzz_reports(trials=TRIALS, seed=SEED):
    rnd = random.Random(seed)
    for trial in range(trials):
        model = _fuzz_model(rnd)
        ev = _fuzz_evidence(rnd)
        lock = _fuzz_lock(rnd, ev)
        mx = build_matrix(model, ev, current_build=rnd.choice([None, {"sha": "new"}]), lock=lock)
        # Through JSON text, as the published file is read.
        yield trial, json.loads(report.render_json(mx))


def _verifiable(doc):
    return [e for s in ("user_needs", "requirements", "mitigations") for e in doc[s]]


def _inject(doc, rnd):
    """Copy an owned case into a second entity's members (its set counts
    adjusted, so only the partition itself is wrong). None if nothing is owned."""
    owned = [r for r in doc["cases"] if r["owner"] is not None]
    if not owned:
        return None
    row = rnd.choice(owned)
    other = rnd.choice([e for e in _verifiable(doc) if e["id"] != row["owner"]])
    other["members"].append(
        {"case": row["case"], "target": row["target"], "selector": "*", "via": "model", "state": "passed"}
    )
    other["set"]["members"] += 1
    other["set"]["passed"] += 1
    return row["case"], other["id"]


def test_check_report_accepts_every_fuzz_generated_report_and_rejects_an_injected_second_owner():
    rnd = random.Random(SEED + 1)
    seen = {"owned": 0, "quarantined": 0, "injected": 0}
    for trial, doc in _fuzz_reports():
        assert checkreport.check_report(doc) == [], trial
        seen["owned"] += doc["summary"]["test_cases_owned"]
        seen["quarantined"] += doc["summary"]["test_cases_quarantined"]
        bad = copy.deepcopy(doc)
        injected = _inject(bad, rnd)
        if injected is None:
            continue
        seen["injected"] += 1
        case, second = injected
        problems = checkreport.check_report(bad)
        assert any(f"{case} appears under 2 entities" in p and second in p for p in problems), (trial, problems)
    assert all(n > 100 for n in seen.values()), seen


def _sample(owned=True, quarantined=False):
    for _trial, doc in _fuzz_reports(400, SEED + 7):
        if owned and not doc["summary"]["test_cases_owned"]:
            continue
        if quarantined and not doc["summary"]["test_cases_quarantined"]:
            continue
        return doc
    raise AssertionError("the fuzz produced no such report")


def test_check_report_rejects_a_list_owner():
    doc = _sample()
    row = next(r for r in doc["cases"] if r["owner"] is not None)
    row["owner"] = [row["owner"], "REQ-2"]
    assert any("is not a scalar id" in p for p in checkreport.check_report(doc))


def test_check_report_rejects_an_owned_quarantined_case_and_a_named_entity_that_is_not_invalid():
    doc = _sample(owned=False, quarantined=True)
    q = doc["attribution"]["quarantined"][0]
    owned = copy.deepcopy(doc)
    row = next(r for r in owned["cases"] if r["case"] == q["case"])
    row["owner"], row["via"] = q["entities"][0] if q["entities"] else "REQ-1", "model"
    assert any(f"{q['case']} is quarantined" in p and "owned by" in p for p in checkreport.check_report(owned))
    named = [e for e in _verifiable(doc) if e["id"] in q["entities"]]
    if named:
        valid = copy.deepcopy(doc)
        ent = next(e for e in _verifiable(valid) if e["id"] == named[0]["id"])
        ent["status"] = "VERIFIED"
        assert any(
            f"names {ent['id']}, which reads VERIFIED, not INVALID" in p for p in checkreport.check_report(valid)
        )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["summary"].__setitem__("test_cases", d["summary"]["test_cases"] + 1),
        lambda d: d["summary"].__setitem__("test_cases_owned", d["summary"]["test_cases_owned"] + 1),
        lambda d: d["cases"].append(dict(d["cases"][0])),
        lambda d: d["requirements"][0]["set"].__setitem__("members", d["requirements"][0]["set"]["members"] + 1),
        lambda d: next(iter(d["attribution"]["targets"].values())).__setitem__("cases", 99),
        lambda d: d["attribution"]["granularity"].__setitem__("owned_by_tag", 99),
    ],
    ids=["summary-cases", "summary-owned", "duplicate-row", "set-count", "target-count", "granularity"],
)
def test_check_report_rejects_counts_that_disagree(mutate):
    doc = _sample()
    assert checkreport.check_report(doc) == []
    mutate(doc)
    assert checkreport.check_report(doc)


def test_check_report_needs_a_v2_report(tmp_path):
    assert checkreport.check_report({"schema": "rules_requirements/report/v1"})
    bad = tmp_path / "r.json"
    bad.write_text('{"schema": "rules_requirements/report/v1"}')
    with pytest.raises(checkreport.ReportError, match="report/v2"):
        checkreport.load_report(str(bad))
    bad.write_text("{nope")
    with pytest.raises(checkreport.ReportError, match="cannot read"):
        checkreport.load_report(str(bad))
