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
        "| REQ-2 | Accept setpoints between 5 and 30 C | satisfies UN-2 | simulation | set 0/1 passed · 1 failed<br>"
        "✗ suite::range [sil] | ❌ FAILED |" in md
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
    assert (
        "set 1/2 passed · 1 quarantined<br>✓ suite::heat [simulation]<br>⛔ suite::both (multi-tag) | ❌ INVALID |"
        in row
    )
    (row,) = [line for line in md.splitlines() if line.startswith("| REQ-2 |")]
    assert "| set 0/1 passed · 1 quarantined<br>⛔ suite::both (multi-tag) | ❌ INVALID |" in row
    # The banner at the top names the case, its code and every id it names.
    assert "> ⛔ **ATTRIBUTION ERROR: 1 quarantined test case(s) count for no requirement**" in md
    assert "> - `//pkg:t#suite::both` — **multi-tag**: declared REQ-1, REQ-2" in md


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


# --------------------------------------------------------------------------- #
# Report v2: the inverse matrix, sets, attribution, lanes                     #
# --------------------------------------------------------------------------- #


def _quarantine_matrix(tmp_path, model):
    cases = [("heat", "passed", ["REQ-1"], ""), ("both", "passed", ["REQ-1", "REQ-2"], ""), ("free", "failed", [], "")]
    path = junit(tmp_path, "bazel-testlogs/pkg/t/test.xml", cases)
    return build_matrix(model, ingest.collect([path]))


def test_v2_json_cases_are_the_inverse_matrix(tmp_path, model):
    m = _quarantine_matrix(tmp_path, model)
    d = report.to_dict(m)
    assert d["schema"] == "rules_requirements/report/v2"
    by_case = {row["case"]: row for row in d["cases"]}
    assert set(by_case) == {"//pkg:t#suite::heat", "//pkg:t#suite::both", "//pkg:t#suite::free"}
    assert by_case["//pkg:t#suite::heat"]["owner"] == "REQ-1" and by_case["//pkg:t#suite::heat"]["via"] == "tag"
    both = by_case["//pkg:t#suite::both"]
    assert both["owner"] is None and both["quarantine"] == "multi-tag" and both["declared"] == ["REQ-1", "REQ-2"]
    assert by_case["//pkg:t#suite::free"]["owner"] is None and "quarantine" not in by_case["//pkg:t#suite::free"]
    s = d["summary"]
    assert (s["test_cases"], s["test_cases_owned"], s["test_cases_unowned"], s["test_cases_quarantined"]) == (
        3,
        1,
        1,
        1,
    )
    # The 0.2 summary keys are all still there (0.3.x compatibility).
    for key in ("requirements_verified", "requirements_failed", "requirements_unverified", "gaps", "test_cases"):
        assert key in s
    att = d["attribution"]
    assert att["mode"] == "hybrid" and att["lock"] is None and att["lane"] is None
    assert att["targets"]["//pkg:t"] == {
        "cases": 3, "owned": 1, "quarantined": 1, "owners": ["REQ-1"], "synthetic": False, "ran": True,
    }  # fmt: skip
    assert [q["case"] for q in att["quarantined"]] == ["//pkg:t#suite::both"]
    assert att["granularity"] == {
        "owned_by_literal": 0, "owned_by_pattern": 0, "owned_by_whole": 0, "owned_by_tag": 1, "coarse_claims": 0,
    }  # fmt: skip


def test_v2_entities_carry_their_set_members_and_basis(tmp_path, model):
    d = report.to_dict(_quarantine_matrix(tmp_path, model))
    req1, req2, req3 = d["requirements"]
    assert req1["status"] == "INVALID" and req1["basis"] == "own"
    assert req1["set"] == {
        "complete": False, "members": 2, "passed": 1, "failed": 0, "error": 0, "skipped": 0,
        "missing": 0, "not_run": 0, "moved": 0, "quarantined": 1,
    }  # fmt: skip
    assert [(mb["case"], mb["state"], mb["via"]) for mb in req1["members"]] == [
        ("//pkg:t#suite::both", "quarantined", "tag"),
        ("//pkg:t#suite::heat", "passed", "tag"),
    ]
    assert req1["evidence"] == [{"name": "suite::heat", "status": "passed", "level": "simulation", "target": "//pkg:t"}]
    assert req3["members"] == [] and req3["set"]["members"] == 0 and req3["set"]["complete"] is False
    un1 = d["user_needs"][0]
    assert un1["basis"] == "derived" and un1["derived_from"] == ["REQ-1"] and un1["members"] == []
    assert d["risks"][0]["basis"] == "derived" and "members" not in d["risks"][0]


