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
    vbs = edit.verified_by_from(["//a", {"target": "//b", "level": "hil"}, {"target": "@x//c", "cases": ["m::*"]}])
    assert [(v.target, v.level, v.cases, v.legacy) for v in vbs] == [
        ("//a:a", "", (), True),
        ("//b:b", "hil", (), True),
        ("@x//c:c", "", ("m::*",), False),
    ]
    assert edit.verified_by_from([{"target": "//x:y", "cases": ["a"]}], main_repo="x")[0].target == "//x:y"
    with pytest.raises(edit.EditError):
        edit.verified_by_from([{"cases": ["a"]}])


# --- adversarial-review regressions ------------------------------------------------


def test_insert_into_empty_or_null_section():
    for body in ("requirements:\n", "requirements: []\n", "requirements: ~\n"):
        text = "user_needs:\n  - id: UN-1\n    title: n\n" + body
        out = edit.insert_entity(text, "requirement", {"id": "REQ-1", "title": "r", "satisfies": ["UN-1"]})
        edit.verify(text, out, {"REQ-1": {"id": "REQ-1", "title": "r", "satisfies": ["UN-1"]}})
        assert parse(out).get("REQ-1").satisfies == ("UN-1",)


def test_flow_containers_and_json_are_refused_not_rewritten():
    flow = "requirements: [{id: REQ-1, title: a}, {id: REQ-2, title: b}]\n"
    for fn in (
        lambda: edit.update_entity(flow, "REQ-2", {"id": "REQ-2", "title": "B"}),
        lambda: edit.delete_entity(flow, "REQ-2"),
    ):
        with pytest.raises(edit.EditError, match="flow style"):
            fn()
    with pytest.raises(edit.EditError):
        edit.insert_entity(flow, "requirement", {"id": "REQ-3", "title": "c"})
    doc = '{"kind": "requirement", "id": "REQ-1", "title": "a"}\n'
    with pytest.raises(edit.EditError, match="flow style"):
        edit.update_entity(doc, "REQ-1", {"id": "REQ-1", "title": "b"})
    # a flow mapping alone on its line inside a block list is fine
    ok = "requirements:\n  - {id: REQ-1, title: a}\n  - id: REQ-2\n    title: b\n"
    out = edit.update_entity(ok, "REQ-1", {"id": "REQ-1", "title": "A"})
    assert parse(out).get("REQ-1").title == "A" and parse(out).get("REQ-2").title == "b"


def test_merge_keys_never_leak_into_other_entities():
    text = "base: &b\n  owner: alice\nrequirements:\n  - id: REQ-1\n    <<: *b\n    title: one\n  - id: REQ-2\n    <<: *b\n    title: two\n"
    out = edit.update_entity(text, "REQ-2", {"id": "REQ-2", "title": "two", "owner": "bob"})
    m = parse(out)
    assert m.get("REQ-1").owner == "alice" and m.get("REQ-2").owner == "bob"
    anchored = "requirements:\n  - &r1\n    id: REQ-1\n    title: one\n    owner: alice\n  - <<: *r1\n    id: REQ-2\n    title: two\n"
    out = edit.update_entity(anchored, "REQ-1", {"id": "REQ-1", "title": "one", "owner": "bob"})
    with pytest.raises(edit.EditError, match="also change REQ-2"):
        edit.verify(anchored, out, {"REQ-1": {"id": "REQ-1", "title": "one", "owner": "bob"}})


@pytest.mark.parametrize(
    "value",
    [".NaN", "0b1010", "0x_1F", "1_000", "yes", "~", "null", "2026-09-30", "12:30", "a\x85b", "a b", "tab\there",
     "  leading", "trailing  ", "line1\n  indented", "ends with newline\n", "#hash", "- dash", ": colon", "@at", "`tick`",
     "quote's", 'dq"', "back\\slash", "ümlaut ✓", "x" * 200, "word " * 30 + "\ttab"],
)  # fmt: skip
def test_rendered_scalars_round_trip(value):
    # Text fields are stripped by the model loader, so that is the contract.
    for key in ("title", "description", "owner"):
        data = {"id": "REQ-1", "title": "t", key: value}
        rendered = edit.render_entity("requirement", data, indent=2)
        back = parse("requirements:\n" + rendered).get("REQ-1")
        assert getattr(back, key) == value.strip(), (key, rendered)


