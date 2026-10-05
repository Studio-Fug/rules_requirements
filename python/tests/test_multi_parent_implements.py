# SPDX-License-Identifier: AGPL-3.0-or-later
"""``multi-parent-implements``: a requirement that implements a mitigation has
no other parent.

A mitigation's VERIFIED is derived from the requirements that implement it,
as a parent requirement's is from its children. A requirement implementing
two mitigations, or implementing one and refining a requirement, would make
each of its test cases the basis of two VERIFIED verdicts that are not on one
chain. The rule is an error by default (validate, the report, the editor's
save guard); a project that relaxes it sees exactly that rollup.
"""

import json
import subprocess

import pytest
from conftest import junit, write

from rules_requirements import cli
from rules_requirements.model import read_model
from rules_requirements.validate import validate

HEAD = (
    "user_needs: [{id: UN-1, title: n1}]\n"
    "risks:\n"
    "  - {id: RISK-1, title: overheats, severity: high, likelihood: possible}\n"
    "  - {id: RISK-2, title: burns, severity: high, likelihood: possible}\n"
)
TWO_MITIGATIONS = HEAD + (
    "requirements:\n"
    "  - {id: REQ-1, title: cut the heater at 40C, satisfies: [UN-1], "
    "verified_by: [{target: //p:t, cases: ['suite::only']}]}\n"
    "mitigations:\n"
    "  - {id: MIT-1, title: over-temperature cut-off, mitigates: [RISK-1], implemented_by: [REQ-1]}\n"
    "  - {id: MIT-2, title: surface temperature limit, mitigates: [RISK-2], implemented_by: [REQ-1]}\n"
)
IMPLEMENTS_AND_REFINES = HEAD + (
    "requirements:\n"
    "  - {id: REQ-0, title: heater control, satisfies: [UN-1]}\n"
    "  - {id: REQ-1, title: cut the heater at 40C, refines: [REQ-0], "
    "verified_by: [{target: //p:t, cases: ['suite::only']}]}\n"
    "mitigations:\n"
    "  - {id: MIT-1, title: over-temperature cut-off, mitigates: [RISK-1], implemented_by: [REQ-1]}\n"
)
# The fixes the message names: one mitigation for both risks; the parent implements it.
ONE_MITIGATION_TWO_RISKS = HEAD + (
    "requirements:\n"
    "  - {id: REQ-1, title: cut the heater at 40C, satisfies: [UN-1], "
    "verified_by: [{target: //p:t, cases: ['suite::only']}]}\n"
    "mitigations:\n"
    "  - id: MIT-1\n"
    "    title: over-temperature cut-off\n"
    "    mitigates: [RISK-1, RISK-2]\n"
    "    implemented_by: [REQ-1, REQ-1]\n"
)
PARENT_IMPLEMENTS = HEAD + (
    "requirements:\n"
    "  - {id: REQ-0, title: heater control, satisfies: [UN-1]}\n"
    "  - {id: REQ-1, title: cut the heater at 40C, refines: [REQ-0], "
    "verified_by: [{target: //p:t, cases: ['suite::only']}]}\n"
    "  - {id: REQ-2, title: limit the surface, refines: [REQ-0]}\n"
    "mitigations:\n"
    "  - {id: MIT-1, title: over-temperature cut-off, mitigates: [RISK-1, RISK-2], implemented_by: [REQ-0]}\n"
)
RELAXED = "config: {rules: {multi-parent-implements: warning}}\n"


def issues(tmp_path, text):
    model, _ = read_model(write(tmp_path, "req/m.yaml", text))
    assert not model.parse_errors
    return validate(model)


def run(capsys, *argv):
    rc = cli.main([str(a) for a in argv])
    out, err = capsys.readouterr()
    return rc, out, err


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for var in ("BUILD_WORKSPACE_DIRECTORY", "BUILD_WORKING_DIRECTORY", "XML_OUTPUT_FILE"):
        monkeypatch.delenv(var, raising=False)
    junit(tmp_path / "bazel-testlogs", "p/t/test.xml", [("only", "passed", [], "")])
    return tmp_path


