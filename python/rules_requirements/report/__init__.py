# SPDX-License-Identifier: AGPL-3.0-or-later
"""Render a :class:`~rules_requirements.trace.Matrix` as JSON, Markdown or HTML.

The JSON document (``rules_requirements/report/v2``) is the canonical form:
fully deterministic (sorted, no timestamps, no machine-specific paths), so it
can be checked into a repository as a golden file and diffed in review.
Markdown and HTML are views of it.

Every case row, member and count is read from the matrix's
:class:`~rules_requirements.attribution.Attribution`; nothing here derives
ownership from tags or targets. ``cases`` is the inverse matrix (each case
key and its one owner, or none): the input of ``rr check-report``
(:mod:`rules_requirements.checkreport`), which re-proves from the JSON alone
that no case is owned twice.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Iterable

from rules_requirements import case_selectors
from rules_requirements import config as cfg
from rules_requirements.attribution import (
    MEMBER_STATES,
    QUARANTINED,
    TAG_SELECTOR,
    WHOLE_SELECTOR,
    Attribution,
    Member,
)
from rules_requirements.trace import INCOMPLETE, UNVERIFIED, Gap, Matrix
from rules_requirements.util import natural_key

SCHEMA = "rules_requirements/report/v2"

OUT_OF_LANE = "out of lane"
"""``lane_hint`` of a not-run member whose target this lane does not run
(``--lane-targets``): it is expected in another lane's evidence."""

# Lock findings, listed in the "Lock drift" section.
LOCK_DRIFT = ("lock-owner-changed", "lock-stale", "unlocked-member", "lock-invalid")


@dataclass(frozen=True)
class Lane:
    """The lane a report was built for (``rr report --lane/--lane-targets``).

    It never changes a verdict: it stamps the report with ``name`` and, when
    ``targets`` is given, labels the not-run members of every other target
    "out of lane (expected elsewhere)" and keeps the gaps those members alone
    cause out of the work queue."""

    name: str = ""
    targets: frozenset[str] | None = None  # normalized labels this lane runs; None: unknown

    def out_of_lane(self, member: Member) -> bool:
        return self.targets is not None and member.state == "not-run" and member.target not in self.targets


NO_LANE = Lane()


def _ids(items: Any) -> list[str]:
    return sorted((getattr(i, "id", i) for i in items), key=natural_key)


def _shown(path: str) -> str:
    """A path as reports show it: relative to the working directory when below it."""
    if not path or not os.path.isabs(path):
        return path
    here = os.getcwd()
    return os.path.relpath(path, here) if path.startswith(here + os.sep) else path


def member_dict(member: Member, lane: Lane = NO_LANE) -> dict[str, Any]:
    """One member of a verification set as the report writes it."""
    out = member.to_dict()
    if lane.out_of_lane(member):
        out["lane_hint"] = OUT_OF_LANE
    return out


def set_summary(members: Iterable[Member]) -> dict[str, Any]:
    """``{complete, members, passed, failed, error, skipped, missing, not_run, moved, quarantined}``."""
    ms = list(members)
    out: dict[str, Any] = {
        "complete": bool(ms) and all(m.owned and m.state != "skipped" for m in ms),
        "members": len(ms),
    }
    for state in MEMBER_STATES:
        out[state.replace("-", "_")] = sum(m.state == state for m in ms)
    return out


def set_line(members: Iterable[Member], lane: Lane = NO_LANE) -> str:
    """``set 17/23 passed · 6 not run (out of lane)``: one entity's set in a line."""
    ms = list(members)
    if not ms:
        return ""
    parts = [f"set {sum(m.state == 'passed' for m in ms)}/{len(ms)} passed"]
    for state in ("failed", "error", "skipped", "missing", "not-run", "moved", "quarantined"):
        those = [m for m in ms if m.state == state]
        if not those:
            continue
        text = f"{len(those)} {state.replace('-', ' ')}"
        if state == "not-run" and lane.targets is not None:
            away = sum(lane.out_of_lane(m) for m in those)
            if away == len(those):
                text += f" ({OUT_OF_LANE})"
            elif away:
                text += f" ({away} {OUT_OF_LANE})"
        parts.append(text)
    return " · ".join(parts)


def _member_kind(member: Member) -> str:
    """How an owned member was selected: literal | pattern | whole | tag."""
    if member.via == "tag" or member.selector == TAG_SELECTOR:
        return "tag"
    if member.selector == WHOLE_SELECTOR:
        return "whole"
    try:
        return "literal" if case_selectors.is_literal(member.selector) else "pattern"
    except case_selectors.BadSelector:
        return "pattern"


