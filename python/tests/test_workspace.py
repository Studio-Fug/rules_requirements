# SPDX-License-Identifier: AGPL-3.0-or-later
import os
import subprocess

import pytest
from conftest import MODEL, write

from rules_requirements.server.workspace import Workspace, WorkspaceError, entity_payload, summary_rows

GIT_ENV = {"GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@x"}


def git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True, env={**os.environ, **GIT_ENV}
    ).stdout


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
    with pytest.raises(WorkspaceError, match="not be read as part of the model"):
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


# --- adversarial-review regressions ------------------------------------------------


def test_create_into_emptied_section_and_json_files_are_refused(repo):
    ws = Workspace(root=str(repo), model_paths=["req"])
    ws.delete("REQ-3", force=True)
    ws.delete("REQ-2", force=True)
    ws.delete("REQ-1", force=True)  # leaves `requirements:` with no items
    rid = ws.create("requirement", {"title": "again", "satisfies": ["UN-1"]})
    assert rid == "REQ-1" and sorted(ws.model.ids()) == ["MIT-1", "REQ-1", "RISK-1", "TM-1", "UN-1", "UN-2"]
    write(repo, "req/gen.json", '{"requirements": [{"id": "REQ-9", "title": "j", "satisfies": ["UN-1"]}]}')
    with pytest.raises(WorkspaceError, match="JSON"):
        ws.update("REQ-9", {"title": "k", "satisfies": ["UN-1"]})
    assert "REQ-9" in (repo / "req/gen.json").read_text()


def test_stale_save_is_a_conflict_and_notes_survive(repo):
    from rules_requirements.server.workspace import entity_payload

    ws = Workspace(root=str(repo), model_paths=["req"])
    loaded = entity_payload(ws, "REQ-2")
    ws.add_note("REQ-2", "added elsewhere", "question")
    with pytest.raises(WorkspaceError) as exc:
        ws.update("REQ-2", {**loaded["data"], "title": "stale"}, version=loaded["version"])
    assert exc.value.status == 409
    # a save without notes (the form editor) keeps them
    ws.update("REQ-2", {"title": "fresh", "satisfies": ["UN-2"]})
    assert [n.text for n in ws.model.get("REQ-2").notes] == ["added elsewhere"]


def test_crlf_and_mode_are_preserved_and_noops_do_not_write(repo):
    path = repo / "req" / "model.yaml"
    path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    os.chmod(path, 0o644)
    ws = Workspace(root=str(repo), model_paths=["req"])
    before = os.stat(path).st_mtime_ns
    ws.update("REQ-1", edit_dict(ws, "REQ-1"))  # no change
    assert os.stat(path).st_mtime_ns == before
    ws.update("REQ-1", {**edit_dict(ws, "REQ-1"), "owner": "ops"})
    raw = path.read_bytes()
    assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")
    assert oct(os.stat(path).st_mode & 0o777) == "0o644"
    assert ws.model.get("REQ-1").owner == "ops"


def edit_dict(ws, eid):
    from rules_requirements.edit import entity_to_dict

    return entity_to_dict(ws.model.get(eid))


def test_rename_is_transactional_and_keeps_everything(repo):
    path = repo / "req" / "model.yaml"
    text = path.read_text().replace(
        "    title: Cut the heater at 35 C\n", "    title: Cut the heater at 35 C\n    # keep me\n    jira: ABC-1\n"
    )
    path.write_text("config: {rules: {unknown-field: warning}}\n" + text)
    ws = Workspace(root=str(repo), model_paths=["req"])
    ws.rename("REQ-3", "REQ-30")
    new = path.read_text()
    assert "# keep me\n    jira: ABC-1" in new and "id: REQ-30" in new
    assert ws.model.mitigations["MIT-1"].implemented_by == ("REQ-30",)
    # a failing part leaves every file untouched
    write(repo, "req/flow.yaml", "requirements: [{id: REQ-7, title: f, refines: [REQ-30]}]\n")
    snapshot = {p: (repo / "req" / p).read_text() for p in ("model.yaml", "flow.yaml")}
    with pytest.raises(WorkspaceError, match="flow style"):
        ws.rename("REQ-30", "REQ-31")
    assert {p: (repo / "req" / p).read_text() for p in snapshot} == snapshot


