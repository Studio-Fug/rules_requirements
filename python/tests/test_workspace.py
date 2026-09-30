# SPDX-License-Identifier: AGPL-3.0-or-later
import os
import subprocess

import pytest
from conftest import MODEL, write

from rules_requirements.server.workspace import Workspace, WorkspaceError, entity_payload, summary_rows

GIT_ENV = {"GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@x"}


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True, env={**os.environ, **GIT_ENV}).stdout


@pytest.fixture
def repo(tmp_path):
    write(tmp_path, "req/model.yaml", MODEL)
    write(tmp_path, "src/ctl.py", "# @rr(REQ-1): heats\ndef heat():\n    return True\n")
    write(tmp_path, "tests/test_ctl.py", "# @rr(REQ-1)\ndef test_heat():\n    assert True\n")
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "init")
    return tmp_path


def test_snapshot_and_payload(repo):
    ws = Workspace(root=str(repo), model_paths=["req"])
    snap = ws.snapshot()
    assert snap is ws.snapshot()  # cached
    assert ws.files() == ["req/model.yaml"]
    p = entity_payload(ws, "REQ-1")
    assert p["status"] == "UNVERIFIED" and p["implemented_in"][0]["path"] == "src/ctl.py"
    assert p["verified_in"][0]["path"] == "tests/test_ctl.py"
    assert {"id": "UN-1", "relation": "satisfies"}.items() <= p["outgoing"][0].items()
    assert entity_payload(ws, "UN-1")["incoming"][0]["id"] == "REQ-1"
    rows = summary_rows(ws, ["risk"])
    assert rows[0]["id"] == "RISK-1" and rows[0]["status"]
    with pytest.raises(WorkspaceError):
        entity_payload(ws, "NOPE-1")


def test_create_update_rename_delete(repo):
    ws = Workspace(root=str(repo), model_paths=["req"], author="Ada <ada@x>")
    assert ws.next_id("requirement") == "REQ-4"
    rid = ws.create("requirement", {"title": "Log changes", "satisfies": ["UN-2"]})
    assert rid == "REQ-4" and ws.model.get("REQ-4").satisfies == ("UN-2",)
    with pytest.raises(WorkspaceError, match="already exists"):
        ws.create("requirement", {"id": "REQ-4", "title": "dup"})
    with pytest.raises(WorkspaceError, match="unknown kind"):
        ws.create("widget", {"title": "x"})
    with pytest.raises(WorkspaceError, match="does not match"):
        ws.create("requirement", {"id": "R-9", "title": "x"})
    with pytest.raises(WorkspaceError):
        ws.create("requirement", {"title": "x", "colour": "red"})
    ws.update("REQ-4", {"title": "Log every change", "satisfies": ["UN-2"]})
    assert ws.model.get("REQ-4").title == "Log every change"
    with pytest.raises(WorkspaceError, match="rename"):
        ws.update("REQ-4", {"id": "REQ-5", "title": "x"})
    touched = ws.rename("REQ-3", "REQ-30")
    assert touched == ["MIT-1"] and ws.model.mitigations["MIT-1"].implemented_by == ("REQ-30",)
    with pytest.raises(WorkspaceError, match="referenced by MIT-1"):
        ws.delete("REQ-30")
    ws.delete("REQ-30", force=True)
    assert "REQ-30" not in ws.model.requirements and ws.model.mitigations["MIT-1"].implemented_by == ()
    with pytest.raises(WorkspaceError):
        ws.delete("REQ-30")


def test_notes(repo):
    ws = Workspace(root=str(repo), model_paths=["req"], author="Ada <ada@x>")
    n1 = ws.add_note("REQ-1", "Is 0.5 C right?", "question")
    n2 = ws.add_note("REQ-1", "Add a soak test", "todo", author="bot")
    assert (n1, n2) == ("n1", "n2")
    notes = ws.model.get("REQ-1").notes
    assert notes[0].author == "Ada <ada@x>" and notes[1].author == "bot" and notes[0].created
    gaps = {(g.kind, g.entity) for g in ws.snapshot().matrix.gaps}
    assert ("note:question", "REQ-1") in gaps and ("note:todo", "REQ-1") in gaps
    ws.update_note("REQ-1", "n1", status="resolved")
    assert ws.model.get("REQ-1").notes[0].status == "resolved"
    ws.delete_note("REQ-1", "n2")
    assert [n.id for n in ws.model.get("REQ-1").notes] == ["n1"]
    assert ws.add_note("REQ-1", "again") == "n2"
    with pytest.raises(WorkspaceError):
        ws.update_note("REQ-1", "n9", status="resolved")
    with pytest.raises(WorkspaceError):
        ws.delete_note("REQ-1", "n9")
    with pytest.raises(WorkspaceError):
        ws.add_note("NOPE-1", "x")


