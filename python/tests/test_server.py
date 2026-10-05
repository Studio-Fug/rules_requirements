# SPDX-License-Identifier: AGPL-3.0-or-later
import json
import os
import re
import subprocess
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

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
    assert call(api, "DELETE", "/api/entities/REQ-9", force="1") == {"deleted": "REQ-9", "unlocked": []}
    with pytest.raises(HttpError):
        call(api, "POST", "/api/entities", [1, 2])


def test_trace_graph_queue_annotations_source(api):
    g = call(api, "GET", "/api/graph", focus="RISK-1", depth="1")
    assert {n["id"] for n in g["nodes"]} == {"RISK-1", "MIT-1"} and "#/entity/" in g["svg"]
    g = call(api, "GET", "/api/graph", kinds="user_need,requirement", methods="1")
    assert {n["kind"] for n in g["nodes"]} == {"user_need", "requirement"} and "flowchart" in g["mermaid"]
    assert call(api, "GET", "/api/report")["schema"] == "rules_requirements/report/v2"
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
    assert {"index.html", "app.js", "app.css", "favicon.svg"} <= names
    with open(os.path.join(app.STATIC, "index.html"), encoding="utf-8") as fh:
        html = fh.read()
    assert 'type="module"' in html and "<script>" not in html  # no inline scripts (CSP)
    assert '<link rel="icon" href="favicon.svg" type="image/svg+xml" />' in html


def test_the_favicon_is_the_logo_and_follows_the_colour_scheme(api):
    httpd = serve(api, port=0)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        status, headers, body = http(base, "GET", "/favicon.svg")
    finally:
        httpd.shutdown()
    assert status == 200 and headers["Content-Type"] == "image/svg+xml"
    svg = body.decode()
    assert "<title" in svg and "rules_requirements logo" in svg and "#16b84e" in svg
    # Colours live in the stylesheet only, so that dark mode can swap them.
    assert "@media (prefers-color-scheme: dark)" in svg and " fill=" not in svg and " stroke=" not in svg
    assert " style=" not in svg  # an inline style would override the class paint the geometry test checks


_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_PAINT = ("fill", "stroke", "stroke-width", "stroke-linecap", "stroke-linejoin")


def _logo(name):
    for base in (_ROOT, os.environ.get("TEST_SRCDIR", "") + "/_main"):
        path = os.path.join(base, "docs", "_static", "logo", name)
        if os.path.exists(path):
            return path
    # In a checkout (or under Bazel, which ships it as data) the artwork must be there.
    assert not os.path.exists(os.path.join(_ROOT, "pyproject.toml")), f"missing docs/_static/logo/{name}"
    pytest.skip("the logo artwork is not available")


def _class_styles(svg):
    """The favicon's stylesheet: class -> paint, without and with dark mode."""
    style = re.search(r"<style>(.*)</style>", svg, re.S)[1]
    light, dark = (
        {
            cls: dict(re.findall(r"([\w-]+):\s*([^;]+);", body))
            for cls, body in re.findall(r"\.([\w-]+)\s*\{([^}]*)\}", css)
        }
        for css in style.split("@media (prefers-color-scheme: dark)")
    )
    return light, {cls: {**paint, **dark.get(cls, {})} for cls, paint in light.items()}


