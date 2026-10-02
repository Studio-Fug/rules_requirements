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


def test_path_prefers_working_directory_but_falls_back_to_cwd(tmp_path, monkeypatch):
    runfiles = tmp_path / "runfiles"
    (runfiles / "req").mkdir(parents=True)
    (runfiles / "req" / "m.yaml").write_text("x")
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(runfiles)
    monkeypatch.setenv("BUILD_WORKING_DIRECTORY", str(work))
    assert cli._path("req/m.yaml") == "req/m.yaml"  # only exists in runfiles
    assert cli._path("out/report.html") == str(work / "out/report.html")  # outputs go to the user
    (work / "req").mkdir()
    (work / "req" / "m.yaml").write_text("y")
    assert cli._path("req/m.yaml") == str(work / "req/m.yaml")
    assert cli._path("-") == "-"
    assert cli._path("/abs") == "/abs"


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


def test_fail_on_counts_failures_on_needs_and_mitigations(capsys, model_path, tmp_path):
    x = junit(tmp_path, "e.xml", [("usability", "failed", ["UN-1"], ""), ("heat", "passed", ["REQ-1"], "")])
    rc, _, err = run(capsys, "report", "--model", model_path, "--evidence", x, "--fail-on", "failed")
    assert rc == 1 and "FAILED: UN-1" in err


def test_current_build_warns_when_no_identity_matches(capsys, model_path, tmp_path):
    x = junit(tmp_path, "e.xml", [("heat", "passed", ["REQ-1"], "", [("artifact.sha", "abc")])])
    _, _, err = run(capsys, "report", "--model", model_path, "--evidence", x, "--current-build", "git_sha=abc")
    assert "match no artifact identity" in err and "(sha)" in err
    _, _, err = run(capsys, "report", "--model", model_path, "--evidence", x, "--current-build", "sha=abc")
    assert "match no artifact identity" not in err


def test_cases_lists_keys(capsys, tmp_path):
    write(
        tmp_path,
        "bazel-testlogs/pkg/t/test.xml",
        '<testsuite name="s"><testcase classname="m" name="a [rr:REQ-9]">'
        '<properties><property name="requirement" value="REQ-1"/></properties></testcase></testsuite>',
    )
    loose = junit(tmp_path, "loose/report.xml", [("b", "failed", [], "")])
    rc, out, err = run(capsys, "cases", "--evidence", str(tmp_path / "bazel-testlogs"), loose)
    assert rc == 0
    assert out.splitlines() == ["//pkg:t#m::a\tpassed\tREQ-1\t-\t-", "suite:s#suite::b\tfailed\t-\t-\t-"]
    assert "2 case(s) in 2 target(s)" in err and "[unscoped-evidence]" in err and "suite:s" in err
    rc, out, _ = run(capsys, "cases", "--evidence", str(tmp_path), "--target", "//pkg:t", "--json")
    (row,) = json.loads(out)
    assert row["case"] == "//pkg:t#m::a" and row["declared"] == ["REQ-1"]


