# SPDX-License-Identifier: AGPL-3.0-or-later
"""``rr check-report``: the one-owner partition re-proved from the report JSON alone.

It accepts every report the seeded fuzz of test_attribution generates, and
rejects each way a report could claim that one test case verifies two
requirements (a key injected under a second entity, a list owner, an owned
quarantined case, a named entity that is not INVALID, counts that disagree).
"""

import copy
import json
import os
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


# --- the 0.3.0 release review: what check-report re-proves from the JSON alone ---


def _small_report(tmp_path, *, mode="model", config="", cases=None):
    """A real report: REQ-1 owns //a:t's cases, REQ-2 //c:t's (claimed in model
    mode; in hybrid mode there are no claims and the cases' tags own them)."""
    from test_attribution import evidence, model_at, tc

    claims = mode == "model"
    reqs = (
        "  - {id: REQ-1, title: a, satisfies: [UN-1]"
        + (", verified_by: [{target: //a:t, cases: ['m::*']}]" if claims else "")
        + "}\n  - {id: REQ-2, title: b, satisfies: [UN-1]"
        + (", verified_by: [{target: //c:t, cases: ['*']}]" if claims else "")
        + "}\n  - {id: REQ-3, title: c, satisfies: [UN-1]}\n"
    )
    model = model_at(tmp_path, reqs, config="{attribution: " + mode + (", " + config if config else "") + "}")
    cases = cases or [
        tc("t1", classname="m", target="//a:t", declared=("REQ-1",), properties={"rr.file": "f.py"}),
        tc("\u00e9t\u00e9", classname="m", target="//a:t", declared=("REQ-1",)),
        tc("z", classname="k", target="//c:t", declared=("REQ-2",)),
    ]
    from rules_requirements.lock import NO_LOCK

    doc = json.loads(report.render_json(build_matrix(model, evidence(*cases), lock=NO_LOCK)))
    assert checkreport.check_report(doc) == [], checkreport.check_report(doc)
    return doc


def _forge(doc, src_key, target, path, owner, *, file=None):
    """Copy the owned row ``src_key`` under ``target#path``, owned by ``owner``,
    with every count consistent (members, evidence[], targets, summary,
    granularity): only the key spelling, or the code it runs, is wrong."""
    d = copy.deepcopy(doc)
    row = next(r for r in d["cases"] if r["case"] == src_key)
    src_member = next(m for e in _verifiable(d) for m in e["members"] if m["case"] == src_key and m["owned"])
    key = f"{target}#{path}"
    new = dict(row, case=key, target=target, path=path, owner=owner, declared=[owner])
    new.pop("file", None)
    if file:
        new["file"] = file
    d["cases"].append(new)
    ent = _entity(d, owner)
    member = {k: v for k, v in src_member.items() if k not in ("case", "target", "selector")}
    _add_member(ent, {"case": key, "target": target, "selector": "*", **member})
    ent.setdefault("evidence", []).append(
        {"name": path, "status": row["status"], "level": src_member.get("level", ""), "target": target}
    )
    t = d["attribution"]["targets"].setdefault(
        target, {"cases": 0, "owned": 0, "quarantined": 0, "owners": [], "synthetic": False, "ran": True}
    )
    t["cases"] += 1
    t["owned"] += 1
    t["owners"] = sorted(set(t["owners"]) | {owner})
    d["summary"]["test_cases"] += 1
    d["summary"]["test_cases_owned"] += 1
    d["attribution"]["granularity"]["owned_by_" + ("tag" if row["via"] == "tag" else "pattern")] += 1
    return d


_T1 = "//a:t#m::t1"
_ETE = "//a:t#m::\u00e9t\u00e9"


@pytest.mark.parametrize(
    ("target", "path", "config", "said"),
    [
        ("@//a:t", "m::t1", "", "is not canonical"),
        ("@@//a:t", "m::t1", "", "is not canonical"),
        ("@ws//a:t", "m::t1", "main_repo: ws", "is not canonical"),
        ("//a", "m::t1", "", "is not canonical"),  # not //a:a, but a spelling no report writes
        ("//a:t", "m::t1 ", "", "is not canonical"),
        ("//a:t", " m::t1", "", "is not canonical"),
        ("//a:t", "m::t1 [rr:REQ-2]", "", "name tag"),
        ("//a:t", "m::e\u0301te\u0301", "", "is not canonical"),  # NFD
    ],
    ids=["at", "atat", "at-main-repo", "short-label", "trailing-blank", "leading-blank", "name-tag", "nfd"],
)
def test_check_report_rejects_a_case_owned_twice_under_another_spelling_of_its_key(
    tmp_path, target, path, config, said
):
    """Release review (high): rows keyed on raw strings let one case be owned
    by REQ-1 as //a:t#m::t1 and by REQ-2 as @//a:t#m::t1 (or with a trailing
    blank, an [rr:ID] tag, NFD...): by the tool's own definition one case with
    two owners. The identical spelling was always rejected (the control)."""
    doc = _small_report(tmp_path, config=config)
    src = _T1 if "t1" in path else _ETE
    assert checkreport.check_report(_forge(doc, src, "//a:t", src.partition("#")[2], "REQ-2"))  # the control
    forged = _forge(doc, src, target, path, "REQ-2")
    problems = checkreport.check_report(forged)
    assert any(said in p and f"{target}#{path}" in p for p in problems), problems


