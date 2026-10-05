# SPDX-License-Identifier: AGPL-3.0-or-later
"""``rr migrate apply --stage model``: explicit selectors for every current
owner; the owner table stays unchanged under ``attribution: model``."""

import json

from conftest import write

from rules_requirements import cli, migrate
from rules_requirements.attribution import attribute
from rules_requirements.ingest import Evidence, TestCase
from rules_requirements.model import read_model
from rules_requirements.validate import validate

MODEL = """\
# The model before --stage model (attribution: hybrid by default).
user_needs: [{id: UN-1, title: n}]
requirements:
  - id: REQ-1
    title: a
    satisfies: [UN-1]
  - id: REQ-2
    title: b  # keeps its comment
    satisfies: [UN-1]
    verified_by:
      - target: //p:t
        cases: ["m::b1"]
  - id: REQ-3
    title: c
    satisfies: [UN-1]
"""


def case(path, status="passed", declared=(), target="//p:t", **kw):
    classname, _, name = path.rpartition("::")
    return TestCase(name, status, classname=classname, declared=declared, target=target, **kw)


def evidence():
    ev = Evidence()
    for c in (
        case("a::1", declared=("REQ-1",)),
        case("a::2", declared=("REQ-1",)),
        case("s::run", declared=("REQ-1",)),
        case("s::skip", "skipped", declared=("REQ-1",)),
        case("m::b1"),
        case("m::b2", declared=("REQ-2",)),
        case("m::free"),
        TestCase("syn", "passed", declared=("REQ-3",), target="//q:syn", properties={"rr.synthetic": "true"}),
    ):
        ev.add(c)
    return ev


def load(path):
    model, _ = read_model(str(path))
    return model


def owner_table(model, ev):
    return dict(attribute(model, ev).owner)


def test_model_stage_writes_a_selector_for_every_tag_owner(tmp_path):
    model = load(write(tmp_path, "m.yaml", MODEL))
    ev = evidence()
    stage = migrate.model_stage(model, ev)
    assert not stage.refused
    assert stage.additions == {"REQ-1": {"//p:t": ["a::1", "a::2", "s::run", "s::skip"]}, "REQ-2": {"//p:t": ["m::b2"]}}
    assert stage.whole == {"REQ-3": ["//q:syn"]}
    # REQ-2's existing item gains the case; REQ-3 claims its synthetic-only target whole.
    assert stage.data["REQ-2"]["verified_by"] == [{"target": "//p:t", "cases": ["m::b1", "m::b2"]}]
    assert stage.data["REQ-3"]["verified_by"] == [{"target": "//q:syn", "whole": True, "reason": migrate.WHOLE_REASON}]
    assert owner_table(stage.model, ev) == owner_table(model, ev)


def test_compress_globs_only_where_exact(tmp_path):
    model = load(write(tmp_path, "m.yaml", MODEL))
    stage = migrate.model_stage(model, evidence(), compress=True)
    assert not stage.refused
    # a::* selects exactly REQ-1's cases; s:: holds a skipped case and m:: an unowned one: literals.
    assert stage.additions["REQ-1"]["//p:t"] == ["a::*", "s::run", "s::skip"]
    assert stage.additions["REQ-2"]["//p:t"] == ["m::b2"]


