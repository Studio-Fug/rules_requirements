# SPDX-License-Identifier: AGPL-3.0-or-later
import json

from conftest import junit

from rules_requirements import graph, ingest, report
from rules_requirements.annotations import Reference
from rules_requirements.trace import build_matrix

CASES = [
    ("heat", "passed", ["REQ-1"], ""),
    ("range", "failed", ["REQ-2"], "sil"),
    ("cutoff", "passed", ["REQ-3"], "hil", [("artifact.sha", "old")]),
]


def make(tmp_path, model, **kw):
    path = junit(tmp_path, "bazel-testlogs/pkg/t/test.xml", CASES)
    return build_matrix(model, ingest.collect([path]), **kw)


def test_json_is_deterministic_and_complete(tmp_path, model):
    m = make(tmp_path, model, current_build={"sha": "new"})
    text = report.render_json(m)
    assert text == report.render_json(make(tmp_path, model, current_build={"sha": "new"}))
    d = json.loads(text)
    assert d["schema"] == report.SCHEMA
    assert d["title"] == "Thermostat"
    assert [r["id"] for r in d["requirements"]] == ["REQ-1", "REQ-2", "REQ-3"]
    req3 = d["requirements"][2]
    assert req3["status"] == "UNDER-VERIFIED" and req3["stale"] and req3["pyramid_violation"]
    assert req3["method"] == "TM-1" and req3["implements"] == ["MIT-1"]
    assert req3["evidence"] == [
        {"name": "suite::cutoff", "status": "passed", "level": "hil", "target": "//pkg:t", "stale": True}
    ]
    assert d["requirements"][1]["evidence"][0]["message"] == "boom"
    risk = d["risks"][0]
    assert risk["score"] == 12 and risk["residual_score"] == 4 and risk["hazard"] == "Heater stuck on"
    assert d["test_methods"][0]["used_by"] == ["REQ-3"]
    assert d["high_open_risks"] == ["RISK-1"]
    assert "source" not in text  # no machine-specific paths


def test_title_override_notes_and_annotations(tmp_path, model):
    from dataclasses import replace

    from rules_requirements.model import Note

    un = replace(
        model.user_needs["UN-1"], notes=(Note("is heat enough?", kind="question"), Note("done", status="resolved"))
    )
    refs = [Reference(("REQ-1",), "implements", "src/c.py", 4, "heats", "def heat")]
    m = build_matrix(model.with_entity(un), ingest.Evidence(), references=refs)
    d = report.to_dict(m, title="Custom")
    assert d["title"] == "Custom"
    assert d["user_needs"][0]["open_notes"] == [{"kind": "question", "text": "is heat enough?"}]
    assert d["requirements"][0]["implemented_in"][0]["symbol"] == "def heat"
    assert d["requirements"][1]["implemented_in"] == []


def test_markdown(tmp_path, model):
    md = report.render_markdown(make(tmp_path, model, current_build={"sha": "new"}))
    assert md.startswith("# Thermostat\n")
    assert (
        "| REQ-2 | Accept setpoints between 5 and 30 C | satisfies UN-2 | simulation | ✗ suite::range [sil] | ❌ FAILED |"
        in md
    )
    assert "> **High-severity risks not mitigated:** RISK-1" in md
    assert "## Gaps" in md and "## Modules" in md and "## Test methods" in md
    assert "high × possible → high × rare" in md
    assert "## Implementation" not in md


def test_markdown_shows_the_quarantined_case_beside_invalid(tmp_path, model):
    """An INVALID row names the case that made it INVALID, not only its passes."""
    cases = [("heat", "passed", ["REQ-1"], ""), ("both", "passed", ["REQ-1", "REQ-2"], "")]
    path = junit(tmp_path, "bazel-testlogs/pkg/t/test.xml", cases)
    md = report.render_markdown(build_matrix(model, ingest.collect([path])))
    (row,) = [line for line in md.splitlines() if line.startswith("| REQ-1 |")]
    assert "✓ suite::heat [simulation]<br>⛔ suite::both (multi-tag) | ❌ INVALID |" in row
    (row,) = [line for line in md.splitlines() if line.startswith("| REQ-2 |")]
    assert "| ⛔ suite::both (multi-tag) | ❌ INVALID |" in row