def test_check_report_reads_main_repo_from_the_report(tmp_path):
    """``@ws//a:t`` is another repository unless the report says ws is the main one."""
    doc = _small_report(tmp_path, config="main_repo: ws, variants: [['//a:t', '@ws//b:t']]")
    assert doc["attribution"]["main_repo"] == "ws"
    assert doc["attribution"]["variants"] == [["//a:t", "//b:t"]]
    forged = _forge(doc, _T1, "@ws//a:t", "m::t1", "REQ-2")
    assert any("is not canonical" in p for p in checkreport.check_report(forged))
    forged["attribution"]["main_repo"] = ""
    assert not any("is not canonical" in p for p in checkreport.check_report(forged))
    forged["attribution"]["main_repo"] = 7
    assert any("main_repo" in p for p in checkreport.check_report(forged))


@pytest.mark.parametrize("state", ["missing", "not-run", "moved"])
def test_a_pseudo_member_under_another_spelling_of_an_owned_case_is_rejected(tmp_path, state):
    """A missing/not-run member of REQ-2 naming REQ-1's case as @@//a:t#m::t1
    evaded the 'names a case of the report' check, which compared raw strings."""
    doc = _small_report(tmp_path)
    _add_member(_entity(doc, "REQ-2"), {"case": "@@//a:t#m::t1", "target": "@@//a:t", "selector": "m::t1",
                                        "via": "model", "state": state, "owned": False})  # fmt: skip
    problems = checkreport.check_report(doc)
    assert any("REQ-2: member @@//a:t#m::t1: its target '@@//a:t' is not canonical" in p for p in problems), problems


def test_a_targets_entry_under_another_spelling_is_rejected(tmp_path):
    doc = _small_report(tmp_path)
    doc["attribution"]["targets"]["@//a:t"] = dict(doc["attribution"]["targets"]["//a:t"], cases=0, owned=0, owners=[])
    assert any("attribution.targets: its target '@//a:t'" in p for p in checkreport.check_report(doc))


def test_a_row_without_string_target_and_path_is_rejected(tmp_path):
    doc = _small_report(tmp_path)
    row = next(r for r in doc["cases"] if r["case"] == _T1)
    row["target"] = None
    assert any("target and path must be strings" in p for p in checkreport.check_report(doc))


def test_check_report_rejects_one_test_code_owned_by_two_entities_in_two_targets(tmp_path):
    """Release review (medium): the same file and case path in //a:t (REQ-1)
    and //b:t (REQ-2) is what attribute() quarantines as
    same-code-multiple-owners; the audit re-proves it from file, path, target."""
    doc = _small_report(tmp_path)
    forged = _forge(doc, _T1, "//b:t", "m::t1", "REQ-2", file="f.py")
    problems = checkreport.check_report(forged)
    assert any("run the same test code (file f.py, path m::t1) but are owned by REQ-1, REQ-2" in p for p in problems)
    other_file = _forge(doc, _T1, "//b:t", "m::t1", "REQ-2", file="g.py")
    assert checkreport.check_report(other_file) == []  # two files: two tests
    same_owner = _forge(doc, _T1, "//b:t", "m::t1", "REQ-1", file="f.py")
    assert checkreport.check_report(same_owner) == []


def test_check_report_rejects_one_path_owned_twice_across_declared_variants(tmp_path):
    doc = _small_report(tmp_path, config="variants: [['//a:t', '//b:t']]")
    forged = _forge(doc, _T1, "//b:t", "m::t1", "REQ-2")  # no file recorded: only the variants say so
    problems = checkreport.check_report(forged)
    assert any("variants //a:t, //b:t, path m::t1" in p for p in problems), problems
    forged["attribution"]["variants"] = "//a:t,//b:t"
    assert any("attribution.variants" in p for p in checkreport.check_report(forged))


