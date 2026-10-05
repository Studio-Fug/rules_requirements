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
        {
            "case": row["case"],
            "target": row["target"],
            "selector": "*",
            "via": "model",
            "state": "passed",
            "owned": True,
        }
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


def _sample(owned=True, quarantined=False, where=None):
    for _trial, doc in _fuzz_reports(400, SEED + 7):
        if owned and not doc["summary"]["test_cases_owned"]:
            continue
        if quarantined and not doc["summary"]["test_cases_quarantined"]:
            continue
        if where is not None and not where(doc):
            continue
        return doc
    raise AssertionError("the fuzz produced no such report")


def _entity(doc, eid):
    return next(e for s in ("user_needs", "requirements", "mitigations", "risks", "test_methods") for e in doc[s]
                if e["id"] == eid)  # fmt: skip


def _add_member(ent, member):
    ent["members"].append(member)
    ent["set"]["members"] += 1
    ent["set"][member["state"].replace("-", "_")] += 1


def _drop_members(ent, pred):
    """Remove ``ent``'s members matching ``pred``, its set counts adjusted."""
    keep = []
    for mb in ent["members"]:
        if pred(mb):
            ent["set"]["members"] -= 1
            ent["set"][mb["state"].replace("-", "_")] -= 1
        else:
            keep.append(mb)
    ent["members"] = keep


def _plain_owned_row(doc):
    """An owned case of a target that is neither tainted nor synthetic-only."""
    for row in doc["cases"]:
        trow = doc["attribution"]["targets"][row["target"]]
        if row["owner"] is not None and not trow.get("tainted") and not trow.get("synthetic"):
            return row
    return None


def _drop_row(doc, row):
    """Remove a case row and fix every count that counts it (summary, target, granularity)."""
    doc["cases"].remove(row)
    doc["summary"]["test_cases"] -= 1
    t = doc["attribution"]["targets"][row["target"]]
    t["cases"] -= 1
    if row["owner"] is not None:
        doc["summary"]["test_cases_owned"] -= 1
        t["owned"] -= 1
        t["owners"] = sorted({r["owner"] for r in doc["cases"] if r["target"] == row["target"] and r["owner"]})
        g = doc["attribution"]["granularity"]
        kind = next(k for k in ("owned_by_tag", "owned_by_literal", "owned_by_pattern", "owned_by_whole") if g[k])
        g[kind] -= 1
    else:
        doc["summary"]["test_cases_unowned"] -= 1


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


# --- regressions: each hole the B6 review found ------------------------------


def test_every_member_says_whether_it_is_owned():
    doc = _sample()
    for ent in _verifiable(doc):
        for mb in ent["members"]:
            assert isinstance(mb["owned"], bool)
            assert mb["owned"] == (mb["case"] is not None and doc_owner(doc, mb["case"]) == ent["id"]), mb
    ent = next(e for e in _verifiable(doc) if e["members"])
    del ent["members"][0]["owned"]
    assert any("does not say whether it is owned" in p for p in checkreport.check_report(doc))


def doc_owner(doc, case):
    return next((r["owner"] for r in doc["cases"] if r["case"] == case), None)


def test_an_owned_error_member_missing_from_cases_still_counts_toward_the_partition():
    """A5: an owned case dropped from cases[] and listed as an ``error`` member
    under its owner and a second entity (every count fixed) is rejected:
    owned members count whether or not cases[] lists them."""
    doc = _sample(where=_plain_owned_row)
    row = _plain_owned_row(doc)
    _drop_row(doc, row)
    owner = _entity(doc, row["owner"])
    for mb in owner["members"]:
        if mb["case"] == row["case"]:
            owner["set"][mb["state"]] -= 1
            owner["set"]["error"] += 1
            mb["state"] = "error"
    second = next(e for e in _verifiable(doc) if e["id"] != row["owner"])
    _add_member(second, {"case": row["case"], "target": row["target"], "selector": "*", "via": "model",
                         "state": "error", "owned": True})  # fmt: skip
    for ent in (owner, second):  # the evidence[] view follows the members
        ent["evidence"] = [e for e in ent.get("evidence", []) if f"{e['target']}#{e['name']}" != row["case"]]
    problems = checkreport.check_report(doc)
    assert any(f"{row['case']} appears under 2 entities" in p for p in problems), problems
    assert any(f"{row['case']} is an owned (error) member of {second['id']} but no case" in p for p in problems)