@pytest.mark.parametrize(
    "text, names",
    [
        (TWO_MITIGATIONS, "implements MIT-1, MIT-2"),
        (IMPLEMENTS_AND_REFINES, "implements MIT-1 and refines REQ-0"),
    ],
)
def test_a_second_parent_of_an_implementing_requirement_is_an_error_by_default(tmp_path, text, names):
    (issue,) = [i for i in issues(tmp_path, text) if i.code == "multi-parent-implements"]
    assert issue.severity == "error" and issue.entity == "REQ-1"
    assert issue.message.startswith(f"REQ-1: {names};")
    assert not [i for i in issues(tmp_path, text) if i.code == "multi-parent-refines"]


@pytest.mark.parametrize("severity", ["warning", "off"])
def test_the_rule_is_configurable(tmp_path, severity):
    text = f"config: {{rules: {{multi-parent-implements: {severity}}}}}\n" + TWO_MITIGATIONS
    found = [i.severity for i in issues(tmp_path, text) if i.code == "multi-parent-implements"]
    assert found == ([] if severity == "off" else [severity])
    assert not [i for i in issues(tmp_path, text) if i.severity == "error"]


@pytest.mark.parametrize("text", [ONE_MITIGATION_TWO_RISKS, PARENT_IMPLEMENTS])
def test_one_chain_is_fine(tmp_path, text):
    """One mitigation for several risks (a requirement listed twice is one
    link), and a parent requirement implementing the mitigation for all of
    its children: each case rolls up one chain."""
    assert [i for i in issues(tmp_path, text) if i.severity == "error"] == []
    assert not [i for i in issues(tmp_path, text) if i.code.startswith("multi-parent")]


@pytest.mark.parametrize("text", [TWO_MITIGATIONS, IMPLEMENTS_AND_REFINES])
def test_validate_and_report_refuse_it(capsys, project, text):
    write(project, "req/m.yaml", text)
    rc, _, err = run(capsys, "validate", "req")
    assert rc == 1 and "[multi-parent-implements] REQ-1:" in err
    out = project / "r.json"
    rc, _, err = run(capsys, "report", "--model", "req", "--evidence", project / "bazel-testlogs", "--json", out)
    assert rc == 2 and "multi-parent-implements" in err and "model is invalid" in err
    assert not out.exists()  # no verdicts are written over an invalid model


def test_relaxed_the_one_case_is_the_derived_basis_of_both_mitigations(capsys, project):
    """What the rule forbids, made visible: with it relaxed, the one case of
    REQ-1 is the whole basis of MIT-1's and MIT-2's VERIFIED."""
    write(project, "req/m.yaml", RELAXED + TWO_MITIGATIONS)
    rc, _, err = run(capsys, "validate", "req")
    assert rc == 0 and "warning: [multi-parent-implements]" in err
    out = project / "r.json"
    rc, _, _ = run(capsys, "report", "--model", "req", "--evidence", project / "bazel-testlogs", "--json", out)
    assert rc == 0
    doc = json.loads(out.read_text())
    mits = {m["id"]: m for m in doc["mitigations"]}
    for mit in ("MIT-1", "MIT-2"):
        assert mits[mit]["status"] == "VERIFIED"
        assert mits[mit]["basis"] == "derived" and mits[mit]["derived_from"] == ["REQ-1"]
    rc, _, _ = run(capsys, "check-report", out)
    assert rc == 0  # the relaxed rollup is consistent; the rule is what forbids it


def _repo(tmp_path, text):
    from rules_requirements.server.workspace import Workspace

    write(tmp_path, "req/m.yaml", text)
    junit(tmp_path / "bazel-testlogs", "p/t/test.xml", [("only", "passed", [], "")])
    for args in (("init", "-q", "-b", "main"), ("add", "-A"), ("commit", "-qm", "init")):
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=tmp_path, check=True, capture_output=True
        )
    return Workspace(root=str(tmp_path), model_paths=["req"], evidence_paths=["bazel-testlogs"])


def _refused(ws, tmp_path, entity, fields, code="multi-parent-implements"):
    from rules_requirements.server.workspace import WorkspaceError

    before = (tmp_path / "req/m.yaml").read_text()
    pre = ws.precheck(entity, fields)
    assert not pre["ok"] and [p["code"] for p in pre["problems"]] == [code]
    with pytest.raises(WorkspaceError) as exc:
        ws.update(entity, fields)
    assert exc.value.status == 409
    assert [c["code"] for c in exc.value.data["conflicts"]] == [code]
    assert (tmp_path / "req/m.yaml").read_text() == before  # nothing written


