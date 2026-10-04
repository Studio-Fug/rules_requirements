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
        test_methods:
          - id: TM-1
            title: t
            verified_by: [//a:b]
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
    assert "MIT-1: verified_by items must be a label or a mapping with a 'target'" in text
    unknown = "\n".join(m.unknown_fields)
    assert "TM-1: unknown field 'verified_by'" in unknown  # test methods and risks claim no cases
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
    # on one line, in flow style
    path = write(tmp_path, "dup3.yaml", "requirements: [{id: REQ-1, title: a, method: hil, method: simulation}]\n")
    assert any("duplicate key 'method'" in e for e in read_model(path)[0].parse_errors)
    # a merge key may legitimately be overridden
    path = write(
        tmp_path,
        "merge.yaml",
        "hw: &hw {method: sil}\nuser_needs: [{id: UN-1, title: n}]\nrequirements:\n"
        "  - <<: *hw\n    id: REQ-1\n    title: a\n    satisfies: [UN-1]\n    method: hitl\n",
    )
    m, _ = read_model(path)
    assert not [e for e in m.parse_errors if "duplicate" in e]
    assert m.requirements["REQ-1"].method == "hitl"


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


def test_named_groups_in_id_pattern_are_rejected(tmp_path):
    path = write(
        tmp_path, "m.yaml", "config: {id_pattern: '{prefix}-(?P<num>\\d+)'}\nuser_needs: [{id: UN-1, title: A}]\n"
    )
    m, _ = read_model(path)
    assert any("named groups" in e for e in m.parse_errors)
    m.config.any_id_regex()  # the fallback pattern still works


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


# --- claims: verified_by / validated_by items (one owner per test case) -----------

CLAIMS = """
config: {main_repo: splanc}
user_needs:
  - id: UN-5
    title: Usable
    validated_by: [{target: "record:usability_study", cases: ["*"]}]
requirements:
  - id: REQ-1
    title: R
    satisfies: [UN-5]
    verified_by:
      - //pkg:t
      - {target: "@splanc//pkg", level: hil}
      - target: "@@//web:clocksync_test"
        level: sil
        cases: ["clocksync::offset*", "clocksync::bestSample keeps the min-RTT sample"]
      - {target: //req:model_test, whole: true, reason: rr validate runs as one test}
mitigations:
  - id: MIT-4
    title: M
    implemented_by: [REQ-1]
    verified_by: [{target: //pi:bench, cases: ["bench::cutoff"]}]
"""


def test_claim_item_shapes(tmp_path):
    m, _ = read_model(write(tmp_path, "m.yaml", CLAIMS))
    assert m.parse_errors == () and m.unknown_fields == ()
    legacy, legacy_level, cases, whole = m.requirements["REQ-1"].verified_by
    assert (legacy.target, legacy.whole, legacy.legacy, legacy.cases) == ("//pkg:t", True, True, ())
    # targets are normalized; the spelling is kept for rewrites
    assert (legacy_level.target, legacy_level.spelling, legacy_level.level) == ("//pkg:pkg", "@splanc//pkg", "hil")
    assert legacy_level.legacy and legacy_level.whole
    assert cases.target == "//web:clocksync_test" and cases.label == "@@//web:clocksync_test"
    assert cases.cases == ("clocksync::offset*", "clocksync::bestSample keeps the min-RTT sample")
    assert not cases.whole and not cases.legacy and cases.level == "sil"
    assert whole.whole and not whole.legacy and whole.reason == "rr validate runs as one test" and whole.cases == ()
    assert cases.location.path.endswith("m.yaml") and cases.location.line == 13
    assert m.user_needs["UN-5"].validated_by[0].target == "record:usability_study"
    assert m.mitigations["MIT-4"].verified_by[0].cases == ("bench::cutoff",)


def test_claims_cover_every_verifiable_kind_in_a_stable_order(tmp_path):
    m, _ = read_model(write(tmp_path, "m.yaml", CLAIMS))
    got = [(c.entity, c.kind, c.relation, c.target, c.pattern, c.literal, c.index) for c in m.claims()]
    assert got == [
        ("UN-5", "user_need", "validated_by", "record:usability_study", "*", False, 0),
        ("REQ-1", "requirement", "verified_by", "//pkg:t", None, False, 0),
        ("REQ-1", "requirement", "verified_by", "//pkg:pkg", None, False, 1),
        ("REQ-1", "requirement", "verified_by", "//web:clocksync_test", "clocksync::offset*", False, 2),
        (
            "REQ-1",
            "requirement",
            "verified_by",
            "//web:clocksync_test",
            "clocksync::bestSample keeps the min-RTT sample",
            True,
            2,
        ),
        ("REQ-1", "requirement", "verified_by", "//req:model_test", None, False, 3),
        ("MIT-4", "mitigation", "verified_by", "//pi:bench", "bench::cutoff", True, 0),
    ]
    claims = m.claims()
    assert claims[1].legacy and not claims[5].legacy
    assert claims[3].level == "sil" and claims[3].location.line == 13
    # a whole claim selects every path; a selector never selects the synthetic result
    assert claims[5].matches("[target]") and claims[5].matches("anything")
    assert claims[3].matches("clocksync::offset") and not claims[3].matches("[target]")
    assert not m.claims()[0].matches("[target]")
    assert m.is_verifiable("UN-5") and m.is_verifiable("MIT-4") and not m.is_verifiable("RISK-1")


@pytest.mark.parametrize(
    "item, problem",
    [
        ("{target: //a:b, cases: [x], whole: true}", "either cases or whole"),
        ("{target: //a:b, cases: [x], whole: false}", "either cases or whole"),
        ("{target: //a:b, cases: []}", "cases is empty"),
        ("{target: //a:b, cases: x}", "must be a list"),
        ("{target: //a:b, cases: [1]}", "must be a list"),
        ("{target: //a:b, whole: false}", "whole must be true"),
        ("{target: //a:b, reason: why}", "reason belongs to a whole"),
    ],
)
def test_malformed_items_claim_the_whole_target(tmp_path, item, problem):
    m, _ = read_model(write(tmp_path, "m.yaml", f"requirements: [{{id: REQ-1, title: r, verified_by: [{item}]}}]\n"))
    (vb,) = m.requirements["REQ-1"].verified_by
    assert problem in vb.problem
    assert vb.selectors == (None,)
    (claim,) = m.claims()
    assert claim.whole  # fail closed: it can only add conflicts, never hide one
    assert claim.matches("x") and claim.matches("anything else")


def test_a_complex_mapping_key_is_a_load_error_not_a_crash(tmp_path):
    m, _ = read_model(write(tmp_path, "m.yaml", "requirements:\n  - ? [id, x]\n    : REQ-1\n"))
    assert any("found unhashable key" in e for e in m.parse_errors)


def test_items_without_a_target_are_parse_errors(tmp_path):
    m, _ = read_model(
        write(tmp_path, "m.yaml", "user_needs: [{id: UN-1, title: u, validated_by: [{cases: [x]}, '', 3]}]\n")
    )
    assert len([e for e in m.parse_errors if "validated_by items must be a label" in e]) == 3


def test_risks_and_test_methods_cannot_claim(tmp_path):
    m, _ = read_model(
        write(
            tmp_path,
            "m.yaml",
            "risks: [{id: RISK-1, title: r, verified_by: [//a:b]}]\n"
            "test_methods: [{id: TM-1, title: t, level: hil, validated_by: [//a:b]}]\n",
        )
    )
    assert m.claims() == []
    unknown = "\n".join(m.unknown_fields)
    assert "RISK-1: unknown field 'verified_by'" in unknown and "TM-1: unknown field 'validated_by'" in unknown


def test_lock_path_is_relative_to_the_config_file(tmp_path):
    (tmp_path / "req").mkdir()
    write(tmp_path, "req/a.yaml", "requirements: [{id: REQ-1, title: r}]\n")
    write(tmp_path, "req/config.yaml", "config: {sets_lock: verification.rrlock}\n")
    m, _ = read_model(str(tmp_path / "req"), root=str(tmp_path))
    assert m.config_file == "req/config.yaml"
    assert m.lock_path(shown=True) == "req/verification.rrlock"
    assert m.lock_path() == str(tmp_path / "req" / "verification.rrlock")
    m, _ = read_model(str(tmp_path / "req"))
    assert m.lock_path() == str(tmp_path / "req" / "verification.rrlock")
