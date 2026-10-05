# SPDX-License-Identifier: AGPL-3.0-or-later
"""The trace graph: every entity as a node, every reference as an edge.

Exported as Graphviz DOT, Mermaid, JSON, or a self-contained SVG with a
layered layout (needs -> requirements -> mitigations -> risks) whose node order
is refined by the barycenter heuristic to keep edge crossings down.

With :func:`cases` (``rr graph --cases``) every owned test case is a node too,
with exactly one in-edge (``verifies``): from the one entity
:func:`~rules_requirements.attribution.attribute` gave it to. A test case
verifies at most one requirement, so no case node can have two.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Collection, Mapping

from rules_requirements import config as cfg
from rules_requirements.model import Model
from rules_requirements.util import natural_key

if TYPE_CHECKING:
    from rules_requirements.attribution import Attribution

CASE = "case"  # the kind of a test case node


@dataclass(frozen=True)
class Node:
    id: str
    kind: str
    title: str
    status: str = ""


@dataclass(frozen=True)
class Edge:
    source: str
    target: str
    relation: str


# Column of each kind in the layered drawing (test cases, when shown, get one more).
COLUMNS = (cfg.USER_NEED, cfg.REQUIREMENT, cfg.MITIGATION, cfg.RISK)

STATUS_COLORS = {
    "VERIFIED": "#2e9d57",
    "VALIDATED": "#2e9d57",
    "MITIGATED": "#2e9d57",
    "UNDER-VERIFIED": "#e08a1e",
    "PARTIAL": "#c9a400",
    "INCOMPLETE": "#c9a400",
    "FAILED": "#d64545",
    "INVALID": "#d64545",
    "UNVERIFIED": "#8a8f98",
    "UNVALIDATED": "#8a8f98",
    "OPEN": "#8a8f98",
    # test case results
    "passed": "#2e9d57",
    "failed": "#d64545",
    "error": "#d64545",
    "skipped": "#8a8f98",
}


def build(
    model: Model, statuses: Mapping[str, str] | None = None, include_methods: bool = False
) -> tuple[list[Node], list[Edge]]:
    statuses = statuses or {}
    nodes: list[Node] = []
    edges: list[Edge] = []
    kinds = COLUMNS + ((cfg.TEST_METHOD,) if include_methods else ())
    for kind in kinds:
        for ent in sorted(model.section(kind).values(), key=lambda e: natural_key(e.id)):
            nodes.append(Node(ent.id, kind, ent.title, statuses.get(ent.id, "")))
    present = {n.id for n in nodes}
    for req in model.requirements.values():
        for un in req.satisfies:
            edges.append(Edge(req.id, un, "satisfies"))
        for parent in req.refines:
            edges.append(Edge(req.id, parent, "refines"))
        if include_methods and req.method in model.test_methods:
            edges.append(Edge(req.id, req.method, "method"))
    for mit in model.mitigations.values():
        for risk in mit.mitigates:
            edges.append(Edge(mit.id, risk, "mitigates"))
        for req_id in mit.implemented_by:
            edges.append(Edge(mit.id, req_id, "implemented_by"))
    edges = [e for e in edges if e.source in present and e.target in present]
    edges.sort(key=lambda e: (natural_key(e.source), natural_key(e.target), e.relation))
    return nodes, edges


def cases(attribution: Attribution, entities: Collection[str] = ()) -> tuple[list[Node], list[Edge]]:
    """A node per owned test case (status: its result) and its one in-edge,
    ``<owner> verifies <case>``, read from the attribution alone. With
    ``entities``, only the cases of those entities (the ones drawn)."""
    nodes: list[Node] = []
    edges: list[Edge] = []
    for key, owner in attribution.owner.items():
        if entities and owner not in entities:
            continue
        nodes.append(Node(str(key), CASE, key.path, attribution.cases[key].status))
        edges.append(Edge(owner, str(key), "verifies"))
    order = sorted(range(len(nodes)), key=lambda i: (natural_key(edges[i].source), natural_key(nodes[i].id)))
    return [nodes[i] for i in order], [edges[i] for i in order]


def _columns(nodes: list[Node]) -> tuple[str, ...]:
    return COLUMNS + ((CASE,) if any(n.kind == CASE for n in nodes) else ())


def to_json(nodes: list[Node], edges: list[Edge]) -> str:
    return json.dumps(
        {
            "nodes": [n.__dict__ for n in nodes],
            "edges": [e.__dict__ for e in edges],
        },
        indent=2,
    )


def _q(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def to_dot(nodes: list[Node], edges: list[Edge]) -> str:
    shapes = {
        cfg.USER_NEED: "ellipse",
        cfg.REQUIREMENT: "box",
        cfg.MITIGATION: "hexagon",
        cfg.RISK: "diamond",
        cfg.TEST_METHOD: "note",
        CASE: "plaintext",
    }
    out = ["digraph trace {", "  rankdir=LR;", "  node [fontname=Helvetica, fontsize=10];"]
    for n in nodes:
        color = STATUS_COLORS.get(n.status, "#555555")
        label = f"{n.id}\\n{n.title[:40]}"
        out.append(f"  {_q(n.id)} [label={_q(label)}, shape={shapes[n.kind]}, color={_q(color)}];")
    for e in edges:
        style = ", style=dashed" if e.relation in ("refines", "method") else ""
        out.append(f"  {_q(e.source)} -> {_q(e.target)} [label={_q(e.relation)}, fontsize=8{style}];")
    out.append("}")
    return "\n".join(out) + "\n"


def to_mermaid(nodes: list[Node], edges: list[Edge]) -> str:
    def mid(i: str) -> str:
        if re.fullmatch(r"[A-Za-z0-9_.-]+", i):
            return i.replace("-", "_")
        # a test case key (//pkg:t#suite::name): not a Mermaid id
        return "case_" + hashlib.sha1(i.encode("utf-8")).hexdigest()[:12]  # noqa: S324 — an id, not security

    brackets = {
        cfg.USER_NEED: ("([", "])"),
        cfg.REQUIREMENT: ("[", "]"),
        cfg.MITIGATION: ("{{", "}}"),
        cfg.RISK: ("{", "}"),
        cfg.TEST_METHOD: ("[/", "/]"),
        CASE: (">", "]"),
    }
    out = ["flowchart LR"]
    for n in nodes:
        a, b = brackets[n.kind]
        title = n.title.replace('"', "'")[:40]
        label = title if n.kind == CASE else f"{n.id}: {title}"
        out.append(f'  {mid(n.id)}{a}"{label}"{b}')
    for e in edges:
        arrow = "-.->" if e.relation in ("refines", "method") else "-->"
        out.append(f"  {mid(e.source)} {arrow}|{e.relation}| {mid(e.target)}")
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- #
# Layered SVG                                                                 #
# --------------------------------------------------------------------------- #

NODE_W, NODE_H, COL_GAP, ROW_GAP, PAD = 190, 38, 90, 12, 16


def layout(nodes: list[Node], edges: list[Edge], sweeps: int = 4) -> dict[str, tuple[float, float]]:
    """Top-left (x, y) per node id: one column per kind, barycenter ordering."""
    columns = _columns(nodes)
    cols: dict[str, list[str]] = {k: [] for k in columns}
    for n in nodes:
        if n.kind in cols:
            cols[n.kind].append(n.id)
    neighbours: dict[str, list[str]] = {n.id: [] for n in nodes}
    for e in edges:
        if e.source in neighbours and e.target in neighbours:
            neighbours[e.source].append(e.target)
            neighbours[e.target].append(e.source)

    def order_by(col: list[str], ref: dict[str, int]) -> list[str]:
        def bary(i: str) -> tuple[float, tuple[object, ...]]:
            ps = [ref[x] for x in neighbours[i] if x in ref]
            return (sum(ps) / len(ps) if ps else float("inf"), natural_key(i))

        return sorted(col, key=bary)

    order = [cols[k] for k in columns]
    for sweep in range(sweeps):
        rng = range(1, len(order)) if sweep % 2 == 0 else range(len(order) - 2, -1, -1)
        for ci in rng:
            ref_col = order[ci - 1] if sweep % 2 == 0 else order[ci + 1]
            ref = {nid: i for i, nid in enumerate(ref_col)}
            order[ci] = order_by(order[ci], ref)
    pos: dict[str, tuple[float, float]] = {}
    tallest = max((len(c) for c in order), default=0)
    for ci, col in enumerate(order):
        offset = (tallest - len(col)) * (NODE_H + ROW_GAP) / 2
        for ri, nid in enumerate(col):
            pos[nid] = (PAD + ci * (NODE_W + COL_GAP), PAD + 24 + offset + ri * (NODE_H + ROW_GAP))
    return pos


def to_svg(nodes: list[Node], edges: list[Edge], link_prefix: str = "#") -> str:
    pos = layout(nodes, edges)
    shown = [n for n in nodes if n.id in pos]
    if not shown:
        return '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"></svg>'
    columns = _columns(nodes)
    width = PAD * 2 + len(columns) * NODE_W + (len(columns) - 1) * COL_GAP
    height = int(max(y for _, y in pos.values()) + NODE_H + PAD)
    esc = html.escape
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" class="rr-graph" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}" font-family="system-ui, sans-serif" font-size="11">',
        '<defs><marker id="rr-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" '
        'markerHeight="6" orient="auto-start-reverse"><path d="M0,0L10,5L0,10z" fill="#8a8f98"/></marker></defs>',
    ]
    headings = {
        cfg.USER_NEED: "User needs",
        cfg.REQUIREMENT: "Requirements",
        cfg.MITIGATION: "Mitigations",
        cfg.RISK: "Risks",
        CASE: "Test cases",
    }
    for ci, kind in enumerate(columns):
        hx = PAD + ci * (NODE_W + COL_GAP)
        parts.append(f'<text x="{hx}" y="{PAD + 10}" font-weight="600" fill="currentColor">{headings[kind]}</text>')
    for e in edges:
        if e.source not in pos or e.target not in pos:
            continue
        (sx, sy0), (tx, ty0) = pos[e.source], pos[e.target]
        sy, ty = sy0 + NODE_H / 2, ty0 + NODE_H / 2
        if abs(sx - tx) < 1:  # same column (refines): loop out to the right
            x1 = sx + NODE_W
            path = f"M{x1},{sy} C{x1 + 40},{sy} {x1 + 40},{ty} {x1},{ty}"
        else:
            if sx < tx:
                x1, x2 = sx + NODE_W, tx
            else:
                x1, x2 = sx, tx + NODE_W
            mx = (x1 + x2) / 2
            path = f"M{x1},{sy} C{mx},{sy} {mx},{ty} {x2},{ty}"
        dash = ' stroke-dasharray="4 3"' if e.relation == "refines" else ""
        parts.append(
            f'<path d="{path}" fill="none" stroke="#8a8f98" stroke-opacity="0.7" stroke-width="1.2"'
            f'{dash} marker-end="url(#rr-arrow)" data-rel="{esc(e.relation)}"><title>{esc(e.source)} {esc(e.relation)} {esc(e.target)}</title></path>'
        )
    for n in shown:
        x, y = pos[n.id]
        color = STATUS_COLORS.get(n.status, "#8a8f98")
        title = n.title if len(n.title) <= 26 else n.title[:25] + "…"
        if n.kind == CASE:  # a test case: its path and target, no entity page to link to
            target = n.id[: -len(n.title) - 1] if n.id.endswith("#" + n.title) else n.id
            short = target if len(target) <= 28 else "…" + target[-27:]
            parts.append(
                f'<g class="rr-node rr-case" data-id="{esc(n.id)}">'
                f"<title>{esc(n.id)} ({esc(n.status or 'no result')})</title>"
                f'<rect x="{x}" y="{y}" width="{NODE_W}" height="{NODE_H}" rx="3" fill="{color}" fill-opacity="0.08" stroke="{color}" stroke-width="1" stroke-dasharray="3 2"/>'
                f'<text x="{x + 8}" y="{y + 15}" fill="currentColor">{esc(title)}</text>'
                f'<text x="{x + 8}" y="{y + 30}" fill="currentColor" fill-opacity="0.6" font-size="10">{esc(short)}</text></g>'
            )
            continue
        parts.append(
            f'<a href="{esc(link_prefix)}{esc(n.id)}"><g class="rr-node" data-id="{esc(n.id)}">'
            f"<title>{esc(n.id)}: {esc(n.title)} ({esc(n.status or 'no status')})</title>"
            f'<rect x="{x}" y="{y}" width="{NODE_W}" height="{NODE_H}" rx="7" fill="{color}" fill-opacity="0.12" stroke="{color}" stroke-width="1.5"/>'
            f'<text x="{x + 8}" y="{y + 15}" font-weight="700" fill="currentColor">{esc(n.id)}</text>'
            f'<text x="{x + 8}" y="{y + 30}" fill="currentColor" fill-opacity="0.8">{esc(title)}</text></g></a>'
        )
    parts.append("</svg>")
    return "\n".join(parts)