def attribution_dict(att: Attribution, lane: Lane = NO_LANE, config: cfg.Config | None = None) -> dict[str, Any]:
    """The ``attribution`` block: mode, ``main_repo`` and ``variants`` (what
    ``rr check-report`` needs to re-prove key spellings and same-code
    ownership), lock, lane, per-target counts, quarantines (with every
    claim's origin), issues and granularity."""
    config = config or cfg.Config()
    targets: dict[str, dict[str, Any]] = {}
    for target, run in att.targets.items():
        keys = [k for k in att.cases if k.target == target]
        owners = sorted({att.owner[k] for k in keys if k in att.owner}, key=natural_key)
        row: dict[str, Any] = {
            "cases": len(keys),
            "owned": sum(k in att.owner for k in keys),
            "quarantined": sum(att.quarantine_of(k) is not None for k in keys),
            "owners": owners,
            "synthetic": run.synthetic_only,
            "ran": run.ran,
        }
        if run.tainted:
            row["tainted"] = run.taint_message
        if lane.targets is not None:
            row["in_lane"] = target in lane.targets
        targets[target] = row
    granularity = {"owned_by_literal": 0, "owned_by_pattern": 0, "owned_by_whole": 0, "owned_by_tag": 0}
    for members in att.members.values():
        for m in members:
            if m.owned and m.key is not None and att.owner.get(m.key) == m.entity:
                granularity["owned_by_" + _member_kind(m)] += 1
    granularity["coarse_claims"] = sum(i.code == "coarse-claim" for i in att.issues)
    return {
        "mode": att.mode,
        "main_repo": config.main_repo,
        "variants": [list(group) for group in config.variant_groups()],
        "lock": _shown(att.lock.path) if att.lock is not None else None,
        "lane": lane.name or None,
        "targets": targets,
        "quarantined": [q.to_dict() for q in att.quarantined],
        "issues": [i.to_dict() for i in att.issues],
        "granularity": granularity,
    }


def case_rows(att: Attribution) -> list[dict[str, Any]]:
    """The inverse matrix: every case key with its one owner (or ``null``)."""
    level: dict[Any, str] = {}
    for members in att.members.values():
        for m in members:
            if m.owned and m.key is not None:
                level[m.key] = m.level
    rows = []
    for key in sorted(att.cases, key=lambda k: (natural_key(k.target), natural_key(k.path))):
        res = att.cases[key]
        row: dict[str, Any] = {
            "case": str(key),
            "target": key.target,
            "path": key.path,
            "owner": att.owner.get(key),
            "via": att.via.get(key),
            "status": res.status,
        }
        if level.get(key) or res.level:
            row["level"] = level.get(key) or res.level
        row["declared"] = list(res.declared)
        q = att.quarantine_of(key)
        if q is not None:
            row["quarantine"] = q.code
        if res.file:
            row["file"] = res.file
        for flag in ("synthetic", "flaky", "duplicate"):
            if getattr(res, flag):
                row[flag] = True
        rows.append(row)
    return rows


def out_of_lane_gaps(matrix: Matrix, lane: Lane) -> list[Gap]:
    """The gaps caused only by members this lane does not run: an
    ``unverified`` or ``incomplete`` gap whose open members are all not-run
    members of out-of-lane targets. ``rr report --queue-out`` leaves them
    out; the report still lists them (with ``lane_hint``)."""
    if lane.targets is None:
        return []
    out = []
    for gap in matrix.gaps:
        if gap.kind not in ("unverified", "incomplete"):
            continue
        verdict = matrix.verdicts.get(gap.entity)
        if verdict is None or verdict.status not in (UNVERIFIED, INCOMPLETE):
            continue
        members = verdict.members
        open_ = [m for m in members if m.state != "passed"]
        if open_ and all(lane.out_of_lane(m) for m in open_):
            out.append(gap)
    return out