def test_check_report_rejects_a_pseudo_target_path_owned_by_another_entity_without_recorded_sources(tmp_path):
    doc = _small_report(tmp_path)
    forged = _forge(doc, _T1, "suite:pytest", "m::t1", "REQ-2")
    problems = checkreport.check_report(forged)
    assert any("under a pseudo-target" in p for p in problems), problems
    pinned = _forge(doc, _T1, "suite:pytest", "m::t1", "REQ-2", file="g.py")
    assert checkreport.check_report(pinned) == []


@pytest.mark.parametrize("via", ["model", "tag"])
def test_check_report_rejects_an_owned_case_that_declares_two_ids(tmp_path, via):
    """Release review (medium): attribute() quarantines a case naming two ids
    (multi-tag) before it reads a claim, so such a row owns nothing."""
    mode = "hybrid" if via == "tag" else "model"
    doc = _small_report(tmp_path, mode=mode)
    row = next(r for r in doc["cases"] if r["case"] == _T1)
    assert row["via"] == via
    row["declared"] = ["REQ-1", "REQ-2"]
    problems = checkreport.check_report(doc)
    assert any(f"{_T1} declares REQ-1, REQ-2 but is owned by REQ-1" in p for p in problems), problems


def test_check_report_rejects_a_tag_owner_its_tag_does_not_name_and_tags_owning_in_model_mode(tmp_path):
    doc = _small_report(tmp_path, mode="hybrid")
    other = copy.deepcopy(doc)
    next(r for r in other["cases"] if r["case"] == _T1)["declared"] = ["REQ-3"]
    assert any(
        "owned by REQ-1 through its tag, but it declares ['REQ-3']" in p for p in checkreport.check_report(other)
    )
    model_mode = copy.deepcopy(doc)
    model_mode["attribution"]["mode"] = "model"
    problems = checkreport.check_report(model_mode)
    assert any("through its tag in a 'model' report" in p for p in problems), problems
    assert any("is passed via 'tag' in a model-mode report" in p for p in problems), problems


def test_check_report_rejects_an_owned_member_whose_state_is_not_its_cases_result(tmp_path):
    """Release review (low, a): the row says failed, the member passed, the entity VERIFIED."""
    doc = _small_report(tmp_path)
    next(r for r in doc["cases"] if r["case"] == _T1)["status"] = "failed"
    problems = checkreport.check_report(doc)
    assert any(f"REQ-1: owned member {_T1} is passed, but its case is failed" in p for p in problems), problems


def test_check_report_rejects_an_own_verdict_its_set_does_not_back(tmp_path):
    """Release review (low, c): VERIFIED through a moved member, or with no
    members at all; INVALID without a quarantined member."""
    doc = _small_report(tmp_path)
    moved = copy.deepcopy(doc)
    _add_member(_entity(moved, "REQ-2"), {"case": "//c:t#k::gone", "target": "//c:t", "selector": "lock",
                                          "via": "lock", "state": "moved", "owned": False})  # fmt: skip
    assert any("REQ-2 reads VERIFIED on its own set, which holds 1 moved" in p for p in checkreport.check_report(moved))
    empty = copy.deepcopy(doc)
    _entity(empty, "REQ-3")["status"] = "VERIFIED"
    empty["summary"]["requirements_verified"] += 1
    empty["summary"]["requirements_unverified"] -= 1
    assert any("REQ-3 reads VERIFIED on its own set, which has no passed member" in p
               for p in checkreport.check_report(empty))  # fmt: skip
    invalid = copy.deepcopy(doc)
    _entity(invalid, "REQ-2")["status"] = "INVALID"
    invalid["summary"]["requirements_verified"] -= 1
    invalid["summary"]["requirements_invalid"] += 1
    assert any("REQ-2 reads INVALID but holds no quarantined case" in p for p in checkreport.check_report(invalid))


def test_check_report_rejects_a_rollup_from_an_entity_that_is_not_a_child(tmp_path):
    """Release review (low, d): REQ-3 'derived from REQ-1' with no refines
    relation makes REQ-1's case the evidence of REQ-3."""
    doc = _small_report(tmp_path)
    ent = _entity(doc, "REQ-3")
    ent["basis"], ent["derived_from"] = "derived", ["REQ-1"]
    assert any("REQ-3 is derived from REQ-1, which is not one of its children" in p
               for p in checkreport.check_report(doc))  # fmt: skip
    own = _entity(doc, "REQ-2")
    own["derived_from"] = ["REQ-1"]
    assert any("REQ-2: basis own, but derived from REQ-1" in p for p in checkreport.check_report(doc))


_THERMOSTAT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "examples",
    "thermostat",
    "report.golden.json",
)


def _relabel_derived(doc, eid):
    ent = _entity(doc, eid)
    ent["basis"], ent["derived_from"] = "derived", []
    return ent


