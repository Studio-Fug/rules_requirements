# SPDX-License-Identifier: AGPL-3.0-or-later
"""``multi-parent-refines``: refines must form a tree.

A requirement refining two parents would make each of its test cases the
basis of both parents' derived verdicts: one case, two VERIFIED
requirements. The rule is an error by default (validate, the report, the
editor's save guard); a project that relaxes it sees exactly that rollup.
"""

import json

import pytest
from conftest import junit, write

from rules_requirements import cli
from rules_requirements.model import read_model
from rules_requirements.validate import validate

TWO_PARENTS = (
    "user_needs: [{id: UN-1, title: n1}, {id: UN-2, title: n2}]\n"
    "requirements:\n"
    "  - {id: REQ-1, title: brakes stop the car, satisfies: [UN-1]}\n"
    "  - {id: REQ-2, title: horn sounds, satisfies: [UN-2]}\n"
    "  - id: REQ-3\n"
    "    title: one child of both\n"
    "    refines:\n"
    "      - REQ-1\n"
    "      - REQ-2\n"
    "    verified_by: [{target: //p:t, cases: ['suite::only']}]\n"
)
CHAIN = (
    "user_needs: [{id: UN-1, title: n1}]\n"
    "requirements:\n"
    "  - {id: REQ-1, title: top, satisfies: [UN-1]}\n"
    "  - {id: REQ-2, title: middle, refines: [REQ-1]}\n"
    "  - {id: REQ-3, title: leaf, refines: [REQ-2, REQ-2], verified_by: [{target: //p:t, cases: ['suite::only']}]}\n"
)
RELAXED = "config: {rules: {multi-parent-refines: warning}}\n"


def issues(tmp_path, text):
    model, _ = read_model(write(tmp_path, "req/m.yaml", text))
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


def test_two_parents_are_an_error_by_default(tmp_path):
    (issue,) = [i for i in issues(tmp_path, TWO_PARENTS) if i.code == "multi-parent-refines"]
    assert issue.severity == "error" and issue.entity == "REQ-3"
    assert "REQ-1, REQ-2" in issue.message and "tree" in issue.message


@pytest.mark.parametrize("severity", ["warning", "off"])
def test_the_rule_is_configurable(tmp_path, severity):
    text = f"config: {{rules: {{multi-parent-refines: {severity}}}}}\n" + TWO_PARENTS
    found = [i.severity for i in issues(tmp_path, text) if i.code == "multi-parent-refines"]
    assert found == ([] if severity == "off" else [severity])
    assert not [i for i in issues(tmp_path, text) if i.severity == "error"]


def test_a_single_parent_chain_is_fine(tmp_path):
    # A parent listed twice is still one parent.
    assert [i for i in issues(tmp_path, CHAIN) if i.severity == "error"] == []
    assert not [i for i in issues(tmp_path, CHAIN) if i.code == "multi-parent-refines"]


def test_validate_and_report_refuse_two_parents(capsys, project):
    write(project, "req/m.yaml", TWO_PARENTS)
    rc, _, err = run(capsys, "validate", "req")
    assert rc == 1 and "[multi-parent-refines] REQ-3: refines 2 requirements (REQ-1, REQ-2)" in err
    rc, _, err = run(capsys, "report", "--model", "req", "--evidence", project / "bazel-testlogs")
    assert rc == 2 and "multi-parent-refines" in err and "model is invalid" in err


def test_relaxed_the_one_case_is_the_derived_basis_of_both_parents(capsys, project):
    write(project, "req/m.yaml", RELAXED + TWO_PARENTS)
    rc, _, err = run(capsys, "validate", "req")
    assert rc == 0 and "warning: [multi-parent-refines]" in err
    out = project / "r.json"
    rc, _, _ = run(capsys, "report", "--model", "req", "--evidence", project / "bazel-testlogs", "--json", out)
    assert rc == 0
    rows = {r["id"]: r for r in json.loads(out.read_text())["requirements"]}
    for parent in ("REQ-1", "REQ-2"):
        assert rows[parent]["status"] == "VERIFIED"
        assert rows[parent]["basis"] == "derived" and rows[parent]["derived_from"] == ["REQ-3"]
    assert rows["REQ-3"]["basis"] == "own"
    rc, _, _ = run(capsys, "check-report", out)
    assert rc == 0  # the relaxed rollup is consistent; the rule is what forbids it


def test_the_editor_refuses_a_second_parent_with_409(tmp_path):
    import subprocess

    from rules_requirements.server.workspace import Workspace, WorkspaceError

    write(tmp_path, "req/m.yaml", CHAIN)
    junit(tmp_path / "bazel-testlogs", "p/t/test.xml", [("only", "passed", [], "")])
    for args in (("init", "-q", "-b", "main"), ("add", "-A"), ("commit", "-qm", "init")):
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=tmp_path, check=True, capture_output=True
        )
    ws = Workspace(root=str(tmp_path), model_paths=["req"], evidence_paths=["bazel-testlogs"])
    before = (tmp_path / "req/m.yaml").read_text()
    leaf = {
        "title": "leaf",
        "refines": ["REQ-2", "REQ-1"],
        "verified_by": [{"target": "//p:t", "cases": ["suite::only"]}],
    }
    pre = ws.precheck("REQ-3", leaf)
    assert not pre["ok"] and [p["code"] for p in pre["problems"]] == ["multi-parent-refines"]
    with pytest.raises(WorkspaceError) as exc:
        ws.update("REQ-3", leaf)
    assert exc.value.status == 409
    assert [c["code"] for c in exc.value.data["conflicts"]] == ["multi-parent-refines"]
    assert (tmp_path / "req/m.yaml").read_text() == before  # nothing written
    # Another edit of a requirement with one parent saves.
    ws.update("REQ-3", {**leaf, "refines": ["REQ-2"], "title": "leaf, renamed"})