def test_migrate_plan_and_apply(capsys, tmp_path, monkeypatch):
    from test_migrate import fixture_repo

    root = fixture_repo(tmp_path, monkeypatch)
    rc, _, err = run(
        capsys,
        "migrate",
        "plan",
        "--model",
        "requirements",
        "--evidence",
        "evidence",
        "--out",
        "plan.rrplan",
        "--json",
        "plan.json",
        "--md",
        "plan.md",
    )
    assert rc == 0 and "16 of 18 attributed evidence unit(s)" in err and "16 open" in err
    assert json.loads((root / "plan.json").read_text())["summary"]["contested_units"] == 16
    assert (root / "plan.md").read_text().startswith("# Attribution worksheet")
    rc, out, _ = run(capsys, "migrate", "plan", "--model", "requirements", "--evidence", "evidence")
    assert rc == 0 and out.startswith("# Attribution worksheet") and "schema: rules_requirements/" in out
    rc, _, err = run(
        capsys, "migrate", "plan", "--model", "requirements", "--evidence", "evidence", "--merge", "decided.rrplan"
    )
    assert rc == 0 and "16 decided, 0 open" in err

    # An undecided worksheet refuses every multi-id file and writes nothing.
    rc, _, err = run(capsys, "migrate", "apply", "plan.rrplan", "--stage", "tags")
    assert rc == 1 and "0 file(s) rewritten, 5 refused" in err and "owner is undecided" in err

    before = (root / "app/tests/test_config.py").read_text()
    rc, out, err = run(capsys, "migrate", "apply", "decided.rrplan", "--stage", "tags", "--dry-run")
    assert rc == 1 and "would hold back app/tests/test_config.py" in err
    assert out.startswith("--- a/app/tests/") and '+@pytest.mark.requirements("REQ-2")' in out
    assert (root / "app/tests/test_config.py").read_text() == before
    rc, out, err = run(capsys, "migrate", "apply", "decided.rrplan", "--stage", "tags", "--dry-run", "--partial")
    assert rc == 1 and "would rewrite app/tests/test_config.py" in err

    # All or nothing by default: test_modes.py is refused, so nothing is written.
    rc, _, err = run(capsys, "migrate", "apply", "decided.rrplan", "--stage", "tags", "--model", "requirements")
    assert rc == 1 and "0 file(s) rewritten, 1 refused" in err and "pass --partial" in err
    assert (root / "app/tests/test_config.py").read_text() == before
    rc, _, err = run(
        capsys, "migrate", "apply", "decided.rrplan", "--stage", "tags", "--model", "requirements", "--partial"
    )
    assert rc == 1 and "4 file(s) rewritten, 1 refused" in err
    assert "refused app/tests/test_modes.py" in err
    assert "//cc:codec_test#Codec::RoundTrip: no Python source of this module" in err
    assert "REQ-5: remove //web:clock_test" in err and "REQ-6: split //app/tests:smoke_test" in err
    assert (root / "app/tests/test_config.py").read_text() != before

    rc, _, err = run(
        capsys, "migrate", "apply", "decided.rrplan", "--stage", "tags", "--only", "app/tests/test_smoke.py"
    )
    assert rc == 0 and "0 file(s) rewritten, 0 refused" in err
    # Cases outside --only are counted, not listed as "another language".
    assert "decided case(s) outside --only were skipped" in err and "no Python source" not in err


def test_migrate_apply_refuses_inherited_tests(capsys, tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "test_inh.py").write_text(
        "import pytest\n\n\n"
        '@pytest.mark.rr("REQ-1", "REQ-2")\n'
        "class TestBase:\n    def test_x(self):\n        pass\n\n\n"
        "class TestSub(TestBase):\n    def test_y(self):\n        pass\n",
        encoding="utf-8",
    )
    sheet = write(
        tmp_path,
        "w.rrplan",
        """
        schema: rules_requirements/attribution-worksheet/v1
        groups:
        - target: //pkg:t
          group: pkg.test_inh.TestBase
          counts_toward: [REQ-1, REQ-2]
          owner: REQ-1
          cases: [{path: pkg.test_inh.TestBase::test_x}]
        - target: //pkg:t
          group: pkg.test_inh.TestSub
          counts_toward: [REQ-1, REQ-2]
          owner: REQ-2
          cases: [{path: pkg.test_inh.TestSub::test_x}, {path: pkg.test_inh.TestSub::test_y}]
        """,
    )
    before = (tmp_path / "pkg" / "test_inh.py").read_text(encoding="utf-8")
    rc, _, err = run(capsys, "migrate", "apply", sheet, "--stage", "tags", "--root", str(tmp_path))
    assert rc == 1 and "refused pkg/test_inh.py" in err and "inherits" in err
    assert "not defined where the codemod looked" in err and "TestSub::test_x" in err
    assert "declare no id" not in err
    assert (tmp_path / "pkg" / "test_inh.py").read_text(encoding="utf-8") == before
    # An owner the case does not count toward is rejected even without a model.
    with open(sheet, encoding="utf-8") as fh:
        typo = write(tmp_path, "typo.rrplan", fh.read().replace("owner: REQ-2", "owner: REQ-9"))
    rc, _, err = run(capsys, "migrate", "apply", typo, "--stage", "tags", "--root", str(tmp_path), "--dry-run")
    assert rc == 2 and "REQ-9 is not among the ids" in err