def test_an_entity_with_members_cannot_relabel_its_basis_derived_to_skip_its_own_set(tmp_path):
    """Release gate (low): basis 'derived' with owned members skipped the
    own-set check. The basis follows the set, and the set is re-checked
    whenever it has members."""
    doc = _small_report(tmp_path)
    assert checkreport.check_report(doc) == []
    honest = copy.deepcopy(doc)
    _relabel_derived(honest, "REQ-1")  # a passing set, only the label forged
    assert any("REQ-1 has 2 member(s) but claims basis 'derived'" in p for p in checkreport.check_report(honest))
    moved = copy.deepcopy(doc)
    _add_member(_entity(moved, "REQ-2"), {"case": "//c:t#k::gone", "target": "//c:t", "selector": "lock",
                                          "via": "lock", "state": "moved", "owned": False})  # fmt: skip
    _relabel_derived(moved, "REQ-2")
    problems = checkreport.check_report(moved)
    assert any("REQ-2 reads VERIFIED on its own set, which holds 1 moved" in p for p in problems), problems
    # Without members an entity may read derived, but a pass derived from nothing is forged.
    empty = copy.deepcopy(doc)
    _relabel_derived(empty, "REQ-3")
    assert checkreport.check_report(empty) == []  # UNVERIFIED, derived from nothing: consistent
    _entity(empty, "REQ-3")["status"] = "VERIFIED"
    empty["summary"]["requirements_verified"] += 1
    empty["summary"]["requirements_unverified"] -= 1
    assert any("REQ-3 reads VERIFIED derived from no entity" in p for p in checkreport.check_report(empty))
    both = copy.deepcopy(doc)
    _entity(both, "REQ-3")["basis"] = "own+derived"
    assert any("REQ-3 claims basis own+derived but has no members" in p for p in checkreport.check_report(both))


def test_the_gate_forge_relabelling_a_failed_set_derived_is_rejected():
    """The release gate's forge on the thermostat golden: REQ-1's owned
    member and its row failed, REQ-1 still VERIFIED, basis relabelled
    'derived' with derived_from []. The control (basis 'own') fails too."""
    if not os.path.exists(_THERMOSTAT):
        pytest.skip("examples/thermostat/report.golden.json not available")
    with open(_THERMOSTAT, encoding="utf-8") as fh:
        golden = json.load(fh)
    assert checkreport.check_report(golden) == []
    forged = copy.deepcopy(golden)
    ent = _entity(forged, "REQ-1")
    assert ent["status"] == "VERIFIED" and ent["basis"] == "own" and ent["members"]
    member = ent["members"][0]
    member["state"] = "failed"
    ent["set"]["passed"] -= 1
    ent["set"]["failed"] += 1
    for ev in ent["evidence"]:
        if f"{ev['target']}#{ev['name']}" == member["case"]:
            ev["status"] = "failed"
    next(r for r in forged["cases"] if r["case"] == member["case"])["status"] = "failed"
    control = copy.deepcopy(forged)
    assert any("REQ-1 reads VERIFIED on its own set, which holds 1 failed" in p
               for p in checkreport.check_report(control))  # fmt: skip
    _relabel_derived(forged, "REQ-1")
    problems = checkreport.check_report(forged)
    assert any("REQ-1 has" in p and "claims basis 'derived'" in p for p in problems), problems
    assert any("REQ-1 reads VERIFIED on its own set, which holds 1 failed" in p for p in problems), problems


def _inject_alias(doc, rnd):
    """The fuzz's second injector: an owned case copied under another spelling
    of its key, owned by a second entity, every count kept consistent."""
    owned = [r for r in doc["cases"] if r["owner"] is not None and r["path"] != "[target]"]
    if not owned:
        return None
    row = rnd.choice(owned)
    other = rnd.choice([e["id"] for e in _verifiable(doc) if e["id"] != row["owner"]])
    target, path = row["target"], row["path"]
    spelling = rnd.choice(["@", "@@", "blank", "tag"] if target.startswith("//") else ["blank", "tag"])
    if spelling in ("@", "@@"):
        target = spelling + target
    elif spelling == "blank":
        path += " "
    else:
        path += f" [rr:{other}]"
    return _forge(doc, row["case"], target, path, other), f"{target}#{path}"


def test_the_fuzz_never_lets_an_alias_spelling_own_a_case_twice():
    rnd = random.Random(SEED + 3)
    injected = 0
    for trial, doc in _fuzz_reports(1500, SEED + 4):
        got = _inject_alias(doc, rnd)
        if got is None:
            continue
        bad, key = got
        injected += 1
        problems = checkreport.check_report(bad)
        assert any(key in p and ("not canonical" in p or "name tag" in p) for p in problems), (trial, problems)
    assert injected > 100
