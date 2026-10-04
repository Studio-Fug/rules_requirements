# SPDX-License-Identifier: AGPL-3.0-or-later
"""Self-contained HTML traceability report (no external assets)."""

from __future__ import annotations

import html
from typing import Any

from rules_requirements import graph
from rules_requirements.trace import Matrix

_CLASS = {
    "VERIFIED": "ok",
    "VALIDATED": "ok",
    "MITIGATED": "ok",
    "UNDER-VERIFIED": "amber",
    "PARTIAL": "warn",
    "INCOMPLETE": "warn",
    "FAILED": "fail",
    "INVALID": "fail",
}


def _e(text: Any) -> str:
    return html.escape("" if text is None else str(text))


def _badge(status: str) -> str:
    return f'<span class="badge {_CLASS.get(status, "none")}">{_e(status)}</span>'


def _links(ids: list[str]) -> str:
    return ", ".join(f'<a href="#{_e(i)}">{_e(i)}</a>' for i in ids) or '<span class="muted">—</span>'


def _evidence(item: dict[str, Any]) -> str:
    ev = item.get("evidence", [])
    if not ev:
        return '<span class="muted">no evidence</span>'
    lis = []
    for e in ev:
        cls = {"passed": "pass", "skipped": "skip"}.get(e["status"], "fail")
        extra = ' <span class="stale-text">stale</span>' if e.get("stale") else ""
        msg = f'<div class="msg">{_e(e["message"])}</div>' if e.get("message") else ""
        where = (
            f' <span class="muted">{_e(e["target"])}</span>' if e.get("target") and e.get("kind") != "target" else ""
        )
        lis.append(f'<li class="{cls}">{_e(e["name"])}{where} <code>{_e(e["level"])}</code>{extra}{msg}</li>')
    return f'<ul class="ev">{"".join(lis)}</ul>'


def _source_refs(item: dict[str, Any]) -> str:
    out = []
    for key, label in (("implemented_in", "implemented in"), ("verified_in", "verified in")):
        refs = item.get(key)
        if refs is None:
            continue
        if not refs:
            if key == "implemented_in":
                out.append(f'<div class="src muted">{label}: —</div>')
            continue
        bits = []
        for r in refs:
            sym = f" <b>{_e(r['symbol'])}</b>" if r.get("symbol") else ""
            txt = f" — {_e(r['text'])}" if r.get("text") else ""
            bits.append(f"<code>{_e(r['path'])}:{r['line']}</code>{sym}{txt}")
        out.append(f'<div class="src">{label}: ' + "; ".join(bits) + "</div>")
    return "".join(out)


def _notes(item: dict[str, Any]) -> str:
    notes = item.get("open_notes") or []
    return "".join(f'<div class="note"><b>{_e(n["kind"])}</b> {_e(n["text"])}</div>' for n in notes)