def _shapes(path, styles=None):
    """An SVG's shapes and paint in drawing order, <use>s expanded and translations applied.

    Paint comes from the (inherited) attributes or, given ``styles``, from the
    stylesheet rule for each shape's class.
    """
    root = ET.parse(path).getroot()
    by_id = {el.get("id"): el for el in root.iter() if el.get("id")}
    shapes = []

    def walk(el, dx, dy, inherited):
        tag = el.tag.replace("{http://www.w3.org/2000/svg}", "")
        if tag in ("defs", "title", "desc", "style"):
            return
        if el.get("transform"):
            m = re.fullmatch(r"translate\((-?\d+(?:\.\d+)?) (-?\d+(?:\.\d+)?)\)", el.get("transform"))
            assert m, el.get("transform")
            dx, dy = dx + float(m[1]), dy + float(m[2])
        paint = {**inherited, **{k: el.get(k) for k in _PAINT if el.get(k)}}
        if tag in ("svg", "g", "use"):
            for child in [by_id[el.get("href").lstrip("#")]] if tag == "use" else el:
                walk(child, dx, dy, paint)
            return
        if tag == "rect":
            geometry = (
                float(el.get("x")) + dx,
                float(el.get("y")) + dy,
                float(el.get("width")),
                float(el.get("height")),
            )
        elif tag == "circle":
            geometry = (float(el.get("cx")) + dx, float(el.get("cy")) + dy, float(el.get("r")))
        else:
            assert tag == "path", tag
            d = el.get("d")
            assert set(re.findall(r"[A-Za-z]", d)) <= set("MLZ"), d  # absolute moves and lines only
            nums = [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", d)]
            geometry = (re.sub(r"[^A-Z]", "", d), *((x + dx, y + dy) for x, y in zip(nums[::2], nums[1::2])))
        if styles is not None:
            paint = styles[el.get("class")]
        shapes.append((tag, geometry, tuple(paint.get(k) for k in _PAINT)))

    walk(root, 0.0, 0.0, {})
    return shapes


def test_the_favicon_draws_exactly_the_canonical_logo():
    from rules_requirements.server import app

    favicon = os.path.join(app.STATIC, "favicon.svg")
    with open(favicon, encoding="utf-8") as fh:
        light, dark = _class_styles(fh.read())
    shapes = _shapes(favicon, light)
    assert len(shapes) == 3 + 3 + 2 * 2  # nodes, checks, and two arrows of a shaft and a head
    assert shapes == _shapes(_logo("rules_requirements_logo.svg"))
    # In dark mode: the variant for dark backgrounds.
    assert _shapes(favicon, dark) == _shapes(_logo("rules_requirements_logo-dark.svg"))
    # The view box frames the art (nodes: centres 64 / 192, radius 34 + half the 12 stroke) without clipping it.
    x, y, w, h = (float(n) for n in ET.parse(favicon).getroot().get("viewBox").split())
    assert x <= 64 - 40 and y <= 64 - 40 and x + w >= 192 + 40 and y + h >= 192 + 40


def test_the_header_mark_is_the_favicon():
    # One copy of the logo geometry: the top bar shows favicon.svg itself.
    from rules_requirements.server import app

    with open(os.path.join(app.STATIC, "app.js"), encoding="utf-8") as fh:
        assert 'src: "favicon.svg"' in fh.read()
    with open(os.path.join(app.STATIC, "index.html"), encoding="utf-8") as fh:
        assert 'href="favicon.svg"' in fh.read()


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


# --------------------------------------------------------------------------- #
# One owner per test case: the 409 guard, precheck and the case ledger        #
# --------------------------------------------------------------------------- #

LEDGER = MODEL.replace(
    "  - id: REQ-1\n    title: Heat below setpoint\n",
    "  - id: REQ-1\n    title: Heat below setpoint\n"
    "    verified_by: [{target: //t:ctl_test, cases: ['suite::a', 'suite::b']}]\n",
)


@pytest.fixture
def ledger_api(tmp_path):
    write(tmp_path, "req/model.yaml", LEDGER)
    cases = "".join(f'<testcase classname="suite" name="{n}"/>' for n in "abc")
    write(
        tmp_path,
        "bazel-testlogs/t/ctl_test/test.xml",
        f'<?xml version="1.0"?><testsuites><testsuite name="s">{cases}</testsuite></testsuites>',
    )
    ws = Workspace(root=str(tmp_path), model_paths=["req"], evidence_paths=["bazel-testlogs"])
    return Api(ws, author="Ada <ada@x>")


def _claims(*cases):
    return {"title": "Accept setpoints between 5 and 30 C", "satisfies": ["UN-2"], "modules": ["controller"]} | {
        "verified_by": [{"target": "//t:ctl_test", "cases": list(cases)}]
    }


def test_a_shared_case_save_is_a_409_naming_the_case_and_its_owner(ledger_api):
    with pytest.raises(HttpError) as exc:
        call(ledger_api, "PUT", "/api/entities/REQ-2", {"data": _claims("suite::*")})
    assert exc.value.status == 409
    assert "//t:ctl_test#suite::a" in str(exc.value) and "owned by REQ-1" in str(exc.value)
    assert exc.value.data["conflicts"][0]["owner"] == "REQ-1"
    with pytest.raises(HttpError) as exc:
        call(
            ledger_api, "POST", "/api/entities", {"kind": "requirement", "data": {"title": "x", **_claims("suite::b")}}
        )
    assert exc.value.status == 409
    r = call(ledger_api, "PUT", "/api/entities/REQ-2", {"data": _claims("suite::c")})
    assert r["set"]["passed"] == 1 and r["members"][0]["case"] == "//t:ctl_test#suite::c"


def test_the_409_reaches_the_browser_with_its_conflicts(ledger_api):
    httpd = serve(ledger_api, port=0)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        status, _, body = http(base, "PUT", "/api/entities/REQ-2", {"data": _claims("suite::a")}, {"X-RR-Request": "1"})
        data = json.loads(body)
        assert status == 409 and "a test case verifies at most one requirement" in data["error"]
        assert data["conflicts"][0]["case"] == "//t:ctl_test#suite::a" and data["conflicts"][0]["owner"] == "REQ-1"
    finally:
        httpd.shutdown()


def test_precheck_endpoint(ledger_api):
    r = call(ledger_api, "POST", "/api/entities/REQ-2/precheck", {"data": _claims("suite::a")})
    assert not r["ok"] and r["problems"][0]["code"] == "shared-case" and r["problems"][0]["owner"] == "REQ-1"
    r = call(ledger_api, "POST", "/api/entities/REQ-2/precheck", {"data": _claims("suite::c")})
    assert (
        r["ok"]
        and r["set"]["members"] == 1
        and r["selectors"] == [{"target": "//t:ctl_test", "selector": "suite::c", "cases": 1}]
    )
    r = call(
        ledger_api,
        "POST",
        "/api/entities/_new/precheck",
        {
            "kind": "mitigation",
            "data": {"title": "m", "verified_by": [{"target": "//t:ctl_test", "cases": ["suite::b"]}]},
        },
    )
    assert not r["ok"] and r["id"] == "MIT-2" and r["problems"][0]["case"] == "//t:ctl_test#suite::b"
    assert "verified_by" not in call(ledger_api, "GET", "/api/entities/REQ-2")["data"]  # nothing written


def test_cases_attribution_and_move_endpoints(ledger_api):
    r = call(ledger_api, "GET", "/api/cases")
    assert r["summary"] == {"cases": 3, "owned": 2, "unowned": 1, "quarantined": 0}
    assert {c["case"]: c["owner"] for c in r["cases"]} == {
        "//t:ctl_test#suite::a": "REQ-1",
        "//t:ctl_test#suite::b": "REQ-1",
        "//t:ctl_test#suite::c": None,
    }
    assert [c["case"] for c in call(ledger_api, "GET", "/api/cases", unowned=1)["cases"]] == ["//t:ctl_test#suite::c"]
    assert call(ledger_api, "GET", "/api/cases", target="//t:other")["cases"] == []
    s = call(ledger_api, "GET", "/api/state")
    assert s["attribution"]["cases"] == 3 and s["attribution"]["quarantined"] == 0
    a = call(ledger_api, "GET", "/api/attribution")
    assert a["targets"]["//t:ctl_test"]["owned"] == 2 and a["sets"]["REQ-1"]["passed"] == 2
    plan = call(
        ledger_api, "POST", "/api/cases/move", {"case": "//t:ctl_test#suite::b", "to": "REQ-2", "dry_run": True}
    )
    assert plan["ok"] and plan["from"] == "REQ-1" and plan["plan"]
    call(ledger_api, "POST", "/api/cases/move", {"case": "//t:ctl_test#suite::b", "to": "REQ-2"})
    rows = {c["case"]: c["owner"] for c in call(ledger_api, "GET", "/api/cases")["cases"]}
    assert rows["//t:ctl_test#suite::b"] == "REQ-2"
    with pytest.raises(HttpError) as exc:
        call(ledger_api, "POST", "/api/cases/move", {"case": "nonsense", "to": "REQ-2"})
    assert exc.value.status == 400


def test_lock_endpoints(ledger_api, tmp_path):
    lock = tmp_path / "req/verification.rrlock"
    write(tmp_path, "req/model.yaml", "config:\n  sets_lock: verification.rrlock\n" + LEDGER)
    write(
        tmp_path,
        "req/verification.rrlock",
        'schema: rules_requirements/verification-lock/v1\ncases:\n  //t:ctl_test:\n    "suite::a": REQ-1\n',
    )
    status = call(ledger_api, "GET", "/api/lock")
    assert status["out_of_date"] and status["added"] == [{"case": "//t:ctl_test#suite::b", "owner": "REQ-1"}]
    assert call(ledger_api, "GET", "/api/lock", entity="REQ-2")["out_of_date"] is False
    assert call(ledger_api, "GET", "/api/entities/REQ-1")["lock"]["out_of_date"]
    assert call(ledger_api, "GET", "/api/cases")["lock_status"]["out_of_date"]
    plan = call(ledger_api, "POST", "/api/lock/update", {"dry_run": True})
    assert plan["ok"] and plan["added"] and '"suite::b"' not in lock.read_text()
    call(ledger_api, "POST", "/api/lock/update", {})
    assert '"suite::b": REQ-1' in lock.read_text()
    assert not call(ledger_api, "GET", "/api/entities/REQ-1")["lock"]["out_of_date"]
    # Renaming carries the entries along; deleting drops them (and says so).
    call(ledger_api, "POST", "/api/entities/REQ-1/rename", {"new_id": "REQ-10"})
    assert '"suite::a": REQ-10' in lock.read_text()
    r = call(ledger_api, "DELETE", "/api/entities/REQ-10", force="1")
    assert r["unlocked"] == ["//t:ctl_test#suite::a", "//t:ctl_test#suite::b"] and "REQ-10" not in lock.read_text()


def test_save_notices_and_ledger_filters(ledger_api, tmp_path):
    write(
        tmp_path,
        "bazel-testlogs/t/tag_test/test.xml",
        '<?xml version="1.0"?><testsuites><testsuite name="s">'
        '<testcase classname="suite" name="t"><properties><property name="requirement" value="REQ-3"/>'
        "</properties></testcase></testsuite></testsuites>",
    )
    ledger_api.ws.snapshot(refresh=True)
    data = _claims() | {"verified_by": [{"target": "//t:tag_test", "cases": ["suite::t"]}]}
    pre = call(ledger_api, "POST", "/api/entities/REQ-2/precheck", {"data": data})
    assert pre["ok"] and pre["notices"][0]["code"] == "takes-from-tag"
    r = call(ledger_api, "PUT", "/api/entities/REQ-2", {"data": data})
    assert r["notices"][0]["owner"] == "REQ-3" and r["notices"][0]["to"] == "REQ-2"
    whole = _claims() | {"verified_by": [{"target": "//t:smoke_test", "whole": True}]}
    with pytest.raises(HttpError) as exc:
        call(ledger_api, "PUT", "/api/entities/REQ-3", {"data": whole | {"title": "t"}})
    assert exc.value.status == 409 and [c["code"] for c in exc.value.data["conflicts"]] == ["whole-target-reference"]
    ledger_api.ws.lanes = {"hitl": ["//t:tag_test"]}
    rows = call(ledger_api, "GET", "/api/cases", lane="hitl")["cases"]
    assert [c["case"] for c in rows] == ["//t:tag_test#suite::t"]
    assert call(ledger_api, "GET", "/api/cases", state="coarse")["cases"] == []
    with pytest.raises(HttpError):
        call(ledger_api, "GET", "/api/cases", lane="nightly")
