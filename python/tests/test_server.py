# SPDX-License-Identifier: AGPL-3.0-or-later
import json
import os
import subprocess
import urllib.error
import urllib.request

import pytest
from conftest import MODEL, write

from rules_requirements.server.app import Api, HttpError, serve
from rules_requirements.server.workspace import Workspace

GIT_ENV = {"GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@x"}


@pytest.fixture
def api(tmp_path):
    write(tmp_path, "req/model.yaml", MODEL)
    write(tmp_path, "src/ctl.py", "# @rr(REQ-1): heats\ndef heat():\n    return True\n")
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "init"], check=True, env={**os.environ, **GIT_ENV})
    return Api(Workspace(root=str(tmp_path), model_paths=["req"]), author="Ada <ada@x>")


def call(api, method, route, body=None, **query):
    return api.dispatch(method, route, {k: [str(v)] for k, v in query.items()}, body)


def test_state_and_entities(api):
    s = call(api, "GET", "/api/state")
    assert s["counts"]["requirements"] == 3 and s["git"]["branch"] == "main"
    assert s["config"]["prefixes"]["requirement"] == "REQ" and s["llm"]["available"] is False
    assert s["author"] == "Ada <ada@x>" and s["annotations_scanned"]
    rows = call(api, "GET", "/api/entities", kind="requirement")["entities"]
    assert [r["id"] for r in rows] == ["REQ-1", "REQ-2", "REQ-3"]
    with pytest.raises(HttpError) as exc:
        call(api, "GET", "/api/entities", kind="widget")
    assert exc.value.status == 400
    e = call(api, "GET", "/api/entities/REQ-1")
    assert e["implemented_in"][0]["symbol"] == "def heat"
    with pytest.raises(HttpError) as exc:
        call(api, "GET", "/api/entities/NOPE-1")
    assert exc.value.status == 404


def test_crud_notes_rename(api):
    assert call(api, "GET", "/api/next-id", kind="risk") == {"id": "RISK-2", "file": "req/model.yaml"}
    e = call(api, "POST", "/api/entities", {"kind": "risk", "data": {"title": "Fire", "severity": "critical"}})
    assert e["id"] == "RISK-2" and e["status"] == "OPEN"
    e = call(
        api, "PUT", "/api/entities/RISK-2", {"data": {"title": "Fire!", "severity": "critical", "likelihood": "rare"}}
    )
    assert e["data"]["likelihood"] == "rare"
    e = api.dispatch(
        "POST", "/api/entities/RISK-2/notes", {}, {"text": "Needs a mitigation", "kind": "todo"}, author="Bob <b@x>"
    )
    assert e["note"] == "n1" and e["data"]["notes"][0]["author"] == "Bob <b@x>"
    e = call(api, "PATCH", "/api/entities/RISK-2/notes/n1", {"status": "resolved"})
    assert e["data"]["notes"][0]["status"] == "resolved"
    for bad in ({"status": "maybe"}, {"kind": "rant"}):
        with pytest.raises(HttpError):
            call(api, "PATCH", "/api/entities/RISK-2/notes/n1", bad)
    with pytest.raises(HttpError):
        call(api, "POST", "/api/entities/RISK-2/notes", {"text": "", "kind": "todo"})
    with pytest.raises(HttpError):
        call(api, "POST", "/api/entities/RISK-2/notes", {"text": "x", "kind": "rant"})
    call(api, "DELETE", "/api/entities/RISK-2/notes/n1")
    r = call(api, "POST", "/api/entities/REQ-3/rename", {"new_id": "REQ-9"})
    assert r == {"renamed": "REQ-9", "updated_references": ["MIT-1"]}
    with pytest.raises(HttpError) as exc:
        call(api, "DELETE", "/api/entities/REQ-9")
    assert exc.value.status == 409
    assert call(api, "DELETE", "/api/entities/REQ-9", force="1") == {"deleted": "REQ-9"}
    with pytest.raises(HttpError):
        call(api, "POST", "/api/entities", [1, 2])