def test_other_key_indentations_and_missing_trailing_newline():
    odd = "requirements:\n-   id: REQ-1\n    title: one\n-   id: REQ-2\n    title: two"  # no final newline
    out = edit.update_entity(odd, "REQ-2", {"id": "REQ-2", "title": "two", "owner": "x"})
    assert parse(out).get("REQ-2").owner == "x"
    out = edit.insert_entity(odd, "requirement", {"id": "REQ-3", "title": "three"})
    assert out.endswith("-   id: REQ-3\n    title: three\n") and sorted(parse(out).ids()) == ["REQ-1", "REQ-2", "REQ-3"]


def test_verify_catches_collateral_changes_and_tolerates_line_shifts():
    text = "requirements:\n  - id: REQ-1\n    title: one\n  - id: REQ-2\n    title: two\n    colour: red\n"
    grown = edit.update_entity(text, "REQ-1", {"id": "REQ-1", "title": "one", "description": "a\nb\nc"})
    edit.verify(
        text, grown, {"REQ-1": {"id": "REQ-1", "title": "one", "description": "a\nb\nc"}}
    )  # unknown field moved lines: fine
    with pytest.raises(edit.EditError, match="also change REQ-2"):
        edit.verify(text, text.replace("two", "TWO"), {})
    with pytest.raises(edit.EditError, match="introduce model errors"):
        edit.verify(text, text + "  - id: REQ-1\n    title: dup\n", {})
    with pytest.raises(edit.EditError, match=r"change REQ-1 .flavour."):
        edit.verify(text, text.replace("title: one", "title: one\n    flavour: x"), {})
    with pytest.raises(edit.EditError, match="not be valid YAML"):
        edit.verify(text, "requirements: [\n", {})


# --- second review round ------------------------------------------------------------


def unknown(text):
    return sorted(parse(text).unknown_fields)


def test_whole_entity_rewrites_never_lose_custom_fields_or_comments():
    flow = "requirements:\n  - {id: REQ-1, title: one, jira: ABC-1}  # from the 2024 audit\n"
    with pytest.raises(edit.EditError, match="jira"):
        edit.update_entity(flow, "REQ-1", {"id": "REQ-1", "title": "two"})
    commented = "requirements:\n  - {id: REQ-1,  # first\n     title: one}\n"
    with pytest.raises(edit.EditError, match="comments"):
        edit.update_entity(commented, "REQ-1", {"id": "REQ-1", "title": "two"})
    # without them the flow item is rewritten, and a comment after it is kept
    plain = "requirements:\n  - {id: REQ-1, title: one}  # from the 2024 audit\n  - id: REQ-2\n    title: t\n"
    out = edit.update_entity(plain, "REQ-1", {"id": "REQ-1", "title": "two"})
    edit.verify(plain, out, {"REQ-1": {"id": "REQ-1", "title": "two"}})
    assert "# from the 2024 audit" in out and parse(out).get("REQ-1").title == "two"
    merged = "base: &b\n  owner: alice\nrequirements:\n  - <<: *b\n    id: REQ-1\n    title: one\n    jira: ABC-1\n"
    with pytest.raises(edit.EditError, match="merge keys"):
        edit.update_entity(merged, "REQ-1", {"id": "REQ-1", "title": "two", "owner": "alice"})