def render(d: dict[str, Any], matrix: Matrix) -> str:
    s = d["summary"]
    nodes, edges = graph.build(matrix.model, {k: v.status for k, v in matrix.verdicts.items()})
    svg = graph.to_svg(nodes, edges)

    def stat(label: str, num: int, den: int) -> str:
        pct = round(100 * num / den) if den else 100
        return (
            f'<div class="stat"><div class="num">{num}<span>/{den}</span></div>'
            f'<div class="lbl">{_e(label)}</div><div class="bar"><i style="width:{pct}%"></i></div></div>'
        )

    stats = "".join(
        [
            stat("user needs validated", s["user_needs_validated"], s["user_needs"]),
            stat("requirements verified", s["requirements_verified"], s["requirements"]),
            stat("mitigations verified", s["mitigations_verified"], s["mitigations"]),
            stat("risks mitigated", s["risks_mitigated"], s["risks"]),
        ]
    )

    alerts = []
    if d["high_open_risks"]:
        alerts.append(
            '<div class="alert fail"><b>High-severity risks not mitigated:</b> '
            + _links(d["high_open_risks"])
            + "</div>"
        )
    if d["pyramid_violations"]:
        alerts.append(
            '<div class="alert amber"><b>Cost-pyramid policy:</b> '
            + _links(d["pyramid_violations"])
            + " rest only on expensive evidence with no cheaper analysis/simulation backing.</div>"
        )
    if d["unknown_evidence"]:
        alerts.append(
            '<div class="alert fail"><b>Evidence references undefined ids:</b> '
            + ", ".join(_e(k) for k in d["unknown_evidence"])
            + "</div>"
        )

    rows_un = "".join(
        f'<tr id="{_e(n["id"])}"><td class="id">{_e(n["id"])}</td><td>{_e(n["title"])}'
        f'<div class="desc">{_e(n.get("description", ""))}</div>{_notes(n)}</td>'
        f"<td>{_links(n['requirements'])}</td><td>{_badge(n['status'])}</td></tr>"
        for n in d["user_needs"]
    )

    def req_row(r: dict[str, Any]) -> str:
        trace = []
        for key, label in (("satisfies", "satisfies"), ("refines", "refines"), ("implements", "implements")):
            if r[key]:
                trace.append(f"{label} {_links(r[key])}")
        if r.get("modules"):
            trace.append("modules: " + _e(", ".join(r["modules"])))
        demand = f"demands <code>{_e(r['demanded_level'])}</code>"
        if r.get("method"):
            demand += (
                f' via <a href="#{_e(r["method"])}">{_e(r["method"])}</a>' if r["method"] != r["demanded_level"] else ""
            )
        if r.get("provided_level"):
            demand += f" · best <code>{_e(r['provided_level'])}</code>"
        badge = _badge(r["status"]) + (' <span class="badge stale">STALE</span>' if r.get("stale") else "")
        return (
            f'<tr id="{_e(r["id"])}"><td class="id">{_e(r["id"])}</td><td>{_e(r["title"])}'
            f'<div class="desc">{_e(r.get("description", ""))}</div>'
            f'<div class="trace">{" · ".join(trace)}</div>{_source_refs(r)}{_notes(r)}</td>'
            f'<td>{_evidence(r)}<div class="demand">{demand}</div></td><td>{badge}</td></tr>'
        )

    rows_req = "".join(req_row(r) for r in d["requirements"])
    rows_mit = "".join(
        f'<tr id="{_e(x["id"])}"><td class="id">{_e(x["id"])}<div class="kind">{_e(x["type"] or "")}</div></td>'
        f'<td>{_e(x["title"])}<div class="desc">{_e(x.get("description", ""))}</div>{_notes(x)}</td>'
        f"<td>mitigates {_links(x['mitigates'])}<br>implemented by {_links(x['implemented_by'])}</td>"
        f"<td>{_badge(x['status'])}</td></tr>"
        for x in d["mitigations"]
    )

    def risk_row(r: dict[str, Any]) -> str:
        chain = " → ".join(_e(r[k]) for k in ("hazard", "hazardous_situation", "harm") if r.get(k))
        est = f"{_e(r['severity'] or '?')} × {_e(r['likelihood'] or '?')}"
        if r.get("score") is not None:
            est += f' <span class="muted">({r["score"]})</span>'
        if r["residual_severity"] or r["residual_likelihood"]:
            est += f"<br>residual {_e(r['residual_severity'] or r['severity'])} × {_e(r['residual_likelihood'] or r['likelihood'])}"
            if r.get("residual_score") is not None:
                est += f' <span class="muted">({r["residual_score"]})</span>'
        sev = r["severity"] or ""
        return (
            f'<tr id="{_e(r["id"])}"><td class="id">{_e(r["id"])}<div class="kind sev-{_e(sev)}">{_e(sev)}</div></td>'
            f'<td>{_e(r["title"])}<div class="desc">{_e(r.get("description", ""))}</div>'
            + (f'<div class="trace">{chain}</div>' if chain else "")
            + (f'<div class="desc">residual: {_e(r["residual"])}</div>' if r.get("residual") else "")
            + f"{_notes(r)}</td><td>{est}</td><td>{_links(r['mitigations'])}</td><td>{_badge(r['status'])}</td></tr>"
        )

    rows_risk = "".join(risk_row(r) for r in d["risks"])
    rows_tm = "".join(
        f'<tr id="{_e(t["id"])}"><td class="id">{_e(t["id"])}</td><td>{_e(t["title"])}'
        f'<div class="desc">{_e(t.get("description", ""))}</div></td><td><code>{_e(t["level"])}</code></td>'
        f"<td>{_links(t['used_by'])}</td></tr>"
        for t in d["test_methods"]
    )
    rows_mod = "".join(f'<tr><td class="id">{_e(k)}</td><td>{_badge(v)}</td></tr>' for k, v in d["modules"].items())
    rows_gap = "".join(
        f'<tr><td><code>{_e(g["kind"])}</code></td><td><a href="#{_e(g["entity"])}">{_e(g["entity"])}</a></td>'
        f'<td><span class="route {_e(g["route"])}">{_e(g["route"])}</span></td><td>{_e(g["message"])}</td></tr>'
        for g in d["gaps"]
    )

    def section(title: str, head: list[str], rows: str, sid: str) -> str:
        if not rows:
            return ""
        header_cells = "".join(f"<th>{_e(h)}</th>" for h in head)
        return f'<h2 id="{sid}">{title}</h2><div class="tw"><table><thead><tr>{header_cells}</tr></thead><tbody>{rows}</tbody></table></div>'

    body = "".join(
        [
            section("User needs — validation", ["ID", "Need", "Requirements", "Status"], rows_un, "needs"),
            section(
                "Requirements — verification", ["ID", "Requirement", "Evidence", "Status"], rows_req, "requirements"
            ),
            section("Risks — control", ["ID", "Risk", "Estimate", "Mitigations", "Status"], rows_risk, "risks"),
            section(
                "Mitigations — risk control measures", ["ID", "Mitigation", "Traces", "Status"], rows_mit, "mitigations"
            ),
            section("Test methods", ["ID", "Method", "Level", "Used by"], rows_tm, "methods"),
            section("Modules — rolled-up verification", ["Module", "Status"], rows_mod, "modules"),
            section("Gaps — work queue", ["Kind", "Entity", "Route", "Detail"], rows_gap, "gaps"),
        ]
    )
    source = d["project"].get("source") or d["project"].get("description") or ""
    return _TEMPLATE.format(
        title=_e(d["title"]),
        source=_e(source),
        stats=stats,
        alerts="".join(alerts),
        graph=svg,
        body=body,
        cases=s["test_cases"],
        gaps=s["gaps"],
    )