def test_one_object_per_file_layout(tmp_path):
    write(tmp_path, "m/config.yaml", "project: {name: X}\n")
    write(tmp_path, "m/un/UN-1.yaml", "kind: user_need\nid: UN-1\ntitle: A\n")
    write(tmp_path, "m/un/UN-2.yaml", "kind: user_need\nid: UN-2\ntitle: B\n")
    write(tmp_path, "m/req/REQ-1.yaml", "kind: requirement\nid: REQ-1\ntitle: R\nsatisfies: [UN-1, UN-2]\n")
    ws = Workspace(root=str(tmp_path), model_paths=["m"], scan=False)
    assert ws.file_for_new("user_need") == "m/un"
    uid = ws.create("user_need", {"title": "C"})
    assert os.path.exists(tmp_path / "m/un/UN-3.yaml")
    ws.create("requirement", {"title": "S", "satisfies": ["UN-3"]}, file="m/req")
    assert os.path.exists(tmp_path / "m/req/REQ-2.yaml")
    ws.delete("REQ-2")
    assert not os.path.exists(tmp_path / "m/req/REQ-2.yaml")
    assert uid == "UN-3"
    with pytest.raises(WorkspaceError, match="not part of the model"):
        ws.create("risk", {"title": "r"}, file="elsewhere/risks.yaml")


def test_paths_are_confined(repo):
    ws = Workspace(root=str(repo), model_paths=["req"])
    assert ws.source("src/ctl.py")[0].startswith("# @rr(REQ-1)")
    for bad in ("../outside.txt", "/etc/passwd", ".git/config"):
        with pytest.raises(WorkspaceError):
            ws.source(bad)
    with pytest.raises(WorkspaceError):
        ws.source("src")
    (repo / "big.txt").write_text("x" * 600_000)
    with pytest.raises(WorkspaceError, match="too large"):
        ws.source("big.txt")
    (repo / "bin.dat").write_bytes(b"\xff\xfe\x00")
    with pytest.raises(WorkspaceError, match="not a text"):
        ws.source("bin.dat")
    with pytest.raises(WorkspaceError):
        Workspace(root=str(repo), model_paths=["../elsewhere"])


def test_git_versioning(repo):
    ws = Workspace(root=str(repo), model_paths=["req"], author="Ada <ada@x>")
    st = ws.git_status()
    assert st["git"] and st["branch"] == "main" and st["changed"] == []
    ws.create("user_need", {"title": "New need"})
    ws.create("requirement", {"title": "For the new need", "satisfies": ["UN-3"]})
    assert ws.git_status()["changed"] == ["req/model.yaml"]
    changes = ws.diff("HEAD")
    assert [(c.id, c.change) for c in changes] == [("UN-3", "added"), ("REQ-4", "added")]
    with pytest.raises(WorkspaceError, match="message"):
        ws.commit("")
    sha = ws.commit("Add UN-3")
    assert sha and ws.git_status()["changed"] == []
    assert "Ada <ada@x>" in git(repo, "log", "-1", "--format=%an <%ae>")
    with pytest.raises(WorkspaceError, match="no model changes"):
        ws.commit("again")
    ws.tag("baseline/v1", "first baseline")
    with pytest.raises(WorkspaceError, match="already exists"):
        ws.tag("baseline/v1")
    with pytest.raises(WorkspaceError, match="invalid tag"):
        ws.tag("bad..name")
    refs = ws.refs()
    assert refs["head"] == "main" and refs["tags"][0]["name"] == "baseline/v1" and "main" in refs["branches"]
    log = ws.log()
    assert [c["subject"] for c in log] == ["Add UN-3", "init"]
    assert [(c.id, c.change) for c in ws.diff("HEAD~1", "baseline/v1")] == [("UN-3", "added"), ("REQ-4", "added")]
    assert ws.diff("baseline/v1") == []
    with pytest.raises(WorkspaceError, match="invalid ref"):
        ws.diff("--output=/tmp/x")
    with pytest.raises(WorkspaceError):
        ws.model_at("no-such-ref")


def test_not_a_git_repo(tmp_path):
    write(tmp_path, "m.yaml", MODEL)
    ws = Workspace(root=str(tmp_path), model_paths=["m.yaml"], scan=False)
    assert ws.git_status() == {"git": False}
    assert ws.refs() == {"branches": [], "tags": [], "head": ""}
    assert ws.log() == []
    assert ws.file_for_new("risk") == "m.yaml"