def test_removing_the_first_key_keeps_everything_else():
    text = (
        "config: {rules: {unknown-field: warning}}\n"
        "requirements:\n"
        "  - owner: alice  # set by the audit\n"
        "    # about the id\n"
        "    id: REQ-1\n"
        "    jira: ABC-1\n"
        "    title: one\n"
    )
    out = edit.update_entity(text, "REQ-1", {"id": "REQ-1", "title": "one"})
    edit.verify(text, out, {"REQ-1": {"id": "REQ-1", "title": "one", "kind": "requirement"}})
    assert "    # about the id\n  - id: REQ-1\n    jira: ABC-1\n" in out
    assert parse(out).get("REQ-1").owner == "" and unknown(out) == unknown(text)
    anchored = "requirements:\n  - &r1\n    owner: x\n    id: REQ-1\n    title: one\n  - <<: *r1\n    id: REQ-2\n    title: two\n"
    out = edit.update_entity(anchored, "REQ-1", {"id": "REQ-1", "title": "uno", "owner": "x"})
    assert out.count("&r1") == 1 and parse(out).get("REQ-1").title == "uno"


def test_custom_keys_in_notes_and_verified_by_items_survive():
    text = (
        "config: {rules: {unknown-field: warning}}\n"
        "user_needs: [{id: UN-1, title: n}]\n"
        "requirements:\n"
        "  - id: REQ-1\n"
        "    title: one\n"
        "    satisfies: [UN-1]\n"
        "    verified_by:\n"
        "      - {target: '//t:a', level: sil, ticket: QA-7}\n"
        "    notes:\n"
        "      - {id: n1, text: first, priority: high, link: 'https://x/1'}\n"
    )
    assert any("notes[0]: unknown field 'priority'" in u for u in unknown(text))
    assert any("verified_by[0]: unknown field 'ticket'" in u for u in unknown(text))
    data = edit.entity_to_dict(parse(text).get("REQ-1"))
    data["notes"].append({"id": "n2", "text": "second"})
    data["verified_by"].append("//t:b")
    out = edit.update_entity(text, "REQ-1", data)
    edit.verify(text, out, {"REQ-1": data})
    req = parse(out).get("REQ-1")
    assert dict(req.notes[0].extra) == {"priority": "high", "link": "https://x/1"} and req.notes[1].text == "second"
    assert dict(req.verified_by[0].extra) == {"ticket": "QA-7"} and req.verified_by[1].target == "//t:b"


def test_verify_sees_lost_custom_fields_comments_and_wrong_kinds():
    text = (
        "config: {rules: {unknown-field: warning}}\nrequirements:\n  - id: REQ-1\n    title: one  # keep\n    jira: A\n"
    )
    same = {"REQ-1": {"id": "REQ-1", "title": "one"}}
    with pytest.raises(edit.EditError, match=r"change REQ-1 .jira."):
        edit.verify(text, text.replace("    jira: A\n", ""), same)
    with pytest.raises(edit.EditError, match="comment"):
        edit.verify(text, text.replace("  # keep", ""), same)
    # a changed field may drop its own comment
    edit.verify(text, text.replace("one  # keep", "uno"), {"REQ-1": {"id": "REQ-1", "title": "uno"}})
    moved = text.replace("requirements:", "user_needs:")
    with pytest.raises(edit.EditError, match="user_need, not a requirement"):
        edit.verify(text, moved, {"REQ-1": {"id": "REQ-1", "title": "one", "kind": "requirement"}})


def test_insert_goes_to_its_own_section_despite_duplicate_ids():
    text = (
        "user_needs:\n  - id: X-1\n    title: need\nrequirements:\n  - id: X-1\n    title: req\n    satisfies: [X-1]\n"
    )
    out = edit.insert_entity(text, "requirement", {"id": "REQ-2", "title": "two", "satisfies": ["X-1"]})
    edit.verify(text, out, {"REQ-2": {"id": "REQ-2", "title": "two", "satisfies": ["X-1"], "kind": "requirement"}})
    assert out.endswith("    satisfies: [X-1]\n  - id: REQ-2\n    title: two\n    satisfies: [X-1]\n")
    # the duplicate's "(first defined at …)" line number shifts: still accepted
    shifted = edit.update_entity(text, "X-1", {"id": "X-1", "title": "need", "description": "one\ntwo\nthree"})
    edit.verify(text, shifted, {"X-1": {"id": "X-1", "title": "need", "description": "one\ntwo\nthree"}})