def test_commit_only_includes_model_files(repo):
    ws = Workspace(root=str(repo), model_paths=["req"], author="Ada <ada@x>")
    ws.create("user_need", {"title": "N"})
    write(repo, "req/BUILD.bazel", "# unfinished\n")
    write(repo, "req/scratch.txt", "notes\n")
    assert ws.git_status()["changed"] == ["req/model.yaml"]
    ws.commit("Add UN-3")
    committed = git(repo, "show", "--name-only", "--format=", "HEAD").split()
    assert committed == ["req/model.yaml"]
    assert "req/BUILD.bazel" in git(repo, "status", "--porcelain")


def test_history_from_a_subdirectory_root_and_unicode_names(tmp_path):
    outer = tmp_path / "outer"
    write(outer, "proj/req/sécurité.yaml", MODEL)
    git(outer, "init", "-q", "-b", "main")
    git(outer, "add", "-A")
    git(outer, "commit", "-qm", "init")
    ws = Workspace(root=str(outer / "proj"), model_paths=["req"], scan=False)
    assert ws.diff("HEAD") == []
    assert len(ws.model_at("HEAD").ids()) == 8
    log = ws.log()
    assert log[0]["parent"] == "" and ws.diff("EMPTY", log[0]["sha"])[0].change == "added"


def test_source_refuses_nested_git_dirs(repo):
    write(repo, "vendor/sub/.git/config", "[core]\n")
    ws = Workspace(root=str(repo), model_paths=["req"])
    with pytest.raises(WorkspaceError):
        ws.source("vendor/sub/.git/config")


def test_next_id_keeps_the_number_width(tmp_path):
    write(
        tmp_path,
        "m.yaml",
        "config: {prefixes: {requirement: SW-REQ}}\nuser_needs: [{id: UN-1, title: n}]\n"
        "requirements:\n  - {id: SW-REQ-001, title: a, satisfies: [UN-1]}\n  - {id: SW-REQ-002, title: b, satisfies: [UN-1]}\n",
    )
    ws = Workspace(root=str(tmp_path), model_paths=["m.yaml"], scan=False)
    assert ws.next_id("requirement") == "SW-REQ-003"
    assert ws.next_id("risk") == "RISK-1"


def test_delete_from_a_one_object_file_keeps_other_documents(tmp_path):
    write(tmp_path, "m/un.yaml", "user_needs: [{id: UN-1, title: n}]\n")
    write(
        tmp_path, "m/REQ-1.yaml", "project: {name: P}\n---\nkind: requirement\nid: REQ-1\ntitle: r\nsatisfies: [UN-1]\n"
    )
    write(tmp_path, "m/REQ-2.yaml", "# SPDX header\nkind: requirement\nid: REQ-2\ntitle: s\nsatisfies: [UN-1]\n")
    ws = Workspace(root=str(tmp_path), model_paths=["m"], scan=False)
    ws.delete("REQ-1")
    assert ws.model.project["name"] == "P" and "kind: requirement" not in (tmp_path / "m/REQ-1.yaml").read_text()
    ws.delete("REQ-2")  # nothing left but a comment: the file goes
    assert not (tmp_path / "m/REQ-2.yaml").exists()


def test_a_failed_write_changes_nothing(repo, monkeypatch):
    write(
        repo,
        "req/refs.yaml",
        "requirements:\n  - id: REQ-9\n    title: r\n    refines: [REQ-3]\n    satisfies: [UN-1]\n",
    )
    ws = Workspace(root=str(repo), model_paths=["req"])
    before = {p: (repo / "req" / p).read_bytes() for p in ("model.yaml", "refs.yaml")}
    real, calls = os.replace, []

    def flaky(src, dst):
        calls.append(dst)
        if len(calls) == 2:
            raise PermissionError(13, "Permission denied", dst)
        return real(src, dst)

    monkeypatch.setattr(os, "replace", flaky)
    with pytest.raises(WorkspaceError, match="Permission denied") as exc:
        ws.rename("REQ-3", "REQ-30")
    assert exc.value.status == 500 and "nothing was changed" in str(exc.value)
    assert len(calls) == 3  # two moves into place (the second fails), one restore
    assert {p: (repo / "req" / p).read_bytes() for p in before} == before
    assert not [f for f in os.listdir(repo / "req") if f.startswith(".rr-")]