def test_an_error_pseudo_member_must_name_an_absent_case_of_a_tainted_or_synthetic_target():
    """Marking the two error members of A5 not owned does not help: an error
    pseudo-member names no case of the report, on a target that ran tainted
    or synthetic-only; an error member of a listed case is owned."""
    doc = _sample(where=_plain_owned_row)
    row = _plain_owned_row(doc)
    owner = _entity(doc, row["owner"])
    second = next(e for e in _verifiable(doc) if e["id"] != row["owner"])
    pseudo = {"case": row["case"], "target": row["target"], "selector": "*", "via": "model", "state": "error",
              "owned": False}  # fmt: skip
    listed = copy.deepcopy(doc)
    _add_member(_entity(listed, second["id"]), dict(pseudo))
    assert any("so an error member of it is owned" in p for p in checkreport.check_report(listed))
    _drop_row(doc, row)
    _drop_members(owner, lambda mb: mb["case"] == row["case"])
    owner["evidence"] = [e for e in owner.get("evidence", []) if f"{e['target']}#{e['name']}" != row["case"]]
    for ent in (owner, second):
        _add_member(ent, dict(pseudo))
    problems = checkreport.check_report(doc)
    assert any("is neither tainted nor synthetic-only" in p for p in problems), problems
    passed = copy.deepcopy(doc)
    for mb in _entity(passed, second["id"])["members"]:
        if mb["case"] == row["case"]:
            mb["state"] = "passed"
    assert any("is passed but not owned" in p for p in checkreport.check_report(passed))


def test_the_fuzz_writes_error_pseudo_members_of_one_absent_key_under_two_entities_and_they_pass():
    """The legitimate shape A5 imitates: two literal claims of an absent case
    on a tainted target. Each is an ``error`` pseudo-member (owned: false)."""
    found = 0
    for _trial, doc in _fuzz_reports(1500, 99):
        seen = {}
        for ent in _verifiable(doc):
            for mb in ent["members"]:
                if mb["state"] == "error" and not mb["owned"] and mb["case"] is not None:
                    seen.setdefault(mb["case"], set()).add(ent["id"])
        if any(len(v) > 1 for v in seen.values()):
            found += 1
            assert checkreport.check_report(doc) == []
    assert found


def _quarantine_sample():
    return _sample(
        owned=False,
        quarantined=True,
        where=lambda d: any(len([e for e in q["entities"] if e in {x["id"] for x in _verifiable(d)}]) >= 2
                            for q in d["attribution"]["quarantined"]),
    )  # fmt: skip


def _two_named(doc):
    ids = {x["id"] for x in _verifiable(doc)}
    return next(q for q in doc["attribution"]["quarantined"] if len([e for e in q["entities"] if e in ids]) >= 2)


def test_quarantine_entities_must_be_a_list_of_ids():
    """A3: entities as a string ("REQ-1,REQ-3") used to iterate characters."""
    doc = _quarantine_sample()
    q = _two_named(doc)
    q["entities"] = ",".join(q["entities"])
    assert any("is not a list of ids" in p for p in checkreport.check_report(doc))


def test_an_entity_a_quarantine_names_cannot_drop_its_quarantined_member_and_read_verified():
    """A3b/A3c: the named entity's quarantined member removed (and its status
    flipped) while the quarantine stops naming it is still rejected: the ids a
    quarantine names follow from its code (declared ids, claims)."""
    doc = _quarantine_sample()
    q = _two_named(doc)
    ids = {x["id"] for x in _verifiable(doc)}
    gone = next(e for e in q["entities"] if e in ids)
    ent = _entity(doc, gone)
    _drop_members(ent, lambda mb: mb["state"] == "quarantined" and mb["case"] == q["case"])
    named = copy.deepcopy(doc)
    assert any(f"names {gone}, which holds no quarantined member" in p for p in checkreport.check_report(named))
    q["entities"] = [e for e in q["entities"] if e != gone]
    problems = checkreport.check_report(doc)
    assert any(f"{q['case']} ({q['code']}) names" in p and "give" in p for p in problems), problems
    q["entities"] = []
    assert any("give" in p for p in checkreport.check_report(doc))