def test_insert_after_an_anchored_item_and_into_commented_empty_sections():
    anchored = "requirements:\n  - &r1 id: REQ-1\n    title: one\n"
    out = edit.insert_entity(anchored, "requirement", {"id": "REQ-2", "title": "two"})
    assert out.count("&r1") == 1 and parse(out).get("REQ-2").title == "two"
    for empty in ("requirements: ~  # none yet\n", "requirements: []  # none yet\n"):
        out = edit.insert_entity(empty, "requirement", {"id": "REQ-1", "title": "one"})
        edit.verify(empty, out, {"REQ-1": {"id": "REQ-1", "title": "one"}})
        assert "# none yet" in out


def test_deleting_a_document_keeps_the_others():
    text = "project: {name: P}\n---\nkind: requirement\nid: REQ-1\ntitle: one\n"
    out = edit.delete_entity(text, "REQ-1")
    edit.verify(text, out, {"REQ-1": None})
    assert parse(out).project["name"] == "P" and not edit.is_blank(out)
    alone = "# SPDX header\nkind: requirement\nid: REQ-1\ntitle: one\n"
    assert edit.is_blank(edit.delete_entity(alone, "REQ-1"))


# --- third review round -------------------------------------------------------------


def test_comments_on_block_scalar_headers_count():
    text = (
        "base: &b\n  owner: qa\n"
        "requirements:\n"
        "  - <<: *b\n    id: REQ-1\n    title: one\n"
        "    description: >-  # wording agreed with QA, do not change\n      Folded text.\n"
        "  - id: REQ-2\n    title: two\n"
        "    rationale: |  # keep\n      Literal text.\n"
    )
    assert [c for _, c in edit._comments(text)] == ["# wording agreed with QA, do not change", "# keep"]
    with pytest.raises(edit.EditError, match="comments"):
        edit.update_entity(text, "REQ-1", {"id": "REQ-1", "title": "uno", "owner": "qa", "description": "Folded text."})
    with pytest.raises(edit.EditError, match="# keep"):
        edit.verify(text, text.replace("|  # keep", "|"), {})
    # changing the field itself may drop its header comment; other edits keep it
    two = {"id": "REQ-2", "title": "two", "rationale": "Literal text."}
    out = edit.update_entity(text, "REQ-2", {**two, "title": "TWO"})
    edit.verify(text, out, {"REQ-2": {**two, "title": "TWO"}})
    assert "|  # keep" in out
    out = edit.update_entity(text, "REQ-2", {**two, "rationale": "New."})
    edit.verify(text, out, {"REQ-2": {**two, "rationale": "New."}})


def test_verify_compares_the_values_of_keys_the_model_does_not_define():
    text = (
        "schema_version: 2\n"
        "config: {rules: {unknown-field: warning}}\n"
        "requirements:\n  - id: REQ-1\n    title: one\n    jira: ABC-1\n"
    )
    for old, new in (("ABC-1", "EVIL-9"), ("schema_version: 2", "schema_version: 3"), ("warning", "off")):
        with pytest.raises(edit.EditError, match="refusing"):
            edit.verify(text, text.replace(old, new), {})
    # a rename keeps them under the new id
    renamed = text.replace("id: REQ-1", "id: REQ-10")
    edit.verify(text, renamed, {"REQ-1": None, "REQ-10": {"id": "REQ-10", "title": "one"}}, aliases={"REQ-10": "REQ-1"})


# --- fourth review round ------------------------------------------------------------


def alias_chain(levels):
    lines = ["x-chain:", "  a0: &a0 [1, 1, 1, 1, 1, 1, 1, 1, 1]"]
    for i in range(1, levels + 1):
        lines.append(f"  a{i}: &a{i} [{', '.join([f'*a{i - 1}'] * 9)}]")
    return "\n".join(lines) + "\nrequirements:\n  - id: REQ-1\n    title: one  # keep\n"