def to_dict(matrix: Matrix, title: str = "", *, lane: Lane = NO_LANE) -> dict[str, Any]:
    m = matrix.model
    v = matrix.verdicts
    att = matrix.attribution

    def base(ent: Any) -> dict[str, Any]:
        out: dict[str, Any] = {"id": ent.id, "title": ent.title, "status": v[ent.id].status}
        if ent.description:
            out["description"] = ent.description
        # Authoring metadata: lifecycle status, owner, category, rationale, tags.
        for key, name in (
            ("status", "lifecycle"),
            ("owner", "owner"),
            ("category", "category"),
            ("rationale", "rationale"),
        ):
            value = getattr(ent, key, "")
            if value:
                out[name] = value
        if ent.tags:
            out["tags"] = list(ent.tags)
        open_notes = [n for n in ent.notes if n.status == "open"]
        if open_notes:
            out["open_notes"] = [{"kind": n.kind, "text": n.text} for n in open_notes]
        verdict = v[ent.id]
        out["basis"] = verdict.basis or "own"
        if verdict.derived_from:
            out["derived_from"] = list(verdict.derived_from)
        if ent.kind in (cfg.USER_NEED, cfg.REQUIREMENT, cfg.MITIGATION):
            out["set"] = set_summary(verdict.members)
            out["members"] = [member_dict(mb, lane) for mb in verdict.members]
        ev = verdict.evidence
        if ev:
            # The 0.2 view of the owned members, kept for 0.3.x readers.
            out["evidence"] = [e.to_dict() for e in ev]
        return out

    def refs(ent_id: str) -> dict[str, Any]:
        verdict = v[ent_id]
        out: dict[str, Any] = {}
        if matrix.annotations_scanned:
            out["implemented_in"] = [r.to_dict() for r in verdict.implemented_in]
            out["verified_in"] = [r.to_dict() for r in verdict.verified_in]
        return out

    needs = []
    for un in sorted(m.user_needs.values(), key=lambda e: natural_key(e.id)):
        needs.append({**base(un), "requirements": _ids(m.requirements_for_need(un.id))})

    reqs = []
    for req in sorted(m.requirements.values(), key=lambda e: natural_key(e.id)):
        verdict = v[req.id]
        item = base(req)
        item.update(
            {
                "demanded_level": verdict.demanded,
                "provided_level": verdict.provided or None,
                "satisfies": list(req.satisfies),
                "refines": list(req.refines),
                "implements": _ids(m.mitigations_implemented_by(req.id)),
            }
        )
        if req.method:
            item["method"] = req.method
        if req.modules:
            item["modules"] = list(req.modules)
        if verdict.stale:
            item["stale"] = True
        if verdict.pyramid_violation:
            item["pyramid_violation"] = True
        item.update(refs(req.id))
        reqs.append(item)

    mits = []
    for mit in sorted(m.mitigations.values(), key=lambda e: natural_key(e.id)):
        item = base(mit)
        item.update(
            {"type": mit.type or None, "mitigates": list(mit.mitigates), "implemented_by": list(mit.implemented_by)}
        )
        item.update(refs(mit.id))
        mits.append(item)

    risks = []
    for risk in sorted(m.risks.values(), key=lambda e: natural_key(e.id)):
        item = base(risk)
        for key in ("hazard", "hazardous_situation", "harm", "residual"):
            if getattr(risk, key):
                item[key] = getattr(risk, key)
        item.update(
            {
                "severity": risk.severity or None,
                "likelihood": risk.likelihood or None,
                "residual_severity": risk.residual_severity or None,
                "residual_likelihood": risk.residual_likelihood or None,
                "mitigations": _ids(m.mitigations_for_risk(risk.id)),
            }
        )
        score = m.config.risk_score(risk.severity, risk.likelihood)
        if score is not None:
            item["score"] = score
        rscore = m.config.risk_score(
            risk.residual_severity or risk.severity, risk.residual_likelihood or risk.likelihood
        )
        if rscore is not None and (risk.residual_severity or risk.residual_likelihood):
            item["residual_score"] = rscore
        risks.append(item)

    tms = []
    for tm in sorted(m.test_methods.values(), key=lambda e: natural_key(e.id)):
        item = base(tm)
        item.update({"level": tm.level, "used_by": _ids(m.requirements_for_method(tm.id))})
        tms.append(item)

    summary = matrix.counts()
    if att is not None:
        owned = len(att.owner)
        quarantined = len(att.quarantined)
        extra = {
            "test_cases_owned": owned,
            "test_cases_unowned": len(att.cases) - owned - quarantined,
            "test_cases_quarantined": quarantined,
        }
        keys = list(summary)
        at = keys.index("test_cases") + 1 if "test_cases" in keys else len(keys)
        summary = dict([*list(summary.items())[:at], *extra.items(), *list(summary.items())[at:]])
    away = {id(g) for g in out_of_lane_gaps(matrix, lane)}
    gaps = []
    for g in matrix.gaps:
        gap: dict[str, Any] = dict(g.to_dict())
        if id(g) in away:
            gap["lane_hint"] = OUT_OF_LANE
        gaps.append(gap)
    return {
        "schema": SCHEMA,
        "title": title or str(m.project.get("name", "Requirements traceability")),
        "project": dict(sorted(m.project.items())),
        "summary": summary,
        "attribution": attribution_dict(att, lane, m.config) if att is not None else None,
        "cases": case_rows(att) if att is not None else [],
        "levels": [{"name": lvl.name, "rank": lvl.rank} for lvl in m.config.levels],
        "user_needs": needs,
        "requirements": reqs,
        "mitigations": mits,
        "risks": risks,
        "test_methods": tms,
        "modules": matrix.module_status(),
        "high_open_risks": matrix.high_open_risks(),
        "pyramid_violations": matrix.pyramid_violations(),
        "unknown_evidence": matrix.unknown_evidence,
        "gaps": gaps,
    }