def test_trace_graph_queue_annotations_source(api):
    g = call(api, "GET", "/api/graph", focus="RISK-1", depth="1")
    assert {n["id"] for n in g["nodes"]} == {"RISK-1", "MIT-1"} and "#/entity/" in g["svg"]
    g = call(api, "GET", "/api/graph", kinds="user_need,requirement", methods="1")
    assert {n["kind"] for n in g["nodes"]} == {"user_need", "requirement"} and "flowchart" in g["mermaid"]
    assert call(api, "GET", "/api/report")["schema"] == "rules_requirements/report/v1"
    assert any(q["kind"] == "unverified" for q in call(api, "GET", "/api/queue")["queue"])
    ann = call(api, "GET", "/api/annotations")
    assert ann["scanned"] and ann["annotations"][0]["unknown"] == []
    assert call(api, "GET", "/api/source", path="src/ctl.py")["lines"][1] == "def heat():"
    with pytest.raises(HttpError) as exc:
        call(api, "GET", "/api/source", path="../etc/passwd")
    assert exc.value.status == 403
    assert call(api, "POST", "/api/reload")["counts"]["requirements"] == 3


def test_versioning_endpoints(api):
    call(api, "POST", "/api/entities", {"kind": "user_need", "data": {"title": "Another need"}})
    assert call(api, "GET", "/api/git/status")["changed"] == ["req/model.yaml"]
    d = call(api, "GET", "/api/diff", **{"from": "HEAD"})
    assert d["summary"] == {"added": 1, "removed": 0, "modified": 0} and d["to"] == "WORKTREE"
    c = call(api, "POST", "/api/git/commit", {"message": "Add UN-3"})
    assert c["commit"] and c["changed"] == []
    refs = call(api, "POST", "/api/git/tag", {"name": "baseline/1", "message": "first"})
    assert refs["tags"][0]["name"] == "baseline/1"
    assert [x["subject"] for x in call(api, "GET", "/api/git/log")["commits"]] == ["Add UN-3", "init"]
    assert call(api, "GET", "/api/git/refs")["head"] == "main"


def test_routing_errors(api):
    with pytest.raises(HttpError) as exc:
        call(api, "GET", "/api/nope")
    assert exc.value.status == 404
    with pytest.raises(HttpError) as exc:
        call(api, "PATCH", "/api/state")
    assert exc.value.status == 405
    with pytest.raises(HttpError) as exc:
        call(api, "GET", "/api/next-id")
    assert exc.value.status == 400


def test_agents_endpoints(api):
    a = call(api, "GET", "/api/agents")
    ids = {w["id"]: w for w in a["workflows"]}
    assert ids["completeness"]["available"] and not ids["test_adequacy"]["available"]
    job = call(api, "POST", "/api/agents/run", {"workflow": "completeness", "params": {}, "wait": True})
    assert job["status"] == "done" and job["findings"]
    assert call(api, "GET", f"/api/agents/jobs/{job['id']}")["status"] == "done"
    assert call(api, "GET", "/api/agents/jobs")["jobs"][0]["findings"] == len(job["findings"])
    with pytest.raises(HttpError) as exc:
        call(api, "POST", "/api/agents/run", {"workflow": "test_adequacy"})
    assert exc.value.status == 503
    with pytest.raises(HttpError):
        call(api, "GET", "/api/agents/jobs/job-99")
    target = next(f for f in job["findings"] if f["entity"])
    r = call(api, "POST", f"/api/findings/{target['id']}/apply", {"action": "note", "kind": "todo"})
    assert r["finding"]["status"] == "applied" and r["entity"]["data"]["notes"][0]["kind"] == "todo"
    loose = next((f for f in job["findings"] if not f["entity"]), None)
    if loose:
        with pytest.raises(HttpError):
            call(api, "POST", f"/api/findings/{loose['id']}/apply", {"action": "note"})
    other = next(f for f in job["findings"] if f["id"] != target["id"])
    assert call(api, "POST", f"/api/findings/{other['id']}/dismiss")["status"] == "dismissed"
    with pytest.raises(HttpError):
        call(api, "POST", f"/api/findings/{other['id']}/apply", {"action": "explode"})
    with pytest.raises(HttpError):
        call(api, "POST", "/api/findings/nope/apply", {"action": "note"})


