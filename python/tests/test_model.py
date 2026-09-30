# SPDX-License-Identifier: AGPL-3.0-or-later
import pytest
from conftest import MODEL, write

from rules_requirements.model import Location, load_model, model_files, read_model
from rules_requirements.validate import ValidationError


def test_load_sections(model):
    assert sorted(model.user_needs) == ["UN-1", "UN-2"]
    req = model.requirements["REQ-3"]
    assert req.method == "TM-1"
    assert model.demanded_level(req) == "hil"
    assert model.demanded_level(model.requirements["REQ-1"]) == "simulation"
    assert [m.id for m in model.mitigations_for_risk("RISK-1")] == ["MIT-1"]
    assert [r.id for r in model.requirements_for_risk("RISK-1")] == ["REQ-3"]
    assert [m.id for m in model.mitigations_implemented_by("REQ-3")] == ["MIT-1"]
    assert [r.id for r in model.requirements_for_method("TM-1")] == ["REQ-3"]
    assert model.modules() == ["controller", "interlock"]
    assert model.get("RISK-1").harm == "Burns, heat stress"
    assert model.get("NOPE-1") is None
    assert [e.id for e in model.entities()][:3] == ["UN-1", "UN-2", "REQ-1"]


def test_locations_have_lines(model, model_path):
    loc = model.requirements["REQ-2"].location
    assert loc.path == model_path
    assert loc.line > 1
    assert str(loc) == f"{model_path}:{loc.line}"
    assert str(Location()) == "<model>"
    assert str(Location("a.yaml")) == "a.yaml"


def test_directory_layout_with_single_object_documents(tmp_path):
    write(tmp_path, "m/config.yaml", "config:\n  prefixes: {requirement: PR}\nproject: {name: X}\n")
    write(tmp_path, "m/needs/UN-1.yaml", "kind: user_need\nid: UN-1\ntitle: Need\n")
    write(tmp_path, "m/reqs/PR-1.yaml", "kind: requirement\nid: PR-1\ntitle: Req\nsatisfies: UN-1\n")
    write(tmp_path, "m/.hidden/x.yaml", "kind: nonsense\n")
    write(tmp_path, "m/README.md", "not a model")
    files = model_files(str(tmp_path / "m"))
    assert [f.rsplit("/", 1)[-1] for f in files] == ["config.yaml", "UN-1.yaml", "PR-1.yaml"]
    m = load_model(str(tmp_path / "m"), root=str(tmp_path))
    assert m.requirements["PR-1"].satisfies == ("UN-1",)
    assert m.requirements["PR-1"].location.path == "m/reqs/PR-1.yaml"
    assert m.project["name"] == "X"


def test_multi_document_file(tmp_path):
    path = write(
        tmp_path,
        "m.yaml",
        "user_needs: [{id: UN-1, title: A}]\n---\nkind: requirement\nid: REQ-1\ntitle: B\nsatisfies: [UN-1]\n",
    )
    assert "REQ-1" in load_model(path).requirements


def test_parse_errors(tmp_path):
    path = write(
        tmp_path,
        "bad.yaml",
        """
        user_needs:
          - id: UN-1
          - title: no id
          - just a string
        requirements: {not: a list}
        risks:
          - id: RISK-1
            title: r
            notes: nope
          - id: RISK-2
            title: r2
            notes: [{kind: gap}, {text: t, kind: weird, status: odd}]
        mitigations:
          - id: MIT-1
            title: m
            verified_by: 3
        mystery: 1
        """,
    )
    m, warnings = read_model(path)
    text = "\n".join(m.parse_errors)
    assert "UN-1: missing required field 'title'" in text
    assert "<no id>: missing required field 'id'" in text
    assert "entries must be mappings" in text
    assert "'requirements' must be a list" in text
    assert "notes must be a list" in text
    assert "notes[0] needs a 'text'" in text
    assert "notes[1].kind must be one of" in text
    assert "notes[1].status must be one of" in text
    unknown = "\n".join(m.unknown_fields)
    assert "unknown field 'verified_by'" in unknown
    assert "unknown top-level key 'mystery'" in unknown
    assert warnings == []


def test_unknown_fields_as_warnings(tmp_path):
    path = write(
        tmp_path,
        "m.yaml",
        "config: {rules: {unknown-field: warning}}\nuser_needs: [{id: UN-1, title: A, colour: red}]\n",
    )
    m, warnings = read_model(path)
    assert not m.parse_errors and warnings == []
    from rules_requirements.validate import validate

    issues = [i for i in validate(m) if i.code == "unknown-field"]
    assert [i.severity for i in issues] == ["warning"] and "colour" in issues[0].message
    assert [i.severity for i in validate(m, strict=True) if i.code == "unknown-field"] == ["error"]