def test_a_quarantined_member_under_an_entity_the_quarantine_does_not_name_is_rejected():
    """A6."""
    doc = _quarantine_sample()
    q = doc["attribution"]["quarantined"][0]
    other = next(e for e in _verifiable(doc) if e["id"] not in q["entities"])
    other["status"] = "INVALID"
    _add_member(other, {"case": q["case"], "target": q["case"].split("#")[0], "selector": "tag", "via": "tag",
                        "state": "quarantined", "owned": False})  # fmt: skip
    problems = checkreport.check_report(doc)
    assert any(f"{other['id']} holds {q['case']} as quarantined, but its quarantine" in p for p in problems), problems


@pytest.mark.parametrize("state", ["missing", "not-run"])
def test_a_missing_or_not_run_pseudo_member_cannot_name_a_case_of_the_report(state):
    """V12: a not-owned missing/not-run member of REQ-1 whose key is a case
    row another entity owns would put one case in two verification sets.
    attribute() writes those states only for keys the evidence lacks (a
    present case it does not own is ``moved``), so check-report rejects it;
    the same member naming an absent key is still accepted."""
    doc = _sample(where=_plain_owned_row)
    row = _plain_owned_row(doc)
    other = next(e for e in _verifiable(doc) if e["id"] != row["owner"])
    pseudo = {"case": row["case"], "target": row["target"], "selector": "*", "via": "model", "state": state,
              "owned": False}  # fmt: skip
    bad = copy.deepcopy(doc)
    _add_member(_entity(bad, other["id"]), dict(pseudo))
    problems = checkreport.check_report(bad)
    assert any(
        f"{other['id']}: member {row['case']} is {state}, but it is a case of this report (owned by {row['owner']})"
        in p
        for p in problems
    ), problems
    absent = copy.deepcopy(doc)
    _add_member(_entity(absent, other["id"]), {**pseudo, "case": row["target"] + "#no::such_case"})
    assert checkreport.check_report(absent) == []
    moved = copy.deepcopy(doc)
    _add_member(_entity(moved, other["id"]), {**pseudo, "state": "moved", "via": "lock"})
    assert checkreport.check_report(moved) == []


def test_the_evidence_view_is_exactly_the_owned_members():
    """A1: an owned case appended to a second requirement's evidence[] (the
    0.3.x compatibility view) is rejected, as is a dropped one."""
    doc = _sample()
    row = next(r for r in doc["cases"] if r["owner"] is not None)
    other = next(e for e in _verifiable(doc) if e["id"] != row["owner"])
    added = copy.deepcopy(doc)
    ent = _entity(added, other["id"])
    ent.setdefault("evidence", []).append({"name": row["path"], "status": "passed", "level": "simulation",
                                           "target": row["target"]})  # fmt: skip
    problems = checkreport.check_report(added)
    assert any(f"evidence[] is not the view of its owned members (it lists {row['case']}" in p for p in problems)
    dropped = copy.deepcopy(doc)
    ent = _entity(dropped, row["owner"])
    ent["evidence"] = [e for e in ent["evidence"] if f"{e['target']}#{e['name']}" != row["case"]]
    assert any(f"{row['owner']}: evidence[] is not the view" in p for p in checkreport.check_report(dropped))
    # The right key with another status, level or staleness is not the view either.
    for field, value in (("status", "failed"), ("level", "hardware"), ("stale", True)):
        changed = copy.deepcopy(doc)
        ent = _entity(changed, row["owner"])
        entry = next(e for e in ent["evidence"] if f"{e['target']}#{e['name']}" == row["case"])
        if entry.get(field) == value:
            value = {"status": "passed", "level": "simulation", "stale": False}[field]
        entry[field] = value
        problems = checkreport.check_report(changed)
        assert f"{row['owner']}: evidence[] is not the view of its owned members" in problems, (field, problems)
    risk = copy.deepcopy(doc)
    _entity(risk, "RISK-1")["evidence"] = [{"name": row["path"], "status": "passed", "level": "simulation",
                                            "target": row["target"]}]  # fmt: skip
    assert any("RISK-1: evidence[] is not the view" in p for p in checkreport.check_report(risk))