def http(base, method, path, body=None, headers=None):
    h = {"Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(
        base + path, data=json.dumps(body).encode() if body is not None else None, method=method, headers=h
    )
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def test_http_security_and_static(api):
    httpd = serve(api, port=0, token="s3cret")
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    auth = {"Authorization": "Bearer s3cret"}
    try:
        assert http(base, "GET", "/api/state")[0] == 401
        status, _, body = http(base, "GET", "/api/state", headers=auth)
        assert status == 200 and json.loads(body)["counts"]["requirements"] == 3
        # mutations need the anti-CSRF header
        assert http(base, "POST", "/api/entities", {"kind": "user_need", "data": {"title": "x"}}, auth)[0] == 403
        status, _, body = http(
            base, "POST", "/api/entities", {"kind": "user_need", "data": {"title": "x"}}, {**auth, "X-RR-Request": "1"}
        )
        assert status == 200 and json.loads(body)["id"] == "UN-3"
        # DNS rebinding: foreign Host header refused
        assert http(base, "GET", "/api/state", headers={**auth, "Host": "evil.example"})[0] == 403
        status, _, body = http(base, "GET", "/api/nope", headers=auth)
        assert status == 404 and "no such endpoint" in json.loads(body)["error"]
        req = urllib.request.Request(
            base + "/api/entities", data=b"{bad", method="POST", headers={**auth, "X-RR-Request": "1"}
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req)
        assert exc.value.code == 400
        # static UI with a strict CSP; no path traversal
        status, headers, body = http(base, "GET", "/")
        assert status == 200 and b"<html" in body.lower() and "script-src 'self'" in headers["Content-Security-Policy"]
        assert http(base, "GET", "/../../etc/passwd")[0] == 404
        assert http(base, "POST", "/index.html", {})[0] == 405
    finally:
        httpd.shutdown()


def test_followups_from_the_ui(api):
    # a level-name method is not shown as a broken reference
    call(
        api,
        "POST",
        "/api/entities",
        {"kind": "requirement", "data": {"title": "Bench", "satisfies": ["UN-1"], "method": "hil"}},
    )
    out = call(api, "GET", "/api/entities/REQ-4")["outgoing"]
    assert {"id": "hil", "level": True, "missing": False}.items() <= next(
        o for o in out if o["relation"] == "method"
    ).items()
    # diff against an empty model, unknown refs are 404s
    d = call(api, "GET", "/api/diff", **{"from": "EMPTY", "to": "HEAD"})
    assert d["summary"]["added"] == 8 and d["summary"]["removed"] == 0
    with pytest.raises(HttpError) as exc:
        call(api, "GET", "/api/diff", **{"from": "no-such-branch"})
    assert exc.value.status == 404
    with pytest.raises(HttpError) as exc:
        call(api, "GET", "/api/git/log", ref="nope")
    assert exc.value.status == 404
    # the UI can show the config it needs
    cfg = call(api, "GET", "/api/state")["config"]
    assert (
        cfg["default_level"] == "simulation" and cfg["high_severities"] == ["high", "critical"] and "id_pattern" in cfg
    )
    # tag author from the body
    refs = call(api, "POST", "/api/git/tag", {"name": "b1", "author": "Zoë <z@x>"})
    assert refs["tags"][0]["name"] == "b1"


def test_percent_encoded_author_header(api):
    httpd = serve(api, port=0)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        status, _, body = http(
            base, "POST", "/api/entities/REQ-1/notes", {"text": "Umlaut check", "kind": "comment"},
            {"X-RR-Request": "1", "X-RR-Author": "Zo%C3%AB%20%3Cz%40x%3E"},
        )  # fmt: skip
        assert status == 200 and json.loads(body)["data"]["notes"][0]["author"] == "Zoë <z@x>"
    finally:
        httpd.shutdown()


def test_ui_assets_are_packaged():
    from rules_requirements.server import app

    names = set(os.listdir(app.STATIC))
    assert {"index.html", "app.js", "app.css"} <= names
    with open(os.path.join(app.STATIC, "index.html"), encoding="utf-8") as fh:
        html = fh.read()
    assert 'type="module"' in html and "<script>" not in html  # no inline scripts (CSP)


def test_findings_apply_once_and_updates_merge(api):
    job = call(api, "POST", "/api/agents/run", {"workflow": "completeness", "params": {}, "wait": True})
    f = next(x for x in job["findings"] if x["entity"])
    call(api, "POST", f"/api/findings/{f['id']}/apply", {"action": "note"})
    with pytest.raises(HttpError) as exc:
        call(api, "POST", f"/api/findings/{f['id']}/apply", {"action": "note"})
    assert exc.value.status == 409
    # an update proposal only carries some fields: the rest are kept
    from rules_requirements.agents import Finding

    finding = Finding(
        "assistant",
        "info",
        "assistant:update",
        "t",
        entity="REQ-1",
        proposal={"kind": "requirement", "op": "update", "data": {"title": "Heat faster"}},
    )
    finding.id = "x-1"
    api.jobs.jobs[job["id"]].findings.append(finding)
    r = call(api, "POST", "/api/findings/x-1/apply", {"action": "update"})
    assert (
        r["entity"]["data"]["title"] == "Heat faster"
        and r["entity"]["data"]["satisfies"] == ["UN-1"]
        and r["entity"]["data"]["modules"] == ["controller"]
    )


