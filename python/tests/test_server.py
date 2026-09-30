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
    e = call(api, "PUT", "/api/entities/RISK-2", {"data": {"title": "Fire!", "severity": "critical", "likelihood": "rare"}})
    assert e["data"]["likelihood"] == "rare"
    e = api.dispatch("POST", "/api/entities/RISK-2/notes", {}, {"text": "Needs a mitigation", "kind": "todo"}, author="Bob <b@x>")
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
    other = next(f for f in job["findings"] if f["status"] == "open")
    assert call(api, "POST", f"/api/findings/{other['id']}/dismiss")["status"] == "dismissed"
    with pytest.raises(HttpError):
        call(api, "POST", f"/api/findings/{other['id']}/apply", {"action": "explode"})
    with pytest.raises(HttpError):
        call(api, "POST", "/api/findings/nope/apply", {"action": "note"})


def http(base, method, path, body=None, headers=None):
    h = {"Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None, method=method, headers=h)
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
        status, _, body = http(base, "POST", "/api/entities", {"kind": "user_need", "data": {"title": "x"}}, {**auth, "X-RR-Request": "1"})
        assert status == 200 and json.loads(body)["id"] == "UN-3"
        # DNS rebinding: foreign Host header refused
        assert http(base, "GET", "/api/state", headers={**auth, "Host": "evil.example"})[0] == 403
        status, _, body = http(base, "GET", "/api/nope", headers=auth)
        assert status == 404 and "no such endpoint" in json.loads(body)["error"]
        req = urllib.request.Request(base + "/api/entities", data=b"{bad", method="POST", headers={**auth, "X-RR-Request": "1"})
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
