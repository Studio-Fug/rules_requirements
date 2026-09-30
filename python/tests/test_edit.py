# SPDX-License-Identifier: AGPL-3.0-or-later
import pytest
from conftest import MODEL

from rules_requirements import edit
from rules_requirements._vendor import yaml
from rules_requirements.model import parse_documents

COMMENTED = """# Header comment that must survive.
user_needs:
  - id: UN-1
    title: Need one
    # a comment inside the entity
    description: >-
      Folded prose that
      spans lines.

  - id: UN-2  # trailing comment
    title: Need two

# ---- section banner ----
requirements:
  - {id: REQ-1, title: Flow style, satisfies: [UN-1]}
  - id: REQ-2
    title: Block style
    satisfies: [UN-1, UN-2]
"""


def parse(text):
    model, _ = parse_documents([("x", d) for d in yaml.safe_load_all(text) if d])
    return model


def test_noop_update_is_byte_identical():
    m = parse(COMMENTED)
    text = COMMENTED
    for ent in m.entities():
        text = edit.update_entity(text, ent.id, edit.entity_to_dict(ent))
    assert text == COMMENTED


def test_field_level_update_keeps_everything_else():
    m = parse(COMMENTED)
    data = edit.entity_to_dict(m.get("UN-1"))
    data["title"] = "Need one, renamed"
    data["rationale"] = "because"
    out = edit.update_entity(COMMENTED, "UN-1", data)
    assert "    title: Need one, renamed\n    # a comment inside the entity\n" in out
    assert "      Folded prose that\n      spans lines.\n    rationale: because\n" in out
    assert out.startswith("# Header comment that must survive.")
    assert "# ---- section banner ----" in out
    assert parse(out).get("UN-1").rationale == "because"


def test_update_flow_item_and_removal():
    m = parse(COMMENTED)
    data = edit.entity_to_dict(m.get("REQ-1"))
    data["satisfies"] = ["UN-2"]
    out = edit.update_entity(COMMENTED, "REQ-1", data)
    assert parse(out).get("REQ-1").satisfies == ("UN-2",)
    data = edit.entity_to_dict(m.get("REQ-2"))
    del data["satisfies"]
    out = edit.update_entity(COMMENTED, "REQ-2", data)
    assert "Block style\n" in out and "UN-1, UN-2" not in out
    assert parse(out).get("REQ-2").satisfies == ()


def test_update_id_line_and_errors():
    out = edit.update_entity(COMMENTED, "UN-2", {"id": "UN-2", "title": "Need two!"})
    assert '  - id: UN-2  # trailing comment\n    title: "Need two!"\n' in out
    with pytest.raises(KeyError):
        edit.update_entity(COMMENTED, "UN-9", {"id": "UN-9", "title": "x"})
    with pytest.raises(ValueError):
        edit.update_entity(COMMENTED, "UN-2", {"id": "UN-2"})  # title is required
    with pytest.raises(ValueError):
        edit.update_entity(COMMENTED, "UN-1", {"title": "no id"})  # would drop the first (id) line


def test_delete_and_insert():
    out = edit.delete_entity(COMMENTED, "UN-2")
    assert "- id: UN-2" not in out and "# ---- section banner ----" in out
    assert [e.id for e in parse(out).entities()] == ["UN-1", "REQ-1", "REQ-2"]
    out = edit.insert_entity(out, "user_need", {"id": "UN-3", "title": "Need three"})
    assert "\n\n  - id: UN-3\n    title: Need three\n" in out
    out = edit.insert_entity(out, "risk", {"id": "RISK-1", "title": "A risk", "severity": "high"})
    assert out.endswith("risks:\n  - id: RISK-1\n    title: A risk\n    severity: high\n")
    assert parse(out).get("RISK-1").severity == "high"
    with pytest.raises(KeyError):
        edit.delete_entity(out, "NOPE-1")
    assert (
        edit.insert_entity("", "user_need", {"id": "UN-1", "title": "t"}) == "user_needs:\n  - id: UN-1\n    title: t\n"
    )


def test_single_object_documents():
    text = "kind: requirement\nid: REQ-7\ntitle: One per file\n"
    span = edit.locate(text, "REQ-7")
    assert span is not None and not span.list_item and span.kind == "requirement"
    out = edit.update_entity(text, "REQ-7", {"id": "REQ-7", "title": "Changed", "satisfies": ["UN-1"]})
    assert out == "kind: requirement\nid: REQ-7\ntitle: Changed\nsatisfies: [UN-1]\n"
    assert edit.render_file("user_need", {"id": "UN-1", "title": "t"}) == "kind: user_need\nid: UN-1\ntitle: t\n"


def test_render_scalars_and_blocks():
    long = "word " * 40
    rendered = edit.render_entity(
        "requirement",
        {
            "id": "REQ-1",
            "title": "Needs: quoting",
            "description": long.strip(),
            "rationale": "line one\nline two",
            "verified_by": ["//pkg:test", {"target": "//pkg:hw", "level": "hil"}],
            "tags": ["yes", "2026-01-01"],
            "notes": [{"text": "note", "kind": "gap", "created": "2026-09-30"}],
        },
        indent=2,
    )
    assert rendered.startswith('  - id: REQ-1\n    title: "Needs: quoting"\n    description: >-\n')
    assert "    rationale: |-\n      line one\n      line two\n" in rendered
    assert "verified_by: [//pkg:test, {target: //pkg:hw, level: hil}]" in rendered
    assert 'tags: ["yes", "2026-01-01"]' in rendered
    assert '      - text: note\n        kind: gap\n        created: "2026-09-30"\n' in rendered
    back = parse("requirements:\n" + rendered).get("REQ-1")
    assert back.description == long.strip() and back.rationale == "line one\nline two"
    assert back.tags == ("yes", "2026-01-01")
    with pytest.raises(ValueError):
        edit.render_entity("requirement", {"id": "REQ-1", "title": "t", "colour": "red"})


def test_entity_dict_roundtrip_on_full_model():
    m = parse(MODEL)
    for ent in m.entities():
        back, problems = edit.dict_to_entity(ent.kind, edit.entity_to_dict(ent))
        assert not problems and edit.entity_to_dict(back) == edit.entity_to_dict(ent)
    assert edit.verified_by_from(["a", {"target": "b", "level": "hil"}])[1].level == "hil"