def test_migrate_plan_and_cases_without_evidence(capsys, model_path, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    rc, out, err = run(capsys, "migrate", "plan", "--model", model_path, "--evidence", str(empty))
    assert rc == 2 and out == "" and "no evidence found" in err and "[no-evidence]" in err
    rc, _, err = run(capsys, "cases", "--evidence", str(tmp_path / "nonexistent"))
    assert rc == 2 and "no evidence found" in err
    # One good path and one mistyped one: a warning naming the bad path.
    good = junit(tmp_path, "logs/report.xml", [("a", "passed", ["REQ-1"], "")])
    rc, _, err = run(capsys, "cases", "--evidence", good, str(tmp_path / "typo"))
    assert rc == 0 and f"[no-evidence] {tmp_path / 'typo'}" in err and str(good) not in err.split("[no-evidence]")[1]


def test_migrate_plan_rejects_a_malformed_merge(capsys, model_path, tmp_path):
    good = junit(tmp_path, "logs/report.xml", [("a", "passed", ["REQ-1"], "")])
    for i, groups in enumerate(('["just a string"]', "{a: 1}")):
        bad = write(
            tmp_path, f"m{i}.rrplan", f"schema: rules_requirements/attribution-worksheet/v1\ngroups: {groups}\n"
        )
        rc, _, err = run(capsys, "migrate", "plan", "--model", model_path, "--evidence", good, "--merge", bad)
        assert rc == 2 and "Traceback" not in err and ("expected a mapping" in err or "expected a list" in err)


def test_cases_tsv_escapes_control_characters(capsys, tmp_path):
    logs = tmp_path / "bazel-testlogs" / "pkg" / "t"
    logs.mkdir(parents=True)
    (logs / "test.xml").write_text(
        '<testsuite name="s"><testcase classname="m" name="line&#10;two&#9;tab"/>'
        '<testcase classname="  " name=" "/></testsuite>',
        encoding="utf-8",
    )
    rc, out, _ = run(capsys, "cases", "--evidence", str(tmp_path / "bazel-testlogs"))
    lines = out.splitlines()
    assert rc == 0 and len(lines) == 2 and all(len(line.split("\t")) == 5 for line in lines)
    assert "//pkg:t#m::line\\ntwo\\ttab" in out and "//pkg:t#[unnamed]" in out
    rc, out, _ = run(capsys, "cases", "--evidence", str(tmp_path / "bazel-testlogs"), "--json")
    assert {r["path"] for r in json.loads(out)} == {"m::line\ntwo\ttab", "[unnamed]"}


def test_migrate_apply_rejects_bad_worksheets(capsys, model_path, tmp_path):
    rc, _, err = run(capsys, "migrate", "apply", str(tmp_path / "none.rrplan"), "--stage", "tags")
    assert rc == 2 and "cannot read worksheet" in err
    bad = write(
        tmp_path,
        "bad.rrplan",
        "schema: rules_requirements/attribution-worksheet/v1\n"
        "groups:\n- {target: '//a:b', group: m, owner: REQ-7, cases: [{path: 'm::t', owner: 'REQ-1, REQ-2'}]}\n",
    )
    rc, _, err = run(capsys, "migrate", "apply", bad, "--stage", "tags", "--model", model_path, "--root", str(tmp_path))
    assert rc == 2 and "REQ-7 is not a requirement" in err and "more than one id" in err
    rc, _, err = run(capsys, "migrate", "plan", "--model", model_path, "--merge", str(tmp_path / "none.rrplan"))
    assert rc == 2 and "cannot read worksheet" in err
    invalid = write(tmp_path, "bad.yaml", MODEL.replace("satisfies: [UN-2]", "satisfies: [UN-7]"))
    rc, _, err = run(capsys, "migrate", "plan", "--model", invalid)
    assert rc == 2 and "model is invalid" in err
    rc, _, err = run(capsys, "migrate", "apply", bad, "--stage", "tags", "--model", invalid)
    assert rc == 2