def render_json(matrix: Matrix, title: str = "", *, lane: Lane = NO_LANE) -> str:
    return json.dumps(to_dict(matrix, title, lane=lane), indent=2, sort_keys=False) + "\n"


# --------------------------------------------------------------------------- #
# Markdown                                                                    #
# --------------------------------------------------------------------------- #

_ICON = {
    "VERIFIED": "✅",
    "VALIDATED": "✅",
    "MITIGATED": "✅",
    "UNDER-VERIFIED": "🟠",
    "PARTIAL": "🟡",
    "INCOMPLETE": "🟡",
    "FAILED": "❌",
    "INVALID": "❌",
    "UNVERIFIED": "⚪",
    "UNVALIDATED": "⚪",
    "OPEN": "⚪",
}


def _md(text: Any) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _badge(status: str) -> str:
    return f"{_ICON.get(status, '')} {status}".strip()


def _claim_origins(q: dict[str, Any]) -> str:
    """``PR-13 (requirements.yaml:233) and PR-29 (requirements.yaml:411)``, else the declared ids."""
    parts = []
    for c in q.get("claims", []):
        where = f" ({c['location']})" if c.get("location") else ""
        parts.append(f"{c['entity']} `{c['selector']}`{where}")
    if q.get("declared"):
        parts.append("declared " + ", ".join(q["declared"]))
    return "; ".join(parts) or ", ".join(q.get("entities", []))


def _status_note(item: dict[str, Any]) -> str:
    """`` · derived from REQ-2, REQ-3`` for a verdict that is not (only) its own set's."""
    if item.get("basis") in ("derived", "own+derived") and item.get("derived_from"):
        return f" · {item['basis'].replace('+', ' + ')} from {', '.join(item['derived_from'])}"
    return ""


