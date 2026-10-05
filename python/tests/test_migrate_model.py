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
    assert "the owner table (4 case(s)) is unchanged" in err
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
