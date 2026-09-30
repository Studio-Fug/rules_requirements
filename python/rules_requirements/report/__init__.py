# SPDX-License-Identifier: AGPL-3.0-or-later
"""Render a :class:`~rules_requirements.trace.Matrix` as JSON, Markdown or HTML.

The JSON document is the canonical form: fully deterministic (sorted, no
timestamps, no machine-specific paths), so it can be checked into a repository
as a golden file and diffed in review. Markdown and HTML are views of it.
"""

from __future__ import annotations

import json
from typing import Any

from rules_requirements import config as cfg
from rules_requirements.trace import Matrix
from rules_requirements.util import natural_key

SCHEMA = "rules_requirements/report/v1"


def _ids(items: Any) -> list[str]:
    return sorted((getattr(i, "id", i) for i in items), key=natural_key)


def to_dict(matrix: Matrix, title: str = "") -> dict[str, Any]:
    m = matrix.model
    v = matrix.verdicts

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
        ev = v[ent.id].evidence
        if ev:
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

    return {
        "schema": SCHEMA,
        "title": title or str(m.project.get("name", "Requirements traceability")),
        "project": dict(sorted(m.project.items())),
        "summary": matrix.counts(),
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
        "gaps": [g.to_dict() for g in matrix.gaps],
    }


def render_json(matrix: Matrix, title: str = "") -> str:
    return json.dumps(to_dict(matrix, title), indent=2, sort_keys=False) + "\n"


# --------------------------------------------------------------------------- #
# Markdown                                                                    #
# --------------------------------------------------------------------------- #

_ICON = {
    "VERIFIED": "✅",
    "VALIDATED": "✅",
    "MITIGATED": "✅",
    "UNDER-VERIFIED": "🟠",
    "PARTIAL": "🟡",
    "FAILED": "❌",
    "UNVERIFIED": "⚪",
    "UNVALIDATED": "⚪",
    "OPEN": "⚪",
}


def _md(text: Any) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _badge(status: str) -> str:
    return f"{_ICON.get(status, '')} {status}".strip()


def render_markdown(matrix: Matrix, title: str = "") -> str:
    d = to_dict(matrix, title)
    s = d["summary"]
    out = [f"# {_md(d['title'])}", ""]
    out.append(
        f"**{s['user_needs_validated']}/{s['user_needs']}** user needs validated · "
        f"**{s['requirements_verified']}/{s['requirements']}** requirements verified "
        f"({s['requirements_under_verified']} under-verified, {s['requirements_failed']} failed, "
        f"{s['requirements_unverified']} unverified) · "
        f"**{s['risks_mitigated']}/{s['risks']}** risks mitigated · "
        f"{s['test_cases']} test cases · {s['gaps']} gaps"
    )
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

    out += ["## User needs — validation", ""]
    table(
        ["ID", "Need", "Requirements", "Status"],
        [[n["id"], n["title"], ", ".join(n["requirements"]), _badge(n["status"])] for n in d["user_needs"]],
    )
    out += ["## Requirements — verification", ""]

    def evidence(item: dict[str, Any]) -> str:
        parts = []
        for e in item.get("evidence", []):
            mark = {"passed": "✓", "failed": "✗", "error": "✗", "skipped": "–"}.get(e["status"], "?")
            stale = " (stale)" if e.get("stale") else ""
            parts.append(f"{mark} {e['name']} [{e['level']}]{stale}")
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
                _badge(r["status"]),
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
                    _badge(x["status"]),
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
    if d["gaps"]:
        out += ["## Gaps", ""]
        table(
            ["Kind", "Entity", "Route", "Detail"],
            [[g["kind"], g["entity"], g["route"], g["message"]] for g in d["gaps"]],
        )
    return "\n".join(out).rstrip() + "\n"


def render_html(matrix: Matrix, title: str = "") -> str:
    from rules_requirements.report.html import render

    return render(to_dict(matrix, title), matrix)


FORMATS = {
    "json": render_json,
    "md": render_markdown,
    "markdown": render_markdown,
    "html": render_html,
}

__all__ = ["FORMATS", "SCHEMA", "cfg", "render_html", "render_json", "render_markdown", "to_dict"]