def test_aliases_cannot_blow_up_the_edit_checks():
    import time

    from rules_requirements.model import load_text

    bomb = alias_chain(8)  # 9**9 values once expanded
    with pytest.raises(yaml.YAMLError, match="expands to over"):
        load_text(bomb)
    start = time.monotonic()
    assert [c for _, c in edit._comments(bomb)] == ["# keep"]
    assert len(edit._extras(bomb, {})) == 1
    assert time.monotonic() - start < 2
    # under the limit, edits work as usual
    text = alias_chain(4)
    out = edit.update_entity(text, "REQ-1", {"id": "REQ-1", "title": "uno"})
    edit.verify(text, out, {"REQ-1": {"id": "REQ-1", "title": "uno"}})


def test_append_document():
    text = "kind: user_need\nid: UN-1\ntitle: one"
    out = edit.append_document(text, "user_need", {"id": "UN-2", "title": "two"})
    edit.verify(text, out, {"UN-2": {"id": "UN-2", "title": "two", "kind": "user_need"}})
    assert out == "kind: user_need\nid: UN-1\ntitle: one\n---\nkind: user_need\nid: UN-2\ntitle: two\n"


# --- claims: verified_by / validated_by items round-trip ---------------------------

CLAIMS = """config: {main_repo: splanc}
user_needs:
  - id: UN-5
    title: Usable
    validated_by: [{target: "record:usability_study", cases: ["*"]}]
requirements:
  - id: REQ-1
    title: R
    satisfies: [UN-5]
    verified_by:
      - //pkg:t  # legacy
      - {target: "@splanc//pkg", level: hil}
      - target: "@@//web:clocksync_test"
        cases:
          - "clocksync::bestSample keeps the min-RTT sample"
          - clocksync::offset*
      - {target: //req:model_test, whole: true, reason: rr validate runs as one test}
mitigations:
  - id: MIT-4
    title: M
    mitigates: [RISK-1]
    implemented_by: [REQ-1]
    verified_by: [{target: //pi:bench, cases: ["bench::cutoff"], level: hil}]
"""


def test_claim_items_round_trip_as_plain_data():
    m = parse(CLAIMS)
    req = edit.entity_to_dict(m.get("REQ-1"))
    assert req["verified_by"] == [
        "//pkg:t",
        {"target": "@splanc//pkg", "level": "hil"},  # the spelling, not the normalized label
        {
            "target": "@@//web:clocksync_test",
            "cases": ["clocksync::bestSample keeps the min-RTT sample", "clocksync::offset*"],
        },
        {"target": "//req:model_test", "whole": True, "reason": "rr validate runs as one test"},
    ]
    assert edit.entity_to_dict(m.get("UN-5"))["validated_by"] == [{"target": "record:usability_study", "cases": ["*"]}]
    assert edit.entity_to_dict(m.get("MIT-4"))["verified_by"] == [
        {"target": "//pi:bench", "cases": ["bench::cutoff"], "level": "hil"}
    ]
    for ent in m.entities():
        back, problems = edit.dict_to_entity(ent.kind, edit.entity_to_dict(ent))
        assert not problems and edit.entity_to_dict(back) == edit.entity_to_dict(ent)


def test_unrelated_edits_keep_claim_text_byte_for_byte():
    data = edit.entity_to_dict(parse(CLAIMS).get("REQ-1"))
    data["title"] = "R, renamed"
    out = edit.update_entity(CLAIMS, "REQ-1", data)
    edit.verify(CLAIMS, out, {"REQ-1": data})
    assert out == CLAIMS.replace("    title: R\n", "    title: R, renamed\n")


def test_adding_selectors_renders_readable_blocks():
    m = parse(CLAIMS)
    data = edit.entity_to_dict(m.get("REQ-1"))
    long_case = "improv_provision::provisionViaBle: survives Android's first-attempt GATT flake via retry"
    data["verified_by"].append({"target": "//web:improv_provision_test", "cases": [long_case, "x\\*y"]})
    out = edit.update_entity(CLAIMS, "REQ-1", data)
    edit.verify(CLAIMS, out, {"REQ-1": data})
    assert "      - target: //web:improv_provision_test\n        cases:\n" in out
    assert '          - "' + long_case + '"\n' in out
    vb = parse(out).get("REQ-1").verified_by[-1]
    assert vb.cases == (long_case, "x\\*y") and not vb.legacy
    assert [v.label for v in parse(out).get("REQ-1").verified_by[:3]] == [
        "//pkg:t",
        "@splanc//pkg",
        "@@//web:clocksync_test",
    ]


