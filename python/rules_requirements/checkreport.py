# SPDX-License-Identifier: AGPL-3.0-or-later
"""``rr check-report``: re-prove the one-owner partition from a published report.

A test case verifies at most one requirement. :func:`rules_requirements.attribution.attribute`
guarantees it by construction, and :meth:`~rules_requirements.attribution.Attribution.check_invariant`
checks it in process. This module is the independent audit (layer L6): it
reads nothing but the JSON a report wrote (``rules_requirements/report/v2``)
and checks that

* the file names every object key once (a duplicate key could name two
  owners for one case: :func:`load_report` refuses it);
* every case is listed once, and its ``owner`` is one id or ``null`` (a scalar,
  never a list) naming a user need, requirement or mitigation of the report;
* every member says whether its entity owns it (``owned``); no case key is an
  owned member of two entities, each owned member is a row of ``cases`` whose
  owner is that entity, and every owned case is a member of its owner (the
  owned members partition the owned cases). A member that is not owned is a
  pseudo-member: an ``error`` one names no case of the report and sits on a
  tainted or synthetic-only target, and a ``missing`` or ``not-run`` one names
  no case of the report (only a ``moved`` or ``quarantined`` member may);
* the ``evidence[]`` compatibility view of each entity is exactly the view of
  its owned members, so a 0.3.x reader of ``evidence[]`` sees the same
  partition;
* no quarantined case is owned; a quarantine's ``entities`` is a list of ids,
  the entities holding the case as a ``quarantined`` member are exactly the
  verifiable ones it names, each of them reads INVALID, and the ids it names
  follow from its code (the declared ids and the claims);
* the counts agree: the summary, every entity's ``set``, the per-target
  counts and the granularity against the rows they count.

:func:`check_report` returns the problems (empty: the partition holds).
"""

from __future__ import annotations

import json
from typing import Any, Mapping

SCHEMA = "rules_requirements/report/v2"
OWNED_STATES = ("passed", "failed", "error", "skipped")
VERIFIABLE_SECTIONS = ("user_needs", "requirements", "mitigations")
OTHER_SECTIONS = ("risks", "test_methods")
_STATES = (*OWNED_STATES, "missing", "not-run", "moved", "quarantined")
_REQUIREMENT_COUNTS = {
    "requirements_verified": "VERIFIED",
    "requirements_under_verified": "UNDER-VERIFIED",
    "requirements_partial": "PARTIAL",
    "requirements_failed": "FAILED",
    "requirements_unverified": "UNVERIFIED",
    "requirements_incomplete": "INCOMPLETE",
    "requirements_invalid": "INVALID",
}


class ReportError(ValueError):
    """The file is no v2 report at all (unreadable, not JSON, another schema)."""


class AmbiguousReportError(ReportError):
    """The file names an object key twice, so readers may disagree on its
    content (one owner to a last-wins parser, another to a first-wins one):
    the partition cannot be proven from it. ``rr check-report`` exits 1."""


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise AmbiguousReportError(f"the key {key!r} appears twice in one object")
        seen[key] = value
    return seen


def load_report(path: str) -> dict[str, Any]:
    """Read a JSON report; raises :class:`ReportError` when it is no v2 report,
    and :class:`AmbiguousReportError` when an object names a key twice."""
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh, object_pairs_hook=_unique_keys)
    except AmbiguousReportError as exc:
        raise AmbiguousReportError(f"{path}: {exc}; a duplicate key can name two owners for one case") from None
    except (OSError, ValueError) as exc:
        raise ReportError(f"{path}: cannot read the report: {exc}") from None
    if not isinstance(doc, dict):
        raise ReportError(f"{path}: not a report (expected a JSON object)")
    if doc.get("schema") != SCHEMA:
        raise ReportError(
            f"{path}: schema is {doc.get('schema')!r}, expected {SCHEMA!r} (rules_requirements 0.3 or newer)"
        )
    return doc


def _list(doc: Mapping[str, Any], key: str, problems: list[str]) -> list[Any]:
    value = doc.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        problems.append(f"{key}: expected a list")
        return []
    return value