_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
:root {{
  --bg: #fbfbfa; --fg: #1d1f23; --muted: #6b7079; --line: #e3e4e8; --card: #ffffff;
  --ok: #2e9d57; --fail: #d64545; --amber: #e08a1e; --warn: #c9a400; --stale: #7c55d9; --accent: #3b6fd6;
}}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg: #15171b; --fg: #e6e7ea; --muted: #9aa0aa; --line: #2c3038; --card: #1c1f25; }}
}}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: var(--bg); color: var(--fg); font: 14px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }}
main {{ max-width: 1180px; margin: 0 auto; padding: 28px 16px 64px; }}
h1 {{ font-size: 1.6rem; margin: 0 0 4px; letter-spacing: -0.01em; }}
h2 {{ font-size: 1.1rem; margin: 36px 0 10px; }}
a {{ color: var(--accent); text-decoration: none; }} a:hover {{ text-decoration: underline; }}
code {{ font: 12px ui-monospace, SFMono-Regular, Menlo, monospace; }}
.sub {{ color: var(--muted); margin: 0 0 20px; }}
.stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 12px; }}
.stat {{ background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 12px 14px; }}
.stat .num {{ font-size: 1.7rem; font-weight: 700; font-variant-numeric: tabular-nums; }}
.stat .num span {{ font-size: 1rem; color: var(--muted); font-weight: 500; }}
.stat .lbl {{ color: var(--muted); font-size: 12px; }}
.bar {{ height: 4px; background: var(--line); border-radius: 2px; margin-top: 8px; overflow: hidden; }}
.bar i {{ display: block; height: 100%; background: var(--ok); }}
.alert {{ margin: 12px 0 0; padding: 10px 14px; border-radius: 8px; border-left: 3px solid; background: var(--card); }}
.alert.fail {{ border-color: var(--fail); }} .alert.amber {{ border-color: var(--amber); }}
.graph {{ background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 8px; overflow-x: auto; margin-top: 12px; }}
.graph svg {{ max-width: none; color: var(--fg); }}
.tw {{ overflow-x: auto; }}
table {{ width: 100%; border-collapse: collapse; background: var(--card); border: 1px solid var(--line); border-radius: 10px; }}
th, td {{ text-align: left; vertical-align: top; padding: 9px 10px; border-bottom: 1px solid var(--line); }}
th {{ font-size: 12px; color: var(--muted); font-weight: 600; }}
tr:target {{ background: color-mix(in srgb, var(--accent) 10%, transparent); }}
td.id {{ font-family: ui-monospace, monospace; font-weight: 700; white-space: nowrap; }}
.desc {{ color: var(--muted); font-size: 12.5px; }}
.trace, .demand, .src {{ color: var(--muted); font-size: 12px; margin-top: 3px; }}
.kind {{ font-size: 10.5px; text-transform: uppercase; letter-spacing: .05em; color: var(--muted); font-weight: 600; }}
.sev-high, .sev-critical {{ color: var(--fail); }}
.badge {{ display: inline-block; padding: 1px 8px; border-radius: 999px; font-size: 11.5px; font-weight: 700; white-space: nowrap;
  color: var(--muted); background: color-mix(in srgb, var(--muted) 14%, transparent); }}
