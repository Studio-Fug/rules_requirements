# SPDX-License-Identifier: AGPL-3.0-or-later
import json

import pytest
from conftest import MODEL, junit, write

from rules_requirements import cli


def run(capsys, *argv):
    rc = cli.main(list(argv))
    out, err = capsys.readouterr()
    return rc, out, err


def test_validate(capsys, model_path, tmp_path):
    rc, out, _ = run(capsys, "validate", model_path)
    assert rc == 0 and "2 user needs, 3 requirements, 1 risks, 1 mitigations, 1 test methods" in out
    bad = write(tmp_path, "bad.yaml", MODEL.replace("satisfies: [UN-2]", "satisfies: [UN-7]"))
    rc, _, err = run(capsys, "validate", bad)
    assert rc == 1 and "dangling-reference" in err
    rc, out, _ = run(capsys, "validate", bad, "--format", "json")
    assert rc == 1 and "dangling-reference" in {i["code"] for i in json.loads(out)}
    rc, _, err = run(capsys, "validate", str(tmp_path / "missing"))
    assert rc == 1 and "not found" in err
    warn = write(
        tmp_path,
        "w.yaml",
        "config: {rules: {need-unsatisfied: warning, unknown-field: warning}}\nuser_needs: [{id: UN-1, title: x, extra: 1}]\n",
    )
    rc, _, err = run(capsys, "validate", warn)
    assert rc == 0 and "warning" in err
    rc, _, _ = run(capsys, "validate", warn, "--strict")
    assert rc == 1


def test_scan(capsys, model_path, tmp_path):
    write(tmp_path, "src/a.py", "# @rr(REQ-1): heats\ndef heat(): pass\n")
    write(tmp_path, "src/b.py", "# @rr(REQ-42)\n")
    rc, out, err = run(
        capsys, "scan", "--model", model_path, "--root", str(tmp_path), "--list", "--json", str(tmp_path / "refs.json")
    )
    assert rc == 1 and "src/b.py:1: REQ-42" in err
    assert "src/a.py:1: implements REQ-1 [def heat] — heats" in out
    assert json.loads((tmp_path / "refs.json").read_text())[0]["ids"] == ["REQ-1"]
    rc, out, _ = run(
        capsys, "check-annotations", "--model", model_path, "--root", str(tmp_path), "--exclude", "src/b.py"
    )
    assert rc == 0 and "all references resolve" in out
    rc, _, _ = run(capsys, "scan", "--model", model_path, "--root", str(tmp_path), "--files", "src/a.py")
    assert rc == 0
    bad = write(tmp_path, "bad.yaml", "user_needs: [{id: UN-1}]\n")
    rc, _, _ = run(capsys, "scan", "--model", bad, "--root", str(tmp_path))
    assert rc == 2


def test_report_outputs_and_fail_on(capsys, model_path, tmp_path):
    logs = tmp_path / "bazel-testlogs"
    junit(
        logs,
        "p/t/test.xml",
        [("a", "passed", ["REQ-1"], ""), ("b", "failed", ["REQ-2"], ""), ("c", "passed", ["REQ-3"], "hil")],
    )
    out = tmp_path / "out"
    rc, _, err = run(
        capsys, "report", "--model", model_path, "--evidence", str(logs),
        "--html", str(out / "r.html"), "--json", str(out / "r.json"), "--out", str(out / "r.md"),
        "--queue-out", str(out / "q.json"), "--title", "T", "--current-build", "sha=1",
    )  # fmt: skip
    assert rc == 0
    assert (
        "verification: 2/3 requirements (1 failed" in err
        and "FAILED: REQ-2" in err
        and "cost-pyramid warn: REQ-3" in err
    )
    assert json.loads((out / "r.json").read_text())["title"] == "T"
    assert (out / "r.md").read_text().startswith("# T")
    assert "<!doctype html>" in (out / "r.html").read_text()
    queue = json.loads((out / "q.json").read_text())["queue"]
    assert {q["kind"] for q in queue} >= {"failed", "pyramid"}
    for policy, expected in (("failed", 1), ("unverified", 1), ("gaps", 1), ("none", 0)):
        rc, _, _ = run(capsys, "aggregate", "--requirements", model_path, "--junit", str(logs), "--fail-on", policy)
        assert rc == expected, policy
    rc, _, _ = run(capsys, "report", "--model", model_path, "--evidence", str(logs), "--pyramid-policy", "error")
    assert rc == 1
    rc, _, err = run(capsys, "report", "--model", model_path, "--evidence", str(logs), "--pyramid-policy", "off")
    assert "cost-pyramid" not in err
    rc, _, err = run(capsys, "report", "--model", model_path, "--out", str(out / "r.pdf"))
    assert rc == 2 and "cannot infer" in err
    with pytest.raises(SystemExit):
        run(capsys, "report", "--model", model_path, "--current-build", "novalue")