def test_v2_html_shows_the_banner_sets_and_case_attribution(tmp_path, model):
    page = report.render_html(_quarantine_matrix(tmp_path, model))
    assert "ATTRIBUTION ERROR: 1 quarantined test case(s)" in page
    assert "<code>//pkg:t#suite::both</code> — <b>multi-tag</b>" in page
    assert '<details class="set"><summary>set 1/2 passed · 1 quarantined</summary>' in page
    assert '<h2 id="cases">Case attribution</h2>' in page and "<code>suite::free</code> (failed)" in page
    assert "1 owned, 1 unowned, 1 quarantined" in page


def test_v2_markdown_sections(tmp_path, model):
    md = report.render_markdown(_quarantine_matrix(tmp_path, model))
    assert "## Verification sets" in md and "### REQ-1 — ❌ INVALID" in md
    assert "## Case attribution" in md and "| //pkg:t | 3 | 1 | 1 | 1 | REQ-1 |" in md
    assert "- `//pkg:t`: `suite::free` (failed)" in md
    assert "3 test cases (1 owned, 1 unowned, 1 quarantined)" in md
    assert "_attribution: hybrid · lock: none (sets not pinned)_" in md


def test_lanes_label_out_of_lane_members_and_never_change_a_verdict(tmp_path):
    from conftest import write

    from rules_requirements.model import load_model

    text = (
        "user_needs: [{id: UN-1, title: n}]\n"
        "requirements:\n"
        "  - {id: REQ-1, title: a, satisfies: [UN-1], verified_by: [{target: //sw:t, cases: ['c::a']},"
        " {target: //hitl:t, cases: ['c::h']}]}\n"
        "  - {id: REQ-2, title: b, satisfies: [UN-1], verified_by: [{target: //sw:u, cases: ['c::b']}]}\n"
    )
    model = load_model(write(tmp_path, "m.yaml", text))
    ev = ingest.Evidence()
    ev.add(ingest.TestCase("a", "passed", classname="c", target="//sw:t"))
    plain = build_matrix(model, ev)
    lane = report.Lane("software", frozenset({"//sw:t", "//sw:u"}))
    d = report.to_dict(plain, lane=lane)
    assert d["attribution"]["lane"] == "software"
    assert d["attribution"]["targets"]["//hitl:t"]["in_lane"] is False
    req1, req2 = d["requirements"]
    assert req1["status"] == "INCOMPLETE" == plain.status("REQ-1")  # verdicts identical with or without a lane
    hitl = [mb for mb in req1["members"] if mb["target"] == "//hitl:t"]
    assert hitl and hitl[0]["lane_hint"] == "out of lane"
    # REQ-2's member did not run although its target is in this lane: no hint, and its gap is kept.
    assert req2["status"] == "UNVERIFIED" and "lane_hint" not in req2["members"][0]
    away = report.out_of_lane_gaps(plain, lane)
    assert [(g.kind, g.entity) for g in away] == [("incomplete", "REQ-1")]
    gaps = {(g["kind"], g["entity"]): g for g in d["gaps"]}
    assert gaps[("incomplete", "REQ-1")]["lane_hint"] == "out of lane"
    assert "lane_hint" not in gaps[("unverified", "REQ-2")]
    md = report.render_markdown(plain, lane=lane)
    assert "set 1/2 passed · 1 not run (out of lane)" in md and "lane: software" in md
    # Without --lane-targets nothing is labelled.
    assert report.out_of_lane_gaps(plain, report.Lane("software")) == []