def test_off_loopback_needs_a_token_and_ipv6_binds(api):
    from rules_requirements.server.app import needs_token

    assert (
        needs_token("0.0.0.0")
        and needs_token("192.168.1.5")
        and not needs_token("127.0.0.1")
        and not needs_token("::1")
    )
    with pytest.raises(ValueError, match="token"):
        serve(api, host="0.0.0.0", port=0)
    try:
        httpd = serve(api, host="::1", port=0)
    except OSError:
        pytest.skip("no IPv6 loopback here")
    httpd.shutdown()


def test_bad_content_length_is_rejected(api):
    import socket

    httpd = serve(api, port=0)
    try:
        s = socket.create_connection(("127.0.0.1", httpd.server_address[1]))
        s.sendall(b"POST /api/reload HTTP/1.1\r\nHost: localhost\r\nX-RR-Request: 1\r\nContent-Length: -1\r\n\r\n")
        assert b" 400 " in s.recv(4096).split(b"\r\n")[0]
        s.close()
    finally:
        httpd.shutdown()


def test_allow_host_is_case_insensitive(api):
    httpd = serve(api, port=0, allowed_hosts={"RR.Example"})
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        assert http(base, "GET", "/api/state", headers={"Host": "rr.example"})[0] == 200
    finally:
        httpd.shutdown()


def test_concurrent_applies_of_one_finding_apply_it_once(api):
    import threading

    job = call(api, "POST", "/api/agents/run", {"workflow": "completeness", "params": {}, "wait": True})
    f = next(x for x in job["findings"] if x["entity"])
    before = len(api.ws.model.get(f["entity"]).notes)
    barrier, outcomes = threading.Barrier(4), []

    def apply():
        barrier.wait()
        try:
            call(api, "POST", f"/api/findings/{f['id']}/apply", {"action": "note"})
            outcomes.append(200)
        except HttpError as exc:
            outcomes.append(exc.status)

    threads = [threading.Thread(target=apply) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(outcomes) == [200, 409, 409, 409]
    assert len(api.ws.model.get(f["entity"]).notes) == before + 1
    with pytest.raises(HttpError) as exc:
        call(api, "POST", f"/api/findings/{f['id']}/dismiss")
    assert exc.value.status == 409
    with pytest.raises(HttpError) as exc:
        call(api, "POST", "/api/findings/nope/apply", {"action": "note"})
    assert exc.value.status == 404


def test_a_failed_apply_can_be_retried(api):
    job = call(api, "POST", "/api/agents/run", {"workflow": "completeness", "params": {}, "wait": True})
    f = next(x for x in job["findings"] if x["entity"])
    with pytest.raises(HttpError):
        call(api, "POST", f"/api/findings/{f['id']}/apply", {"action": "note", "entity": "NOPE-1"})
    call(api, "POST", f"/api/findings/{f['id']}/apply", {"action": "note"})


def test_ipv6_urls_are_bracketed(api):
    urls = []
    try:
        httpd = serve(api, host="::1", port=0, ready=urls.append)
    except OSError:
        pytest.skip("no IPv6 loopback here")
    httpd.shutdown()
    assert urls[0].startswith("http://[::1]:")


def test_static_files_may_be_symlinks_but_never_escape(api, tmp_path, monkeypatch):
    from rules_requirements.server import app as app_module

    real = tmp_path / "src"
    (real / "js").mkdir(parents=True)
    (real / "index.html").write_text("<html>ok</html>")
    (real / "js" / "a.js").write_text("export {};")
    (tmp_path / "secret.txt").write_text("secret")
    runfiles = tmp_path / "runfiles" / "static"  # like Bazel: a tree of symlinks
    (runfiles / "js").mkdir(parents=True)
    os.symlink(real / "index.html", runfiles / "index.html")
    os.symlink(real / "js" / "a.js", runfiles / "js" / "a.js")
    monkeypatch.setattr(app_module, "STATIC", str(runfiles))
    httpd = serve(api, port=0)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        assert http(base, "GET", "/")[0] == 200 and http(base, "GET", "/js/a.js")[0] == 200
        for path in ("/../secret.txt", "/js/../../secret.txt", "/js/../../../secret.txt", "/%2e%2e/secret.txt"):
            assert http(base, "GET", path)[0] == 404, path
    finally:
        httpd.shutdown()