def unowned_cases(d: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Per target, the cases no entity owns and none is quarantined (the granularity backlog)."""
    out: dict[str, list[dict[str, Any]]] = {}
    for row in d.get("cases", []):
        if row.get("owner") is None and "quarantine" not in row:
            out.setdefault(row["target"], []).append(row)
    return out


def render_markdown(matrix: Matrix, title: str = "", *, lane: Lane = NO_LANE) -> str:
    d = to_dict(matrix, title, lane=lane)
    s = d["summary"]
    att = d.get("attribution") or {}
    out = [f"# {_md(d['title'])}", ""]
    out.append(
        f"**{s['user_needs_validated']}/{s['user_needs']}** user needs validated · "
        f"**{s['requirements_verified']}/{s['requirements']}** requirements verified "
        f"({s['requirements_under_verified']} under-verified, {s['requirements_failed']} failed, "
        f"{s['requirements_invalid']} invalid, {s['requirements_incomplete']} incomplete, "
        f"{s['requirements_unverified']} unverified) · "
        f"**{s['risks_mitigated']}/{s['risks']}** risks mitigated · "
        f"{s['test_cases']} test cases ({s.get('test_cases_owned', 0)} owned, "
        f"{s.get('test_cases_unowned', 0)} unowned, {s.get('test_cases_quarantined', 0)} quarantined) · "
        f"{s['gaps']} gaps"
    )
    out.append("")
    if att.get("mode") or att.get("lane"):
        bits = [f"attribution: {att['mode']}"] if att.get("mode") else []
        bits.append(f"lock: {att['lock']}" if att.get("lock") else "lock: none (sets not pinned)")
        if att.get("lane"):
            bits.append(f"lane: {att['lane']}")
        out += ["_" + " · ".join(_md(b) for b in bits) + "_", ""]
    if att.get("quarantined"):
        out.append(
            f"> ⛔ **ATTRIBUTION ERROR: {len(att['quarantined'])} quarantined test case(s) count for no "
            "requirement** (a test case verifies at most one requirement); every entity they name is INVALID:"
        )
        out.append(">")
        for q in att["quarantined"]:
            out.append(f"> - `{_md(q['case'])}` — **{q['code']}**: {_md(_claim_origins(q))}")
        out.append("")
    if d["high_open_risks"]:
        out.append("> **High-severity risks not mitigated:** " + ", ".join(d["high_open_risks"]))
        out.append("")

    def table(header: list[str], rows: list[list[str]]) -> None:
        out.append("| " + " | ".join(header) + " |")
        out.append("|" + "|".join("---" for _ in header) + "|")
        for row in rows:
            out.append("| " + " | ".join(_md(c) for c in row) + " |")
        out.append("")

    def verdict_members(item: dict[str, Any]) -> tuple[Member, ...]:
        verdict = matrix.verdicts.get(item["id"])
        return verdict.members if verdict is not None else ()

    out += ["## User needs — validation", ""]
    table(
        ["ID", "Need", "Requirements", "Status"],
        [
            [
                n["id"],
                n["title"],
                ", ".join(n["requirements"]),
                _badge(n["status"]) + (f" · {set_line(verdict_members(n), lane)}" if n.get("members") else ""),
            ]
            for n in d["user_needs"]
        ],
    )
    out += ["## Requirements — verification", ""]

    def evidence(item: dict[str, Any]) -> str:
        line = set_line(verdict_members(item), lane)
        parts = [line] if line else []
        for e in item.get("evidence", []):
            mark = {"passed": "✓", "failed": "✗", "error": "✗", "skipped": "–"}.get(e["status"], "?")
            stale = " (stale)" if e.get("stale") else ""
            parts.append(f"{mark} {e['name']} [{e['level']}]{stale}")
        # A quarantined case is why an entity is INVALID: never leave it out
        # of the row (it counts for nobody, so it is no evidence above).
        for member in verdict_members(item):
            if member.state == QUARANTINED and member.key is not None:
                parts.append(f"⛔ {member.key.path} ({member.reason})")
        return "<br>".join(parts) or "—"

    table(
        ["ID", "Requirement", "Traces", "Demands", "Evidence", "Status"],
        [
            [
                r["id"],
                r["title"],
                "; ".join(
                    x
                    for x in (
                        ("satisfies " + ", ".join(r["satisfies"])) if r["satisfies"] else "",
                        ("refines " + ", ".join(r["refines"])) if r["refines"] else "",
                        ("implements " + ", ".join(r["implements"])) if r["implements"] else "",
                    )
                    if x
                ),
                r["demanded_level"],
                evidence(r),
                _badge(r["status"]) + _status_note(r),
            ]
            for r in d["requirements"]
        ],
    )
    if d["risks"]:
        out += ["## Risks — control", ""]
        table(
            ["ID", "Risk", "Severity × likelihood", "Mitigations", "Status"],
            [
                [
                    r["id"],
                    r["title"],
                    f"{r['severity'] or '?'} × {r['likelihood'] or '?'}"
                    + (
                        f" → {r['residual_severity'] or r['severity'] or '?'} × "
                        f"{r['residual_likelihood'] or r['likelihood'] or '?'}"
                        if r["residual_severity"] or r["residual_likelihood"]
                        else ""
                    ),
                    ", ".join(r["mitigations"]),
                    _badge(r["status"]),
                ]
                for r in d["risks"]
            ],
        )
    if d["mitigations"]:
        out += ["## Mitigations — risk control measures", ""]
        table(
            ["ID", "Mitigation", "Type", "Mitigates", "Implemented by", "Status"],
            [
                [
                    x["id"],
                    x["title"],
                    x["type"] or "",
                    ", ".join(x["mitigates"]),
                    ", ".join(x["implemented_by"]),
                    _badge(x["status"]) + (f" · {set_line(verdict_members(x), lane)}" if x.get("members") else ""),
                ]
                for x in d["mitigations"]
            ],
        )
    if d["test_methods"]:
        out += ["## Test methods", ""]
        table(
            ["ID", "Method", "Level", "Used by"],
            [[t["id"], t["title"], t["level"], ", ".join(t["used_by"])] for t in d["test_methods"]],
        )
    if matrix.annotations_scanned:

        def where(refs: list[dict[str, Any]]) -> str:
            return "<br>".join(
                f"{r['path']}:{r['line']}" + (f" ({r['symbol']})" if r.get("symbol") else "") for r in refs
            )

        out += ["## Implementation — source annotations", ""]
        table(
            ["ID", "Implemented in", "Verified in"],
            [
                [x["id"], where(x["implemented_in"]) or "—", where(x["verified_in"]) or "—"]
                for x in d["requirements"] + d["mitigations"]
            ],
        )
    if d["modules"]:
        out += ["## Modules", ""]
        table(["Module", "Status"], [[k, _badge(v)] for k, v in d["modules"].items()])
    with_sets = [x for x in d["user_needs"] + d["requirements"] + d["mitigations"] if x.get("members")]
    if with_sets:
        out += [
            "## Verification sets",
            "",
            "Each entity's set: the cases it owns, the cases it expects (literal selectors, the lock) and every "
            "quarantined case that names it. It is verified only when the whole set passed together.",
            "",
        ]
        for x in with_sets:
            out += [f"### {_md(x['id'])} — {_badge(x['status'])}", "", set_line(verdict_members(x), lane), ""]
            table(
                ["Case", "State", "Level", "Via", "Selector", "Note"],
                [
                    [
                        mb["case"] or mb["target"],
                        mb["state"],
                        mb.get("level", ""),
                        mb["via"],
                        mb["selector"],
                        "; ".join(n for n in (mb.get("lane_hint", ""), mb.get("reason", "")) if n),
                    ]
                    for mb in x["members"]
                ],
            )
    if att.get("targets"):
        out += ["## Case attribution", ""]
        table(
            ["Target", "Cases", "Owned", "Quarantined", "Unowned", "Owners"],
            [
                [
                    t,
                    str(row["cases"]),
                    str(row["owned"]),
                    str(row["quarantined"]),
                    str(row["cases"] - row["owned"] - row["quarantined"]),
                    ", ".join(row["owners"]) or ("not run" if not row["ran"] else "—"),
                ]
                for t, row in att["targets"].items()
            ],
        )
        unowned = unowned_cases(d)
        if unowned:
            out += ["Unowned cases (the granularity backlog: claim each for one entity, or leave it unowned):", ""]
            for target, rows in unowned.items():
                out.append(f"- `{_md(target)}`: " + ", ".join(f"`{_md(r['path'])}` ({r['status']})" for r in rows))
            out.append("")
    drift = [i for i in att.get("issues", []) if i["code"] in LOCK_DRIFT]
    moved = [
        (x["id"], mb)
        for x in d["user_needs"] + d["requirements"] + d["mitigations"]
        for mb in x.get("members", [])
        if mb["state"] == "moved"
    ]
    if drift or moved:
        out += ["## Lock drift", ""]
        table(
            ["Kind", "Case", "Detail"],
            [[i["code"], i.get("case", ""), i["message"]] for i in drift]
            + [["moved", mb["case"] or "", f"{ent}: {mb.get('reason', '')}"] for ent, mb in moved],
        )
    coarse = [i for i in att.get("issues", []) if i["code"] == "coarse-claim"]
    if coarse:
        out += ["## Coarse claims", ""]
        table(
            ["Entity", "Target", "Detail"],
            [[", ".join(i.get("entities", [])), i.get("target", ""), i["message"]] for i in coarse],
        )
    if d["gaps"]:
        out += ["## Gaps", ""]
        table(
            ["Kind", "Entity", "Route", "Detail"],
            [
                [
                    g["kind"],
                    g["entity"],
                    g["route"] + (f" ({g['lane_hint']})" if g.get("lane_hint") else ""),
                    g["message"],
                ]
                for g in d["gaps"]
            ],
        )
    return "\n".join(out).rstrip() + "\n"


def render_html(matrix: Matrix, title: str = "", *, lane: Lane = NO_LANE) -> str:
    from rules_requirements.report.html import render

    return render(to_dict(matrix, title, lane=lane), matrix, lane)


FORMATS = {
    "json": render_json,
    "md": render_markdown,
    "markdown": render_markdown,
    "html": render_html,
}

__all__ = [
    "FORMATS",
    "LOCK_DRIFT",
    "NO_LANE",
    "OUT_OF_LANE",
    "SCHEMA",
    "Lane",
    "attribution_dict",
    "case_rows",
    "cfg",
    "member_dict",
    "out_of_lane_gaps",
    "render_html",
    "render_json",
    "render_markdown",
    "set_line",
    "set_summary",
    "to_dict",
    "unowned_cases",
]