def test_markdown_implementation_section(model):
    refs = [
        Reference(("REQ-1",), "implements", "src/c.py", 4, "", "def heat"),
        Reference(("REQ-1",), "verifies", "t.py", 2),
    ]
    md = report.render_markdown(build_matrix(model, ingest.Evidence(), references=refs))
    assert "| REQ-1 | src/c.py:4 (def heat) | t.py:2 |" in md
    assert "| REQ-2 | — | — |" in md


def test_html(tmp_path, model):
    m = make(tmp_path, model, current_build={"sha": "new"})
    refs = [Reference(("REQ-1",), "implements", "src/c.py", 4, "heats <b>", "def heat")]
    m2 = build_matrix(model, m.evidence, current_build={"sha": "new"}, references=refs)
    page = report.render_html(m2)
    assert page.startswith("<!doctype html>")
    assert '<tr id="REQ-3">' in page and "STALE" in page and "Cost-pyramid" in page
    assert "heats &lt;b&gt;" in page  # escaped
    assert "<svg" in page and 'data-id="MIT-1"' in page
    assert "prefers-color-scheme: dark" in page
    assert report.FORMATS["md"] is report.render_markdown


def test_graph_exports(model):
    nodes, edges = graph.build(model, {"REQ-1": "VERIFIED"}, include_methods=True)
    assert {(e.source, e.target, e.relation) for e in edges} == {
        ("REQ-1", "UN-1", "satisfies"),
        ("REQ-2", "UN-2", "satisfies"),
        ("REQ-3", "TM-1", "method"),
        ("MIT-1", "RISK-1", "mitigates"),
        ("MIT-1", "REQ-3", "implemented_by"),
    }
    dot = graph.to_dot(nodes, edges)
    assert dot.startswith("digraph trace {") and '"REQ-3" -> "TM-1"' in dot and "#2e9d57" in dot
    mer = graph.to_mermaid(nodes, edges)
    assert "MIT_1{{" in mer and "REQ_3 -.->|method| TM_1" in mer
    data = json.loads(graph.to_json(nodes, edges))
    assert len(data["nodes"]) == 8
    svg = graph.to_svg(nodes, edges)
    assert svg.count('class="rr-node"') == 7  # test methods are not drawn in columns
    assert graph.to_svg([], []).startswith("<svg")


def test_graph_refines_loop_and_layout(tmp_path):
    from conftest import write

    from rules_requirements.model import load_model

    m = load_model(
        write(
            tmp_path,
            "m.yaml",
            "user_needs: [{id: UN-1, title: n}]\nrequirements: [{id: REQ-1, title: a, satisfies: [UN-1]}, {id: REQ-2, title: a very long title that will be truncated for display, refines: [REQ-1]}]\n",
        )
    )
    nodes, edges = graph.build(m)
    pos = graph.layout(nodes, edges)
    assert pos["REQ-1"][0] == pos["REQ-2"][0]
    svg = graph.to_svg(nodes, edges)
    assert 'stroke-dasharray="4 3"' in svg and "…" in svg


def test_graph_case_nodes_read_the_attribution(model):
    from rules_requirements.attribution import attribute
    from rules_requirements.ingest import Evidence, TestCase

    ev = Evidence()
    ev.add(TestCase("a", "passed", classname="s", target="//t:x_test", declared=("REQ-1",)))
    ev.add(TestCase("b", "passed", classname="s", target="//t:x_test", declared=("REQ-1", "REQ-2")))
    att = attribute(model, ev)
    nodes, edges = graph.cases(att)
    assert [(n.id, n.kind, n.status) for n in nodes] == [("//t:x_test#s::a", "case", "passed")]  # b: multi-tag
    assert [(e.source, e.target, e.relation) for e in edges] == [(o, str(k), "verifies") for k, o in att.owner.items()]
    assert len({e.target for e in edges}) == len(edges)  # one in-edge per case
    assert graph.cases(att, {"REQ-2"}) == ([], [])
    entity_nodes, entity_edges = graph.build(model)
    svg = graph.to_svg(entity_nodes + nodes, entity_edges + edges)
    assert "Test cases" in svg and svg.count("rr-case") == len(nodes)