.badge.ok {{ color: var(--ok); background: color-mix(in srgb, var(--ok) 14%, transparent); }}
.badge.fail {{ color: var(--fail); background: color-mix(in srgb, var(--fail) 14%, transparent); }}
.badge.amber {{ color: var(--amber); background: color-mix(in srgb, var(--amber) 14%, transparent); }}
.badge.warn {{ color: var(--warn); background: color-mix(in srgb, var(--warn) 16%, transparent); }}
.badge.stale {{ color: var(--stale); background: color-mix(in srgb, var(--stale) 14%, transparent); }}
.stale-text {{ color: var(--stale); font-size: 12px; }}
ul.ev {{ margin: 0; padding-left: 16px; font-size: 12.5px; }}
ul.ev li.pass::marker {{ color: var(--ok); }} ul.ev li.fail {{ color: var(--fail); }} ul.ev li.skip {{ color: var(--muted); }}
.msg {{ font: 11.5px ui-monospace, monospace; color: var(--fail); white-space: pre-wrap; }}
.note {{ font-size: 12px; margin-top: 4px; padding: 3px 8px; border-radius: 6px; background: color-mix(in srgb, var(--amber) 12%, transparent); }}
.route {{ font-size: 11.5px; font-weight: 600; }} .route.human-gate {{ color: var(--amber); }}
.muted {{ color: var(--muted); }}
footer {{ margin-top: 40px; color: var(--muted); font-size: 12px; }}
</style></head>
<body><main>
<h1>{title}</h1>
<p class="sub">{source}</p>
<div class="stats">{stats}</div>
{alerts}
<h2 id="graph">Trace graph</h2>
<div class="graph">{graph}</div>
{body}
<footer>{cases} test cases · {gaps} gaps · generated by rules_requirements</footer>
</main></body></html>
"""