def test_report_unverified_and_invalid_model(capsys, model_path, tmp_path):
    rc, _, err = run(capsys, "report", "--model", model_path, "--evidence", "--fail-on", "unverified")
    assert rc == 1 and "UNVERIFIED: REQ-1, REQ-2, REQ-3" in err
    bad = write(tmp_path, "bad.yaml", "requirements: [{id: REQ-1, title: x, satisfies: [UN-1]}]\n")
    rc, _, err = run(capsys, "report", "--model", bad)
    assert rc == 2 and "invalid" in err


def test_report_scan_and_unknown_evidence(capsys, model_path, tmp_path):
    write(tmp_path, "src/c.py", "# @rr(REQ-1)\ndef heat(): pass\n")
    x = junit(tmp_path, "e.xml", [("g", "passed", ["REQ-77"], "")])
    rc, out, err = run(
        capsys, "report", "--model", model_path, "--evidence", x, "--scan", "--root", str(tmp_path), "--json", "-"
    )
    d = json.loads(out)
    assert d["requirements"][0]["implemented_in"][0]["path"] == "src/c.py"
    assert "undefined ids: REQ-77" in err


def test_graph_and_ingest(capsys, model_path, tmp_path):
    for fmt, needle in (("dot", "digraph"), ("mermaid", "flowchart LR"), ("json", '"nodes"'), ("svg", "<svg")):
        rc, out, _ = run(capsys, "graph", "--model", model_path, "--format", fmt, "--methods")
        assert rc == 0 and needle in out
    x = junit(tmp_path, "e.xml", [("g", "passed", ["REQ-1"], "")])
    rc, _, _ = run(
        capsys, "graph", "--model", model_path, "--format", "svg", "--evidence", x, "--out", str(tmp_path / "g.svg")
    )
    assert rc == 0 and "#2e9d57" in (tmp_path / "g.svg").read_text()
    rc, out, _ = run(capsys, "ingest", x)
    assert json.loads(out)["cases"][0]["requirements"] == ["REQ-1"]
    bad = write(tmp_path, "bad.yaml", "user_needs: [{id: UN-1}]\n")
    assert run(capsys, "graph", "--model", bad)[0] == 2


def test_paths_resolve_against_bazel_working_directory(capsys, model_path, tmp_path, monkeypatch):
    monkeypatch.setenv("BUILD_WORKING_DIRECTORY", str(tmp_path))
    monkeypatch.setenv("BUILD_WORKSPACE_DIRECTORY", str(tmp_path))
    rc, out, _ = run(capsys, "validate", "model.yaml")
    assert rc == 0
    m = __import__("rules_requirements.model", fromlist=["read_model"]).read_model(
        str(tmp_path / "model.yaml"), root=str(tmp_path)
    )[0]
    assert m.requirements["REQ-1"].location.path == "model.yaml"


def test_wrap_subcommand(capsys, tmp_path):
    with pytest.raises(SystemExit):
        cli.main(["wrap", "--junit-xml", str(tmp_path / "x.xml")])


def test_ingest_with_extra_ingestor_spec(capsys, tmp_path, monkeypatch):
    (tmp_path / "noop_ing.py").write_text(
        "from rules_requirements.ingest import Ingestor, TestCase\n"
        "class N(Ingestor):\n    name='noop'\n    suffixes=('.noop',)\n"
        "    def ingest(self, p):\n        return [TestCase(name='n', status='passed', requirements=('REQ-1',))]\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    f = write(tmp_path, "r.noop", "x")
    from rules_requirements import ingest

    try:
        rc, out, _ = run(capsys, "ingest", f, "--ingestor", "noop_ing:N")
        assert rc == 0 and json.loads(out)["cases"][0]["name"] == "n"
    finally:
        ingest._REGISTRY.pop("noop", None)