def test_load_report_refuses_a_duplicate_key(tmp_path):
    """A2: ``"owner": "REQ-2", "owner": "REQ-1"`` reads REQ-1 to a last-wins
    parser and REQ-2 to a first-wins one (or a person)."""
    doc = _sample()
    text = json.dumps(doc, indent=1)
    at = text.index('"owner": "')
    path = tmp_path / "r.json"
    path.write_text(text[:at] + '"owner": "REQ-2",\n' + text[at:])
    with pytest.raises(checkreport.AmbiguousReportError, match="'owner' appears twice"):
        checkreport.load_report(str(path))
    path.write_text(text.replace('"requirements": [', '"requirements": [], "requirements": [', 1))
    with pytest.raises(checkreport.AmbiguousReportError, match="'requirements' appears twice"):
        checkreport.load_report(str(path))
    path.write_text(text)
    assert checkreport.load_report(str(path)) == doc


# --- one test per check: each fails if its check is removed ------------------


def test_an_owned_member_moved_to_another_entity_is_rejected():
    doc = _sample()
    row = next(r for r in doc["cases"] if r["owner"] is not None)
    owner = _entity(doc, row["owner"])
    other = next(e for e in _verifiable(doc) if e["id"] != row["owner"])
    (moved,) = [mb for mb in owner["members"] if mb["case"] == row["case"]]
    _drop_members(owner, lambda mb: mb is moved)
    _add_member(other, moved)
    problems = checkreport.check_report(doc)
    assert any(f"{row['case']} is an owned ({moved['state']}) member of {other['id']} but owned by {row['owner']}" in p
               for p in problems), problems  # fmt: skip
    assert any(f"{row['case']} is owned by {row['owner']} but is no member of it" in p for p in problems)


def test_an_owner_that_is_a_risk_is_rejected():
    doc = _sample()
    row = next(r for r in doc["cases"] if r["owner"] is not None)
    row["owner"] = "RISK-1"
    assert any("owner 'RISK-1' is no user need, requirement or mitigation" in p for p in checkreport.check_report(doc))


def test_a_case_row_listed_twice_with_another_owner_is_rejected():
    doc = _sample()
    row = next(r for r in doc["cases"] if r["owner"] is not None)
    twin = dict(row, owner=next(e["id"] for e in _verifiable(doc) if e["id"] != row["owner"]))
    doc["cases"].append(twin)
    assert any(f"{row['case']} is listed twice in cases" in p for p in checkreport.check_report(doc))


def test_a_quarantine_code_that_disagrees_with_its_case_row_is_rejected():
    doc = _sample(owned=False, quarantined=True)
    q = doc["attribution"]["quarantined"][0]
    row = next(r for r in doc["cases"] if r["case"] == q["case"])
    row["quarantine"] = "attribution-conflict" if q["code"] != "attribution-conflict" else "multi-tag"
    assert any(f"{q['case']}: cases says quarantine" in p for p in checkreport.check_report(doc))


def test_an_entity_holding_a_quarantined_case_must_read_invalid():
    doc = _sample(owned=False, quarantined=True, where=lambda d: any(
        mb["state"] == "quarantined" for e in _verifiable(d) for mb in e["members"]))  # fmt: skip
    ent = next(e for e in _verifiable(doc) if any(mb["state"] == "quarantined" for mb in e["members"]))
    ent["status"] = "VERIFIED"
    assert any(f"{ent['id']} holds a quarantined case but reads VERIFIED" in p for p in checkreport.check_report(doc))