EDITOR = HEAD + (
    "requirements:\n"
    "  - {id: REQ-0, title: heater control, satisfies: [UN-1]}\n"
    "  - {id: REQ-1, title: cut the heater at 40C, satisfies: [UN-1], "
    "verified_by: [{target: //p:t, cases: ['suite::only']}]}\n"
    "  - {id: REQ-2, title: limit the surface, satisfies: [UN-1]}\n"
    "mitigations:\n"
    "  - {id: MIT-1, title: over-temperature cut-off, mitigates: [RISK-1], implemented_by: [REQ-1]}\n"
    "  - {id: MIT-2, title: surface temperature limit, mitigates: [RISK-2], implemented_by: [REQ-2]}\n"
)
MIT_2 = {"title": "surface temperature limit", "mitigates": ["RISK-2"]}
REQ_1 = {"title": "cut the heater at 40C", "verified_by": [{"target": "//p:t", "cases": ["suite::only"]}]}


def test_the_editor_refuses_a_second_mitigation_with_409(tmp_path):
    """The link is written on the mitigation: making MIT-2 implemented by
    REQ-1, which already implements MIT-1, is refused, whether MIT-2 is
    edited or created."""
    ws = _repo(tmp_path, EDITOR)
    _refused(ws, tmp_path, "MIT-2", {**MIT_2, "implemented_by": ["REQ-2", "REQ-1"]})
    from rules_requirements.server.workspace import WorkspaceError

    created = {**MIT_2, "id": "MIT-3", "implemented_by": ["REQ-1"]}
    pre = ws.precheck("MIT-3", created, kind="mitigation")
    assert [p["code"] for p in pre["problems"]] == ["multi-parent-implements"]
    before = (tmp_path / "req/m.yaml").read_text()
    with pytest.raises(WorkspaceError) as exc:
        ws.create("mitigation", created)
    assert exc.value.status == 409
    assert [c["code"] for c in exc.value.data["conflicts"]] == ["multi-parent-implements"]
    assert (tmp_path / "req/m.yaml").read_text() == before
    # One mitigation for both risks saves.
    ws.update(
        "MIT-1", {"title": "over-temperature cut-off", "mitigates": ["RISK-1", "RISK-2"], "implemented_by": ["REQ-1"]}
    )


def test_the_editor_refuses_a_refines_parent_on_an_implementing_requirement_with_409(tmp_path):
    ws = _repo(tmp_path, EDITOR)
    _refused(ws, tmp_path, "REQ-1", {**REQ_1, "refines": ["REQ-0"]})
    ws.update("REQ-1", {**REQ_1, "title": "cut the heater at 40 C", "satisfies": ["UN-1"]})  # no second parent


def test_the_editor_lets_a_grandfathered_requirement_drop_parents_but_not_gain_one(tmp_path):
    """REQ-1 already implements two mitigations on disk (the model is
    invalid, but an edit must not make it worse): its own edits save,
    dropping a link saves, a third mitigation is refused."""
    text = EDITOR.replace("implemented_by: [REQ-2]", "implemented_by: [REQ-1]")
    text += "  - {id: MIT-3, title: alarm, mitigates: [RISK-2], implemented_by: [REQ-2]}\n"
    ws = _repo(tmp_path, text)
    ws.update("REQ-1", {**REQ_1, "title": "cut the heater at 40 C", "satisfies": ["UN-1"]})
    ws.rename("MIT-1", "MIT-10")  # a renamed parent is the same parent
    _refused(ws, tmp_path, "MIT-3", {"title": "alarm", "mitigates": ["RISK-2"], "implemented_by": ["REQ-2", "REQ-1"]})
    _refused(ws, tmp_path, "REQ-1", {**REQ_1, "satisfies": ["UN-1"], "refines": ["REQ-0"]})
    ws.update("MIT-2", {**MIT_2, "implemented_by": []})  # drops a parent: one is left
    assert not [i for i in validate(ws.model) if i.code == "multi-parent-implements"]