def check_report(doc: Mapping[str, Any]) -> list[str]:
    """Every way ``doc`` breaks the one-owner partition or disagrees with its own counts."""
    problems: list[str] = []
    if doc.get("schema") != SCHEMA:
        return [f"schema is {doc.get('schema')!r}, expected {SCHEMA!r}"]

    # Entities and their verdicts.
    status: dict[str, str] = {}
    verifiable: set[str] = set()
    for section in (*VERIFIABLE_SECTIONS, *OTHER_SECTIONS):
        for ent in _list(doc, section, problems):
            if not isinstance(ent, dict) or not isinstance(ent.get("id"), str):
                problems.append(f"{section}: an entry without an id")
                continue
            if ent["id"] in status:
                problems.append(f"{ent['id']} is listed twice")
            status[ent["id"]] = str(ent.get("status"))
            if section in VERIFIABLE_SECTIONS:
                verifiable.add(ent["id"])
            elif ent.get("members"):
                problems.append(f"{ent['id']}: a {section[:-1].replace('_', ' ')} holds no verification set")

    # The inverse matrix: one row, one scalar owner per case.
    owner: dict[str, str | None] = {}
    quarantine_code: dict[str, str] = {}
    rows = _list(doc, "cases", problems)
    by_target: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("case"), str):
            problems.append(f"cases: a row without a case key: {row!r}")
            continue
        key = row["case"]
        if key in owner:
            problems.append(f"{key} is listed twice in cases")
        if row.get("target") is not None and row.get("path") is not None and key != f"{row['target']}#{row['path']}":
            problems.append(f"{key}: its key is not <target>#<path> ({row['target']!r}, {row['path']!r})")
        value = row.get("owner")
        if value is not None and not isinstance(value, str):
            problems.append(f"{key}: owner {value!r} is not a scalar id; a test case verifies at most one requirement")
            value = None
        elif isinstance(value, str) and value not in verifiable:
            problems.append(f"{key}: owner {value!r} is no user need, requirement or mitigation of this report")
        if value is not None and row.get("via") not in ("model", "tag"):
            problems.append(f"{key}: owned by {value} via {row.get('via')!r}")
        if value is None and row.get("via") is not None:
            problems.append(f"{key}: owned by nobody, but via {row.get('via')!r}")
        owner[key] = value
        if isinstance(row.get("quarantine"), str):
            quarantine_code[key] = row["quarantine"]
        by_target.setdefault(str(row.get("target")), []).append(row)

    att = doc.get("attribution")
    if not isinstance(att, dict):
        problems.append("attribution: missing")
        att = {}
    raw_targets = att.get("targets")
    targets: dict[str, Any] = raw_targets if isinstance(raw_targets, dict) else {}

    # Members: the owned members partition the owned cases. Ownership is the
    # member's ``owned`` flag (the report writes Member.owned), never guessed
    # from the state: an ``error`` pseudo-member (a selector or lock entry
    # whose case is absent from a tainted target) is in an owned state too.
    member_of: dict[str, list[str]] = {}
    quarantined_members: set[tuple[str, str]] = set()
    owned_view: dict[str, list[tuple[str, str, str, bool]]] = {}
    for section in VERIFIABLE_SECTIONS:
        for ent in _list(doc, section, problems):
            if not isinstance(ent, dict) or not isinstance(ent.get("id"), str):
                continue
            eid = ent["id"]
            members = ent.get("members")
            if not isinstance(members, list):
                problems.append(f"{eid}: members missing or not a list")
                continue
            counted = {state: 0 for state in _STATES}
            view = owned_view.setdefault(eid, [])
            for mb in members:
                if not isinstance(mb, dict):
                    problems.append(f"{eid}: a member that is not an object")
                    continue
                state, case, is_owned = mb.get("state"), mb.get("case"), mb.get("owned")
                if state not in counted:
                    problems.append(f"{eid}: member {case!r} has an unknown state {state!r}")
                    continue
                counted[state] += 1
                if case is not None and not isinstance(case, str):
                    problems.append(f"{eid}: member case {case!r} is not one case key")
                    continue
                if not isinstance(is_owned, bool):
                    problems.append(f"{eid}: member {case!r} does not say whether it is owned ({is_owned!r})")
                    continue
                if is_owned:
                    if state not in OWNED_STATES or case is None:
                        problems.append(f"{eid}: an owned member must be a case in an owned state ({case!r}, {state})")
                        continue
                    # Every owned key counts, whether or not cases[] lists it.
                    member_of.setdefault(case, []).append(eid)
                    view.append((case, state, str(mb.get("level") or ""), mb.get("stale") is True))
                    if case not in owner:
                        problems.append(f"{case} is an owned ({state}) member of {eid} but no case of this report")
                    elif owner[case] != eid:
                        problems.append(f"{case} is an owned ({state}) member of {eid} but owned by {owner[case]}")
                elif state in OWNED_STATES:
                    # A pseudo-member in an owned state: only an error for a
                    # case the evidence lacks, on a target that ran tainted or
                    # with a failed whole-target result only.
                    trow = targets.get(str(mb.get("target")))
                    if state != "error":
                        problems.append(f"{eid}: member {case!r} is {state} but not owned")
                    elif case is not None and case in owner:
                        problems.append(f"{eid}: {case} is a case of this report, so an error member of it is owned")
                    elif not isinstance(trow, dict) or not (trow.get("tainted") or trow.get("synthetic")):
                        problems.append(
                            f"{eid}: error member {case!r} of {mb.get('target')!r} is not owned, but that target "
                            "is neither tainted nor synthetic-only"
                        )
                elif state in ("missing", "not-run"):
                    # An expected member whose case the evidence lacks: never
                    # a case of this report (only a moved or quarantined
                    # member may name one), or one case would sit in two sets.
                    if case is not None and case in owner:
                        held = f"owned by {owner[case]}" if owner[case] else "owned by nobody"
                        problems.append(
                            f"{eid}: member {case} is {state}, but it is a case of this report ({held}); only a "
                            "moved or quarantined member may name a case the evidence holds"
                        )
                elif state == "quarantined":
                    if case is None:
                        problems.append(f"{eid}: a quarantined member without a case")
                    else:
                        quarantined_members.add((eid, case))
            if counted["quarantined"] and status.get(eid) != "INVALID":
                problems.append(f"{eid} holds a quarantined case but reads {status.get(eid)}, not INVALID")
            summary = ent.get("set")
            if isinstance(summary, dict):
                if summary.get("members") != len(members):
                    problems.append(f"{eid}: set.members is {summary.get('members')}, it lists {len(members)}")
                for state, n in counted.items():
                    field = state.replace("-", "_")
                    if summary.get(field) != n:
                        problems.append(f"{eid}: set.{field} is {summary.get(field)}, its members say {n}")
            else:
                problems.append(f"{eid}: no set summary")
    # The 0.3.x evidence[] view: exactly the owned members, nothing else.
    for section in (*VERIFIABLE_SECTIONS, *OTHER_SECTIONS):
        for ent in _list(doc, section, problems):
            if not isinstance(ent, dict) or not isinstance(ent.get("id"), str):
                continue
            eid = ent["id"]
            evidence = ent.get("evidence") or []
            if not isinstance(evidence, list):
                problems.append(f"{eid}: evidence is not a list")
                continue
            listed = []
            for ev in evidence:
                if not isinstance(ev, dict) or not isinstance(ev.get("name"), str):
                    problems.append(f"{eid}: an evidence entry without a name: {ev!r}")
                    continue
                listed.append(
                    (
                        f"{ev.get('target')}#{ev['name']}",
                        str(ev.get("status")),
                        str(ev.get("level") or ""),
                        ev.get("stale") is True,
                    )
                )
            derived = owned_view.get(eid, [])
            if sorted(listed) != sorted(derived):
                extra = sorted({g[0] for g in listed} - {w[0] for w in derived})
                problems.append(
                    f"{eid}: evidence[] is not the view of its owned members"
                    + (f" (it lists {', '.join(extra)}, which {eid} does not own)" if extra else "")
                )
    for case, ents in sorted(member_of.items()):
        if len(ents) > 1:
            problems.append(
                f"{case} appears under {len(ents)} entities ({', '.join(ents)}); a test case verifies at most one"
            )
    for case, who in sorted(owner.items()):
        if who is not None and who not in member_of.get(case, ()):
            problems.append(f"{case} is owned by {who} but is no member of it")

    # Quarantines: owned by nobody; the entities holding the case as a
    # quarantined member are exactly the verifiable ones named, each INVALID.
    quarantined = _list(att, "quarantined", problems)
    rows_by_key = {r["case"]: r for r in rows if isinstance(r, dict) and isinstance(r.get("case"), str)}
    holders: dict[str, set[str]] = {}
    for eid, key in quarantined_members:
        holders.setdefault(key, set()).add(eid)
    seen_q: set[str] = set()
    for q in quarantined:
        if not isinstance(q, dict) or not isinstance(q.get("case"), str):
            problems.append(f"attribution.quarantined: an entry without a case: {q!r}")
            continue
        key = q["case"]
        code = q.get("code")
        if key in seen_q:
            problems.append(f"{key} is quarantined twice")
        seen_q.add(key)
        if key not in owner:
            problems.append(f"{key} is quarantined but no case of this report")
        elif owner[key] is not None:
            problems.append(f"{key} is quarantined ({code}) and owned by {owner[key]}")
        if key in member_of:
            problems.append(f"{key} is quarantined and an owned member of {', '.join(member_of[key])}")
        if quarantine_code.get(key) != code:
            problems.append(f"{key}: cases says quarantine {quarantine_code.get(key)!r}, attribution {code!r}")
        named = q.get("entities")
        if not isinstance(named, list) or not all(isinstance(e, str) for e in named):
            problems.append(f"{key} ({code}): entities {named!r} is not a list of ids")
            continue
        for eid in named:
            if eid not in status:
                continue  # an id the model does not define: nothing to read INVALID
            if status[eid] != "INVALID":
                problems.append(f"{key} ({code}) names {eid}, which reads {status[eid]}, not INVALID")
        named_here = {e for e in named if e in verifiable}
        have = holders.get(key, set())
        for eid in sorted(named_here - have):
            problems.append(f"{key} ({code}) names {eid}, which holds no quarantined member for it")
        for eid in sorted(have - named_here):
            problems.append(f"{eid} holds {key} as quarantined, but its quarantine ({code}) does not name {eid}")
        # Who a quarantine names follows from its code.
        claimed = {str(c.get("entity")) for c in q.get("claims") or [] if isinstance(c, dict)}
        declared = q.get("declared")
        if not isinstance(declared, list):
            problems.append(f"{key} ({code}): declared {declared!r} is not a list")
            declared = []
        row = rows_by_key.get(key)
        if row is not None and sorted(map(str, row.get("declared") or [])) != sorted(map(str, declared)):
            problems.append(f"{key}: cases says declared {row.get('declared')!r}, its quarantine {declared!r}")
        if code == "multi-tag":
            follows = {str(d) for d in declared if d in verifiable} | claimed
            if len(declared) < 2:
                problems.append(f"{key} (multi-tag) declares {declared!r}, fewer than two ids")
        elif code == "attribution-conflict":
            follows = claimed
            if len(claimed) < 2:
                problems.append(f"{key} (attribution-conflict) has claims of {len(claimed)} entities, not two or more")
        elif code == "same-code-multiple-owners":
            follows = set(named) if len(named) == 1 else set()
            if len(named) != 1:
                problems.append(f"{key} (same-code-multiple-owners) names {named!r}, not the one owner it had")
        else:
            problems.append(f"{key}: unknown quarantine code {code!r}")
            continue
        if follows != set(named):
            problems.append(f"{key} ({code}) names {sorted(named)}, its declared ids and claims give {sorted(follows)}")
    for key in sorted(set(quarantine_code) - seen_q):
        problems.append(f"{key}: cases marks it quarantined, attribution.quarantined does not list it")
    for eid, key in sorted(quarantined_members):
        if key not in seen_q:
            problems.append(f"{eid} holds {key} as quarantined, but no quarantine lists it")

    # Counts.
    raw_summary = doc.get("summary")
    s: dict[str, Any] = raw_summary if isinstance(raw_summary, dict) else {}
    owned_n = sum(v is not None for v in owner.values())
    expected = {
        "test_cases": len(owner),
        "test_cases_owned": owned_n,
        "test_cases_quarantined": len(seen_q),
        "test_cases_unowned": len(owner) - owned_n - len(seen_q),
        "requirements": len(_list(doc, "requirements", [])),
        "gaps": len(_list(doc, "gaps", [])),
    }
    for field, wanted in _REQUIREMENT_COUNTS.items():
        expected[field] = sum(isinstance(r, dict) and r.get("status") == wanted for r in _list(doc, "requirements", []))
    for field, n in expected.items():
        if s.get(field) != n:
            problems.append(f"summary.{field} is {s.get(field)}, the report lists {n}")
    for target, trow in targets.items():
        mine = by_target.get(target, [])
        if not isinstance(trow, dict):
            problems.append(f"attribution.targets[{target}]: not an object")
            continue
        got = {
            "cases": len(mine),
            "owned": sum(r.get("owner") is not None for r in mine),
            "quarantined": sum(r["case"] in seen_q for r in mine),
        }
        for field, n in got.items():
            if trow.get(field) != n:
                problems.append(f"attribution.targets[{target}].{field} is {trow.get(field)}, cases list {n}")
        owners = sorted({str(r["owner"]) for r in mine if r.get("owner") is not None})
        if sorted(trow.get("owners") or []) != owners:
            problems.append(f"attribution.targets[{target}].owners is {trow.get('owners')}, cases say {owners}")
    for target in sorted(set(by_target) - set(targets)):
        problems.append(f"{target} has cases but no attribution.targets entry")
    gran = att.get("granularity")
    if isinstance(gran, dict):
        kinds = ("owned_by_literal", "owned_by_pattern", "owned_by_whole", "owned_by_tag")
        by = sum(int(gran.get(k) or 0) for k in kinds)
        if by != owned_n:
            problems.append(f"attribution.granularity counts {by} owned case(s), cases list {owned_n}")
    return problems


def summarize(doc: Mapping[str, Any]) -> str:
    """``N case(s): O owned by E entities, Q quarantined, U unowned``."""
    rows = [r for r in doc.get("cases") or [] if isinstance(r, dict)]
    owners = {r.get("owner") for r in rows if isinstance(r.get("owner"), str)}
    owned = sum(isinstance(r.get("owner"), str) for r in rows)
    quarantined = sum("quarantine" in r for r in rows)
    return (
        f"{len(rows)} case(s): {owned} owned by {len(owners)} entit{'y' if len(owners) == 1 else 'ies'}, "
        f"{quarantined} quarantined, {len(rows) - owned - quarantined} unowned"
    )