def test_mixed_line_endings_are_kept_line_by_line(repo):
    path = repo / "req" / "model.yaml"
    lines = path.read_bytes().split(b"\n")
    mixed = b"".join(line + (b"\r\n" if i % 2 else b"\n") for i, line in enumerate(lines[:-1]))
    path.write_bytes(mixed)
    ws = Workspace(root=str(repo), model_paths=["req"])
    ws.update("REQ-2", {**edit_dict(ws, "REQ-2"), "title": "Accept 5..30 C"})
    new = path.read_bytes().splitlines(keepends=True)
    old = mixed.splitlines(keepends=True)
    i = next(k for k, line in enumerate(old) if line.startswith(b"    title: Accept setpoints"))
    assert len(new) == len(old) and [k for k, (a, b) in enumerate(zip(old, new)) if a != b] == [i]
    assert new[i] == b"    title: Accept 5..30 C\r\n" and old[i].endswith(b"\r\n")
    # an LF line in the same file keeps LF when it changes
    j = next(k for k, line in enumerate(old) if line.startswith(b"    title: Keep the room"))
    assert not old[j].endswith(b"\r\n")
    ws.update("UN-1", {**edit_dict(ws, "UN-1"), "title": "Stay comfortable"})
    assert path.read_bytes().splitlines(keepends=True)[j] == b"    title: Stay comfortable\n"


def test_with_line_endings():
    from rules_requirements.server.workspace import with_line_endings

    assert with_line_endings("a\nb\n", "a\nc\n") == "a\nc\n"
    assert with_line_endings("a\r\nb\r\n", "a\nx\nb\n") == "a\r\nx\r\nb\r\n"
    assert with_line_endings("a\r\nb\nc\n", "a\nb\nd\nc") == "a\r\nb\nd\nc"
    assert with_line_endings("a\r\nb\nc\r\n", "a\nB\nc\n") == "a\r\nB\nc\r\n"  # B keeps b's LF


def test_create_refuses_files_the_model_would_not_load(repo):
    ws = Workspace(root=str(repo), model_paths=["req"])
    for target in ("req/.drafts/x.yaml", "req/.drafts", "elsewhere/x.yaml"):
        with pytest.raises(WorkspaceError, match="would not be read"):
            ws.create("user_need", {"title": "N"}, file=target)
    assert not (repo / "req" / ".drafts").exists()


def test_rename_with_move_keeps_the_file_mode(tmp_path):
    write(tmp_path, "m/UN-1.yaml", "kind: user_need\nid: UN-1\ntitle: n\n")
    write(tmp_path, "m/UN-2.yaml", "kind: user_need\nid: UN-2\ntitle: o\n")
    os.chmod(tmp_path / "m/UN-1.yaml", 0o640)
    ws = Workspace(root=str(tmp_path), model_paths=["m"], scan=False)
    ws.rename("UN-1", "UN-7")
    assert not (tmp_path / "m/UN-1.yaml").exists()
    assert oct(os.stat(tmp_path / "m/UN-7.yaml").st_mode & 0o777) == "0o640"


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores file modes")
def test_read_only_files_and_folders_are_refused_up_front(tmp_path):
    write(tmp_path, "m/un.yaml", "user_needs: [{id: UN-1, title: n}]\n")
    write(tmp_path, "m/risks/RISK-1.yaml", "kind: risk\nid: RISK-1\ntitle: r\nseverity: high\nlikelihood: rare\n")
    write(
        tmp_path,
        "m/mit.yaml",
        "mitigations:\n  - id: MIT-1\n    title: m\n    mitigates: [RISK-1]\n    implemented_by: [REQ-1]\n",
    )
    write(tmp_path, "m/reqs/REQ-1.yaml", "kind: requirement\nid: REQ-1\ntitle: r\nsatisfies: [UN-1]\n")
    ws = Workspace(root=str(tmp_path), model_paths=["m"], scan=False)
    snapshot = {p: p.read_bytes() for p in (tmp_path / "m").rglob("*.yaml")}
    os.chmod(tmp_path / "m/mit.yaml", 0o444)
    try:
        with pytest.raises(WorkspaceError, match=r"m/mit\.yaml is read-only") as exc:
            ws.delete("REQ-1", force=True)
        assert exc.value.status == 403
        os.chmod(tmp_path / "m/mit.yaml", 0o644)
        os.chmod(tmp_path / "m/reqs", 0o555)
        with pytest.raises(WorkspaceError, match="m/reqs/ is not writable"):
            ws.delete("REQ-1", force=True)
    finally:
        os.chmod(tmp_path / "m/reqs", 0o755)
        os.chmod(tmp_path / "m/mit.yaml", 0o644)
    assert {p: p.read_bytes() for p in (tmp_path / "m").rglob("*.yaml")} == snapshot