def test_duplicate_keys_are_errors(tmp_path):
    path = write(
        tmp_path,
        "dup.yaml",
        "requirements: [{id: REQ-1, title: a}]\nrequirements: [{id: REQ-2, title: b}]\n",
    )
    m, _ = read_model(path)
    assert any("duplicate key 'requirements'" in e for e in m.parse_errors)
    path = write(
        tmp_path, "dup2.yaml", "requirements:\n  - id: REQ-1\n    title: a\n    method: hil\n    method: simulation\n"
    )
    m, _ = read_model(path)
    assert any("duplicate key 'method'" in e for e in m.parse_errors)


def test_braces_and_groups_in_id_pattern(tmp_path):
    path = write(
        tmp_path,
        "m.yaml",
        r"""
        config: {id_pattern: '{prefix}-\d{4}(\.\d+)?'}
        user_needs: [{id: UN-0001, title: A}]
        requirements: [{id: REQ-0001.2, title: R, satisfies: [UN-0001]}, {id: REQ-1, title: bad, satisfies: [UN-0001]}]
        """,
    )
    m = read_model(path)[0]
    from rules_requirements.annotations import extract
    from rules_requirements.validate import validate

    assert [i.entity for i in validate(m) if i.code == "bad-id"] == ["REQ-1"]
    refs = extract("# @rr(REQ-0001.2, UN-0001)", "a.py", m.config)
    assert refs[0].ids == ("REQ-0001.2", "UN-0001")


def test_malformed_config_sections_do_not_crash(tmp_path):
    path = write(
        tmp_path, "m.yaml", "config: {prefixes: [REQ], rules: 3, levels: [[1]]}\nuser_needs: [{id: UN-1, title: A}]\n"
    )
    m, _ = read_model(path)
    text = "\n".join(m.parse_errors)
    assert "config.prefixes: must be a mapping" in text and "config.rules: must be a mapping" in text
    assert "must be a name or a mapping" in text


def test_duplicates_and_bad_documents(tmp_path):
    a = write(tmp_path, "a.yaml", "user_needs: [{id: UN-1, title: A}]\nconfig: {}\n")
    b = write(tmp_path, "b.yaml", "user_needs: [{id: UN-1, title: B}]\nconfig: {}\n")
    c = write(tmp_path, "c.yaml", "- a list\n")
    d = write(tmp_path, "d.yaml", "kind: widget\nid: W-1\n")
    e = write(tmp_path, "e.yaml", "key: [unclosed\n")
    m, _ = read_model([a, b, c, d, e, str(tmp_path / "missing.yaml")])
    text = "\n".join(m.parse_errors)
    assert "duplicate id UN-1" in text
    assert "'config' is defined more than once" in text
    assert "top-level document must be a mapping" in text
    assert "unknown kind 'widget'" in text
    assert "cannot load" in text


def test_notes_and_verified_by(tmp_path):
    path = write(
        tmp_path,
        "m.yaml",
        """
        user_needs: [{id: UN-1, title: A, notes: ["plain note", {text: gap here, kind: gap, author: bot, created: 2026-01-01}]}]
        requirements:
          - id: REQ-1
            title: R
            satisfies: UN-1, UN-1
            tags: a, b
            verified_by: ["//pkg:t", {target: "//pkg:hw", level: hil}]
        """,
    )
    m = load_model(path)
    un = m.user_needs["UN-1"]
    assert [n.kind for n in un.notes] == ["comment", "gap"]
    assert un.notes[0].id == "n1" and un.notes[1].author == "bot"
    req = m.requirements["REQ-1"]
    assert req.tags == ("a", "b")
    assert [(v.target, v.level) for v in req.verified_by] == [("//pkg:t", ""), ("//pkg:hw", "hil")]


def test_with_and_without_entity(model):
    from dataclasses import replace

    changed = model.with_entity(replace(model.requirements["REQ-1"], title="New"))
    assert changed.requirements["REQ-1"].title == "New"
    assert model.requirements["REQ-1"].title == "Heat below setpoint"
    removed = changed.without_entity("REQ-1")
    assert "REQ-1" not in removed.requirements
    assert removed.without_entity("NOPE") is removed


def test_load_model_raises_with_all_errors(tmp_path):
    path = write(tmp_path, "m.yaml", MODEL.replace("satisfies: [UN-2]", "satisfies: [UN-9]"))
    with pytest.raises(ValidationError) as exc:
        load_model(path)
    assert any("UN-9" in e for e in exc.value.errors)
    assert "requirements model is invalid" in str(exc.value)