def test_claims_on_needs_and_mitigations_can_be_inserted_and_edited():
    text = "user_needs:\n  - id: UN-1\n    title: n\nmitigations:\n  - id: MIT-1\n    title: m\n"
    un = {"id": "UN-1", "title": "n", "validated_by": [{"target": "record:study", "cases": ["*"]}]}
    out = edit.update_entity(text, "UN-1", un)
    edit.verify(text, out, {"UN-1": un})
    mit = {"id": "MIT-2", "title": "m2", "verified_by": [{"target": "//a:b", "whole": True, "reason": "one binary"}]}
    out2 = edit.insert_entity(out, "mitigation", mit)
    edit.verify(out, out2, {"MIT-2": {**mit, "kind": "mitigation"}})
    m = parse(out2)
    assert m.get("UN-1").validated_by[0].cases == ("*",)
    assert m.get("MIT-2").verified_by[0].whole and m.get("MIT-2").verified_by[0].reason == "one binary"


@pytest.mark.parametrize(
    "item",
    [
        {"target": "//a:b", "cases": ["x"], "whole": True},
        {"target": "//a:b", "cases": []},
        {"target": "//a:b", "whole": False},
        {"target": "//a:b", "reason": "r"},
    ],
)
def test_malformed_claim_items_are_never_written(item):
    data = {"id": "REQ-1", "title": "t", "verified_by": [item]}
    _, problems = edit.dict_to_entity("requirement", data)
    assert problems
    with pytest.raises(edit.EditError):
        edit.normalize("requirement", data)
    with pytest.raises(edit.EditError):
        edit.insert_entity("requirements: []\n", "requirement", data)


@pytest.mark.parametrize(
    "selectors",
    [
        ["returns x, y when z"],
        ["a,b"],
        ["null,", "a"],
        ["a[", "b]"],
        ["{x}", "x{"],
        ["a #b", "#c"],
        ["test_x[exc0-False]", "x, " * 30],
    ],
)
def test_selectors_with_flow_indicators_are_written_as_one_selector(selectors):
    data = {"id": "PR-1", "title": "t", "verified_by": [{"target": "//a:b", "cases": selectors}]}
    out = edit.insert_entity("requirements: []\n", "requirement", data)
    edit.verify("requirements: []\n", out, {"PR-1": {**data, "kind": "requirement"}})
    assert parse(out).get("PR-1").verified_by[0].cases == tuple(selectors)
    # and again as one item among several, where each item gets its own line
    data["verified_by"].append({"target": "//c:d", "cases": ["y, " * 20]})
    out = edit.update_entity(out, "PR-1", data)
    assert [vb.cases for vb in parse(out).get("PR-1").verified_by] == [tuple(selectors), ("y, " * 20,)]


def test_claims_that_render_no_other_way_are_double_quoted_blocks(monkeypatch):
    long_case = "a, b " * 20
    items = [{"target": "//a:b", "cases": [long_case], "level": "hil"}, "//c:d"]
    monkeypatch.setattr(edit, "_claim_lines", lambda key, items, pad: [f"{pad}{key}: [broken"])
    lines = edit._claim_block("verified_by", items, "")
    assert lines == [
        "verified_by:",
        '  - "target": "//a:b"',
        '    "cases":',
        f'      - "{long_case}"',
        '    "level": "hil"',
        '  - "//c:d"',
    ]
    monkeypatch.setattr(edit, "_quoted_block", lambda key, items, pad: [f"{pad}{key}: [broken"])
    with pytest.raises(edit.EditError):
        edit._claim_block("verified_by", items, "")