def test_stage_model_cli_rewrites_the_model_and_keeps_the_owner_table(capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BUILD_WORKSPACE_DIRECTORY", raising=False)
    monkeypatch.delenv("BUILD_WORKING_DIRECTORY", raising=False)
    write(tmp_path, "req/requirements.yaml", MODEL)
    junit = (
        '<testsuites><testsuite name="s">'
        '<testcase classname="a" name="1"><properties><property name="requirement" value="REQ-1"/></properties></testcase>'
        '<testcase classname="a" name="2"><properties><property name="requirement" value="REQ-1"/></properties></testcase>'
        '<testcase classname="m" name="b1"/>'
        '<testcase classname="m" name="b2"><properties><property name="requirement" value="REQ-2"/></properties></testcase>'
        "</testsuite></testsuites>"
    )
    write(tmp_path, "bazel-testlogs/p/t/test.xml", junit)
    sheet = {
        "schema": migrate.SCHEMA,
        "inputs": {"model": ["req"], "evidence": ["bazel-testlogs"]},
        "groups": [{"target": "//p:t", "group": "a", "counts_toward": ["REQ-1", "REQ-2"], "owner": "REQ-1",
                    "cases": [{"path": "a::1"}]}],
    }  # fmt: skip
    write(tmp_path, "plan.rrplan", json.dumps(sheet))
    before, _ = read_model("req", root=str(tmp_path))
    from rules_requirements import ingest

    ev = ingest.collect(["bazel-testlogs"])
    owners = owner_table(before, ev)
    rc = cli.main(["migrate", "apply", "plan.rrplan", "--stage", "model", "--dry-run"])
    out, err = capsys.readouterr()
    assert rc == 0 and "+    verified_by: [{target: //p:t, cases: [a::1, a::2]}]" in out
    assert "over the evidence given, the owner table (4 case(s)) is unchanged" in err
    assert (tmp_path / "req/requirements.yaml").read_text() == MODEL  # a dry run writes nothing
    rc = cli.main(["migrate", "apply", "plan.rrplan", "--stage", "model", "--compress"])
    out, err = capsys.readouterr()
    assert rc == 0, err
    text = (tmp_path / "req/requirements.yaml").read_text()
    assert "# keeps its comment" in text and "a::*" in text
    after, _ = read_model("req", root=str(tmp_path))
    from dataclasses import replace

    assert owner_table(replace(after, config=replace(after.config, attribution="model")), ev) == owners
    assert not [i for i in validate(after) if i.severity == "error"]
    # Now in model mode the tags are only cross-checks: rr attribution --check passes.
    write(tmp_path, "req/requirements.yaml", "config: {attribution: model}\n" + text)
    rc = cli.main(["attribution", "--model", "req", "--evidence", "bazel-testlogs", "--check"])
    assert rc == 0, capsys.readouterr().err


def test_stage_model_refuses_a_quarantine_or_a_decision_the_evidence_does_not_show(capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "req/requirements.yaml", MODEL)
    sheet = {"schema": migrate.SCHEMA, "groups": [{"target": "//p:t", "group": "a", "counts_toward": ["REQ-1", "REQ-2"],
             "owner": "REQ-2", "cases": [{"path": "a::1"}]}]}  # fmt: skip
    write(tmp_path, "plan.rrplan", json.dumps(sheet))
    write(
        tmp_path,
        "bazel-testlogs/p/t/test.xml",
        '<testsuite name="s"><testcase classname="a" name="1"><properties>'
        '<property name="requirement" value="REQ-1"/></properties></testcase></testsuite>',
    )
    args = ["migrate", "apply", "plan.rrplan", "--stage", "model", "--model", "req", "--evidence", "bazel-testlogs"]
    rc = cli.main(args)
    err = capsys.readouterr().err
    assert rc == 1 and "the worksheet decided REQ-2, the evidence makes it owned by REQ-1" in err
    assert (tmp_path / "req/requirements.yaml").read_text() == MODEL
    write(
        tmp_path,
        "bazel-testlogs/p/t/test.xml",
        '<testsuite name="s"><testcase classname="a" name="1"><properties>'
        '<property name="requirement" value="REQ-1, REQ-2"/></properties></testcase></testsuite>',
    )
    sheet["groups"][0]["owner"] = "?"
    write(tmp_path, "plan.rrplan", json.dumps(sheet))
    rc = cli.main(args)
    err = capsys.readouterr().err
    assert rc == 1 and "multi-tag:" in err and "nothing written" in err


def test_check_model_stage_catches_a_changed_owner(tmp_path):
    model = load(write(tmp_path, "m.yaml", MODEL))
    broad = load(write(tmp_path, "b.yaml", MODEL.replace('cases: ["m::b1"]', 'cases: ["m::*"]')))
    problems = migrate.check_model_stage(model, broad, evidence())
    assert any("owner of //p:t#m::free would change: None -> REQ-2" in p for p in problems)


# --- regressions (B6 review) -------------------------------------------------


def _junit(classname, name, rid=None):
    prop = f'<properties><property name="requirement" value="{rid}"/></properties>' if rid else ""
    return f'<testsuite name="s"><testcase classname="{classname}" name="{name}">{prop}</testcase></testsuite>'


def test_stage_model_keeps_a_worksheet_owner_whose_case_is_in_another_lanes_evidence(capsys, tmp_path, monkeypatch):
    """The worksheet decided //h:t#h::1 -> REQ-2; only the software evidence
    is given. The decision is written as a literal selector of REQ-2 (it used
    to be warned about and dropped with exit 0), so over every lane's
    evidence REQ-2 still owns its case under attribution: model."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BUILD_WORKSPACE_DIRECTORY", raising=False)
    monkeypatch.delenv("BUILD_WORKING_DIRECTORY", raising=False)
    model = "user_needs: [{id: UN-1, title: n}]\nrequirements:\n  - {id: REQ-1, title: a, satisfies: [UN-1]}\n" \
            "  - {id: REQ-2, title: b, satisfies: [UN-1]}\n"  # fmt: skip
    write(tmp_path, "req/requirements.yaml", model)
    write(tmp_path, "sw/testlogs/p/t/test.xml", _junit("a", "1", "REQ-1"))
    write(tmp_path, "hitl/testlogs/h/t/test.xml", _junit("h", "1", "REQ-2"))
    sheet = {"schema": migrate.SCHEMA, "groups": [{"target": "//h:t", "group": "h", "counts_toward": ["REQ-2"],
             "owner": "REQ-2", "cases": [{"path": "h::1"}]}]}  # fmt: skip
    write(tmp_path, "plan.rrplan", json.dumps(sheet))
    rc = cli.main(
        ["migrate", "apply", "plan.rrplan", "--stage", "model", "--model", "req", "--evidence", "sw/testlogs"]
    )
    err = capsys.readouterr().err
    assert rc == 0, err
    assert "//h:t#h::1: decided REQ-2 on the worksheet, not in the evidence given: a literal selector of REQ-2" in err
    assert "1 decided case(s) the evidence lacks keep the worksheet's owner" in err
    text = (tmp_path / "req/requirements.yaml").read_text()
    write(tmp_path, "req/requirements.yaml", "config: {attribution: model}\n" + text)
    rc = cli.main(["attribution", "--model", "req", "--evidence", "sw/testlogs", "hitl/testlogs", "--format", "json",
                   "--check"])  # fmt: skip
    out, err = capsys.readouterr()
    assert rc == 0, err
    rows = {r["case"]: r["owner"] for r in json.loads(out)["cases"]}
    assert rows == {"//p:t#a::1": "REQ-1", "//h:t#h::1": "REQ-2"}


def test_stage_model_refuses_when_the_claims_would_give_an_absent_decided_case_another_owner(tmp_path):
    """REQ-1 already claims //h:t h::*; the worksheet gives the absent h::1 to
    REQ-2. Its literal would share the case: check_claims refuses it, and so
    does the static check of the absent decided cases."""
    text = MODEL.replace("  - id: REQ-1\n    title: a\n    satisfies: [UN-1]\n",
                         "  - id: REQ-1\n    title: a\n    satisfies: [UN-1]\n"
                         "    verified_by: [{target: //h:t, cases: ['h::*']}]\n")  # fmt: skip
    model = load(write(tmp_path, "m.yaml", text))
    sheet = {"groups": [{"target": "//h:t", "owner": "REQ-2", "cases": [{"path": "h::1"}]}]}
    stage = migrate.model_stage(model, evidence(), sheet)
    assert stage.from_worksheet == ["//h:t#h::1"]
    assert any(r.startswith("check_claims: ") and "shared-case" in r for r in stage.refused), stage.refused
    assert any("//h:t#h::1: the worksheet decided REQ-2 (not in the evidence given), but the new claims select it "
               "for REQ-1, REQ-2" in r for r in stage.refused)  # fmt: skip
    # Decided none, but REQ-1's claim selects it: refused too.
    sheet["groups"][0]["owner"] = "none"
    stage = migrate.model_stage(model, evidence(), sheet)
    assert any("decided none (not in the evidence given), but the new claims select it for REQ-1" in r
               for r in stage.refused)  # fmt: skip


def test_an_absent_case_decided_none_gets_no_selector(tmp_path):
    """//h:t#h::1 is decided none on the worksheet and is not in the evidence;
    no claim selects it. Nothing is written for it (no entity named "none",
    no literal), and the stage is not refused: nobody owns it, as decided."""
    model = load(write(tmp_path, "m.yaml", MODEL))
    sheet = {"groups": [{"target": "//h:t", "owner": "none", "cases": [{"path": "h::1"}]}]}
    stage = migrate.model_stage(model, evidence(), sheet)
    assert not stage.refused, stage.refused
    assert stage.unseen == {"//h:t#h::1": "none"}
    assert stage.from_worksheet == []
    assert "none" not in stage.additions and "none" not in stage.data
    assert not any("//h:t" in targets for targets in stage.additions.values())


def test_compress_keeps_its_globs_off_an_absent_case_decided_for_another_entity(tmp_path):
    model = load(write(tmp_path, "m.yaml", MODEL))
    sheet = {"groups": [{"target": "//p:t", "owner": "REQ-2", "cases": [{"path": "a::3"}]}]}
    stage = migrate.model_stage(model, evidence(), sheet, compress=True)
    assert not stage.refused, stage.refused
    assert stage.additions["REQ-1"]["//p:t"] == ["a::1", "a::2", "s::run", "s::skip"]  # no a::*: it reaches a::3
    assert stage.additions["REQ-2"]["//p:t"] == ["m::b2", "a::3"]
    sheet["groups"][0]["owner"] = "REQ-1"  # decided for REQ-1: the glob may cover it, no literal needed
    stage = migrate.model_stage(model, evidence(), sheet, compress=True)
    assert not stage.refused and stage.additions["REQ-1"]["//p:t"] == ["a::*", "s::run", "s::skip"]
    assert stage.from_worksheet == []


def test_model_stage_refuses_additions_that_would_change_an_owner(tmp_path, monkeypatch):
    """The safety net behind _compress: if a selector it wrote reached a case
    of another owner (or of nobody), check_model_stage refuses the stage."""
    model = load(write(tmp_path, "m.yaml", MODEL))
    real = migrate._compress
    # REQ-1's cases on //p:t compress to "*": it also reaches m::free (owned by nobody).
    monkeypatch.setattr(migrate, "_compress", lambda paths, *rest: ["*"] if "a::1" in paths else real(paths, *rest))
    stage = migrate.model_stage(model, evidence(), compress=True)
    assert any("owner of //p:t#m::free would change: None -> REQ-1" in r for r in stage.refused), stage.refused


def test_model_stage_refuses_a_quarantine_before_writing_anything(tmp_path):
    model = load(write(tmp_path, "m.yaml", MODEL))
    ev = evidence()
    ev.add(case("q::1", declared=("REQ-1", "REQ-3")))
    stage = migrate.model_stage(model, ev)
    assert [r.split(":")[0] for r in stage.refused] == ["multi-tag"]
    assert stage.model is None and not stage.additions and not stage.data


def test_stage_model_notes_every_skipped_case_it_claims(capsys, tmp_path, monkeypatch):
    """Release review (S7): a permanently skipped case got a literal selector
    and silently left its entity INCOMPLETE; the apply now says so."""
    stage = migrate.model_stage(load(write(tmp_path, "m.yaml", MODEL)), evidence())
    assert stage.skipped == {"//p:t#s::skip": "REQ-1"}
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BUILD_WORKSPACE_DIRECTORY", raising=False)
    monkeypatch.delenv("BUILD_WORKING_DIRECTORY", raising=False)
    write(tmp_path, "req/requirements.yaml", MODEL)
    prop = '<properties><property name="requirement" value="REQ-1"/></properties>'
    write(tmp_path, "bazel-testlogs/p/t/test.xml",
          f'<testsuites><testsuite name="s"><testcase classname="a" name="1">{prop}</testcase>'
          f'<testcase classname="a" name="2">{prop}<skipped/></testcase></testsuite></testsuites>')  # fmt: skip
    sheet = {"schema": migrate.SCHEMA, "inputs": {"model": ["req"], "evidence": ["bazel-testlogs"]}, "groups": []}
    write(tmp_path, "plan.rrplan", json.dumps(sheet))
    rc = cli.main(["migrate", "apply", "plan.rrplan", "--stage", "model", "--dry-run"])
    err = capsys.readouterr().err
    assert rc == 0 and "note: //p:t#a::2: skipped in this evidence, now claimed by REQ-1" in err, err
    assert "//p:t#a::1" not in err.split("note:", 1)[-1].split("\n")[0]