def test_new_files_never_touch_the_process_umask(repo, monkeypatch):
    def forbidden(_mask):
        raise AssertionError("os.umask changes the umask of every thread")

    monkeypatch.setattr(os, "umask", forbidden)
    ws = Workspace(root=str(repo), model_paths=["req"])
    ws.create("user_need", {"title": "N"}, file="req/more/UN-9.yaml")
    assert (repo / "req/more/UN-9.yaml").exists()


def test_a_duplicated_id_can_be_repaired(tmp_path):
    dup = "  - id: REQ-3\n    title: Second copy\n    satisfies: [UN-1]\n"
    write(tmp_path, "req/model.yaml", MODEL.replace("risks:\n", dup.replace("REQ-3", "REQ-X") + "risks:\n"))
    path = tmp_path / "req/model.yaml"
    path.write_text(path.read_text().replace("REQ-X", "REQ-3"))
    ws = Workspace(root=str(tmp_path), model_paths=["req"], scan=False)
    assert any("duplicate id REQ-3" in e for e in ws.model.parse_errors)
    ws.delete("REQ-3")  # no force needed: the id stays defined, MIT-1 keeps its reference
    assert ws.model.get("REQ-3").title == "Second copy" and not ws.model.parse_errors
    assert ws.model.mitigations["MIT-1"].implemented_by == ("REQ-3",)
    # or rename one copy: references follow the renamed (first) copy, as they resolved
    path.write_text(path.read_text().replace("risks:\n", dup.replace("Second", "Third") + "risks:\n"))
    ws.rename("REQ-3", "REQ-30")
    assert ws.model.get("REQ-30").title == "Second copy" and ws.model.get("REQ-3").title == "Third copy"
    assert ws.model.mitigations["MIT-1"].implemented_by == ("REQ-30",) and not ws.model.parse_errors


def test_second_object_of_a_kind_gets_its_own_file(tmp_path):
    write(tmp_path, "m/un/UN-1.yaml", "kind: user_need\nid: UN-1\ntitle: n\n")
    write(tmp_path, "m/objs/multi.yaml", "kind: test_method\nid: TM-1\ntitle: t\nlevel: sil\n")
    ws = Workspace(root=str(tmp_path), model_paths=["m"], scan=False)
    assert ws.file_for_new("user_need") == "m/un"
    ws.create("user_need", {"title": "second"})
    assert (tmp_path / "m/un/UN-2.yaml").exists()
    # naming an existing one-object file adds a document to it
    ws.create("test_method", {"title": "u", "level": "hil"}, file="m/objs/multi.yaml")
    assert "---\nkind: test_method\nid: TM-2\n" in (tmp_path / "m/objs/multi.yaml").read_text()
    # a folder target whose file for the new id is taken (by another object) is a conflict
    write(tmp_path, "m/un/UN-9.yaml", "kind: user_need\nid: UN-8\ntitle: misnamed\n")
    with pytest.raises(WorkspaceError, match="already exists") as exc:
        ws.create("user_need", {"id": "UN-9", "title": "x"}, file="m/un")
    assert exc.value.status == 409


def test_history_reads_the_files_the_working_tree_reads(repo):
    write(repo, "req/.drafts/wip.yaml", "requirements: [{id: REQ-99, title: draft, satisfies: [UN-1]}]\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "drafts")
    ws = Workspace(root=str(repo), model_paths=["req"])
    assert ws.model.get("REQ-99") is None and ws.model_at("HEAD").get("REQ-99") is None
    assert ws.diff("HEAD") == []
