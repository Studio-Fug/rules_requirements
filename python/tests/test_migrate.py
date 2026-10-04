# SPDX-License-Identifier: AGPL-3.0-or-later
"""`rr migrate plan`: the attribution worksheet over today's union semantics.

The fixture repository (fixtures/migrate/pyrepo) has multi-id pytest and
unittest tags, a googletest case naming two ids, a whole target shared by two
requirements and a single-tagged case inside a target another requirement
claims. Its worksheet and Markdown rendering are pinned under
fixtures/migrate/expected; regenerate them with RR_UPDATE_FIXTURES=1.
"""

import json
import os
import shutil
import sys

import pytest
from conftest import write

from rules_requirements import ingest, migrate
from rules_requirements.case_keys import CaseKey
from rules_requirements.ingest import Evidence, TestCase
from rules_requirements.model import read_model

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "migrate")
REPO = os.path.join(FIXTURES, "pyrepo")
EXPECTED = os.path.join(FIXTURES, "expected")


def fixture_repo(tmp_path, monkeypatch):
    """A writable copy of the fixture repository, as the current directory."""
    root = tmp_path / "pyrepo"
    shutil.copytree(REPO, root)
    monkeypatch.chdir(root)
    return root


def golden(name, actual):
    path = os.path.join(EXPECTED, name)
    if os.environ.get("RR_UPDATE_FIXTURES"):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(actual)
    with open(path, encoding="utf-8") as fh:
        assert actual == fh.read(), f"{name} differs; RR_UPDATE_FIXTURES=1 regenerates it"


def fixture_plan():
    model, warnings = read_model(os.path.join(REPO, "requirements"))
    assert not warnings and not model.parse_errors
    return model, migrate.census(model, ingest.collect([os.path.join(REPO, "evidence")]))


def test_census_counts_today_union():
    _, plan = fixture_plan()
    assert len(plan.units) == 18
    assert len(plan.attributed) == 18
    contested = {str(u.row.key): u.counts_toward for u in plan.contested}
    assert len(contested) == 16
    assert contested["//web:clock_test#[target]"] == ["REQ-4", "REQ-5"]
    assert contested["//cc:codec_test#Codec::RoundTrip"] == ["REQ-1", "REQ-2"]
    assert contested["//app/tests:smoke_test#app.tests.test_smoke::test_boots"] == ["REQ-3", "REQ-6"]
    assert contested["//app/tests:unit_test#app.tests.test_legacy.LegacyTest::test_rejects_old_garbage"] == [
        "REQ-1",
        "REQ-2",
        "REQ-3",
    ]
    # Single-id and claim-only units count toward one entity: not contested.
    assert "//app/tests:smoke_test#app.tests.test_smoke::test_prints_version" not in contested
    assert "//cc:codec_test#Codec::EmptyInput" not in contested
    assert plan.claimers == {
        "//web:clock_test": ["REQ-4", "REQ-5"],
        "//app/tests:smoke_test": ["REQ-6"],
    }


def test_proposals_only_where_determined():
    _, plan = fixture_plan()
    by_key = {str(u.row.key): u for u in plan.contested}
    # REQ-2 refines REQ-1: the parent's verdict rolls up from its refinement.
    assert by_key["//cc:codec_test#Codec::RoundTrip"].proposed == "REQ-2"
    # A case's own single tag beats a whole-target claim.
    boots = by_key["//app/tests:smoke_test#app.tests.test_smoke::test_boots"]
    assert (boots.proposed, "more specific" in boots.reason) == ("REQ-3", True)
    # Unrelated ids, and a shared whole target: left open.
    assert by_key["//app/tests:unit_test#app.tests.test_link::test_reconnects_after_drop"].proposed == ""
    assert by_key["//web:clock_test#[target]"].proposed == ""


def test_worksheet_goldens(tmp_path, monkeypatch):
    fixture_repo(tmp_path, monkeypatch)
    model, _ = read_model("requirements")
    plan = migrate.census(model, ingest.collect(["evidence"]))
    doc = migrate.worksheet(plan, inputs={"model": ["requirements"], "evidence": ["evidence"]})
    assert doc["summary"] == {
        "units": 18,
        "attributed": 18,
        "contested_units": 16,
        "shared_targets": 1,
        "decided": 0,
        "open": 16,
    }
    golden("plan.json", migrate.render_json(doc))
    golden("plan.md", migrate.render_markdown(doc))
    # The YAML worksheet reads back as the same document.
    path = write(tmp_path, "w.rrplan", migrate.render_yaml(doc))
    assert migrate.load_worksheet(path) == json.loads(migrate.render_json(doc))
    assert migrate.check_worksheet(doc, model) == []


def test_decisions_group_and_case_owners():
    doc = migrate.load_worksheet(os.path.join(REPO, "decided.rrplan"))
    decided = migrate.decisions(doc)
    assert len(decided) == 16
    assert decided[CaseKey("//app/tests:unit_test", "app.tests.test_config::test_parses_minimal_file")] == "REQ-1"
    assert decided[CaseKey("//app/tests:unit_test", "app.tests.test_config::test_rejects_missing_key")] == "REQ-2"
    assert decided[CaseKey("//app/tests:unit_test", "app.tests.test_obsolete::test_helper_shape")] == "none"
    assert decided[CaseKey("//web:clock_test", "[target]")] == "REQ-4"
    assert migrate.summary(doc)["open"] == 0
    assert migrate.decisions({"groups": [{"target": "//a:b", "owner": None, "cases": [{"path": "x"}]}]}) == {
        CaseKey("//a:b", "x"): "?"
    }


def test_model_edits_from_decisions():
    doc = migrate.load_worksheet(os.path.join(REPO, "decided.rrplan"))
    edits = {(e["target"], e["requirement"]): e["action"] for e in migrate.model_edits(doc)}
    assert edits == {
        # test_boots went to REQ-3 while test_prints_version still needs REQ-6's claim.
        ("//app/tests:smoke_test", "REQ-6"): "split",
        ("//web:clock_test", "REQ-4"): "keep",
        ("//web:clock_test", "REQ-5"): "remove",
    }
    md = migrate.render_markdown(doc)
    assert "## Model edits implied by the decisions" in md and "| REQ-5 | remove |" in md
    doc["groups"][-1]["owner"] = "?"
    assert {e["action"] for e in migrate.model_edits(doc) if e["target"] == "//web:clock_test"} == {"open"}


def test_target_absent_from_the_evidence_is_never_removed():
    """A shared target the evidence has no result of (HITL-only, or a wrong
    --evidence path) says nothing about its references."""
    model, _ = read_model(os.path.join(REPO, "requirements"))
    evidence = ingest.collect([os.path.join(REPO, "evidence", "testlogs", "app")])
    doc = migrate.worksheet(migrate.census(model, evidence))
    (web,) = [t for t in doc["targets"] if t["target"] == "//web:clock_test"]
    assert web["cases"] == 0
    edits = {e["requirement"]: e for e in migrate.model_edits(doc) if e["target"] == "//web:clock_test"}
    assert {r: e["action"] for r, e in edits.items()} == {"REQ-4": "no-evidence", "REQ-5": "no-evidence"}
    md = migrate.render_markdown(doc)
    assert "## Targets without evidence" in md and "**Warning:**" in md
    assert "| `//web:clock_test` | REQ-5 |" in md and "remove" not in md


def test_merge_skips_malformed_groups():
    model, plan = fixture_plan()
    doc = migrate.worksheet(plan, previous={"schema": migrate.SCHEMA, "groups": ["just a string", {"a": 1}]})
    assert doc["summary"]["open"] == 16


def test_merge_carries_decisions_forward():
    model, plan = fixture_plan()
    previous = migrate.load_worksheet(os.path.join(REPO, "decided.rrplan"))
    doc = migrate.worksheet(plan, previous=previous)
    assert migrate.decisions(doc) == migrate.decisions(previous)
    assert doc["summary"]["decided"] == 16 and doc["summary"]["open"] == 0


def test_check_worksheet_rejects_bad_decisions():
    model, plan = fixture_plan()
    doc = migrate.worksheet(plan)
    doc["groups"][0]["owner"] = "REQ-1, REQ-3"
    doc["groups"][1]["cases"][0]["owner"] = ["REQ-1", "REQ-2"]
    doc["groups"][2]["owner"] = "REQ-99"
    doc["groups"][3]["cases"].append(dict(doc["groups"][3]["cases"][0]))
    doc["groups"].append({"target": 3})
    problems = migrate.check_worksheet(doc, model)
    assert any("groups[0].owner" in p and "more than one id" in p for p in problems)
    assert any("groups[1].cases[0].owner" in p and "more than one id" in p for p in problems)
    assert any("REQ-99 is not a requirement" in p for p in problems)
    assert any("listed twice" in p for p in problems)
    assert any("expected a mapping with target and cases" in p for p in problems)
    assert migrate.check_worksheet({"groups": {}}) == ["groups: expected a list"]
    # Without a model, an owner must still be one of the ids the cases count toward.
    assert any("REQ-99 is not among the ids" in p for p in migrate.check_worksheet(doc))


_UNSET = object()


def _one_group(owner, case_owner=_UNSET, counts=("REQ-4", "REQ-5")):
    case = {"path": "m::test_helper_shape", "status": "passed"}
    if case_owner is not _UNSET:
        case["owner"] = case_owner
    group = {"target": "//a:t", "group": "m", "counts_toward": list(counts), "owner": owner, "cases": [case]}
    return {"schema": migrate.SCHEMA, "groups": [group]}


@pytest.mark.parametrize(
    "owner, message",
    [
        ("REQ-9", "REQ-9 is not among the ids group 'm' counts toward (REQ-4, REQ-5)"),  # a typo
        ("REQ-1", "REQ-1 is not among the ids"),  # a real id, but a new claim
        (7, "7 is not an id; quote ids"),  # YAML read a number
        (True, "is not an id"),
        (None, "empty; write '?'"),  # `owner:` with nothing after it
        ("", "empty; write '?'"),  # `owner: ''`
        ("  ", "empty; write '?'"),
    ],
)
def test_owner_must_choose_among_the_counted_ids(owner, message):
    problems = migrate.check_worksheet(_one_group(owner))
    assert any(message in p for p in problems), problems
    # The same at case level.
    problems = migrate.check_worksheet(_one_group("?", case_owner=owner))
    assert any(message.replace("group 'm'", "//a:t#m::test_helper_shape") in p for p in problems), problems


def test_none_is_accepted_in_any_case():
    # YAML reads `owner: None` as the string "None": it means none, not an id named None.
    for spelling in ("none", "None", "NONE"):
        doc = _one_group(spelling)
        assert migrate.check_worksheet(doc) == []
        assert set(migrate.decisions(doc).values()) == {"none"}
    assert migrate.check_worksheet(_one_group("REQ-4")) == []
    assert migrate.check_worksheet(_one_group("?", case_owner="REQ-5")) == []


def test_group_owner_outside_a_cases_own_counts():
    doc = _one_group("REQ-4")
    doc["groups"][0]["cases"][0]["counts_toward"] = ["REQ-5", "REQ-6"]
    (problem,) = migrate.check_worksheet(doc)
    assert "takes the group's owner REQ-4" in problem and "give it an owner of its own" in problem
    doc["groups"][0]["cases"][0]["owner"] = "REQ-6"
    assert migrate.check_worksheet(doc) == []


def test_unquoted_open_owner_is_explained(tmp_path):
    text = f"schema: {migrate.SCHEMA}\ngroups:\n- target: //a:t\n  owner: ?\n  cases: []\n"
    with pytest.raises(migrate.WorksheetError, match="written with quotes"):
        migrate.load_worksheet(write(tmp_path, "q.rrplan", text))


def test_load_worksheet_errors(tmp_path):
    with pytest.raises(migrate.WorksheetError, match="cannot read"):
        migrate.load_worksheet(str(tmp_path / "missing.rrplan"))
    with pytest.raises(migrate.WorksheetError, match="not an attribution worksheet"):
        migrate.load_worksheet(write(tmp_path, "x.rrplan", "groups: []\n"))
    json_doc = write(tmp_path, "w.json", json.dumps({"schema": migrate.SCHEMA, "groups": []}))
    assert migrate.load_worksheet(json_doc)["groups"] == []


def test_census_follows_today_semantics(tmp_path):
    """Needs and mitigations count, risks/test methods/unknown ids do not, and a
    target-scope result is listed apart (it carries ids only until v0.3)."""
    model, _ = read_model(
        write(
            tmp_path,
            "m.yaml",
            """
            user_needs: [{id: UN-1, title: n}]
            requirements:
              - {id: REQ-1, title: a, satisfies: [UN-1], verified_by: ["//p:t"]}
              - {id: REQ-2, title: b, satisfies: [UN-1]}
            risks: [{id: RISK-1, title: r, severity: low}]
            """,
        )
    )
    cases = [
        TestCase("need", "passed", classname="c", requirements=("UN-1", "REQ-2"), target="//q:t"),
        TestCase("risky", "passed", classname="c", requirements=("RISK-1", "REQ-9", "REQ-2"), target="//q:t"),
        TestCase("claimed", "passed", classname="c", requirements=("REQ-2",), target="//p:t"),
        TestCase(
            "exit-status",
            "failed",
            requirements=("REQ-1", "REQ-2"),
            target="//p:t",
            properties={"rr.scope": "target"},
        ),
    ]
    plan = migrate.census(model, Evidence(cases=cases))
    units = {u.row.key.path: u for u in plan.units}
    assert units["c::need"].counts_toward == ["REQ-2", "UN-1"]
    assert units["c::risky"].counts_toward == ["REQ-2"] and units["c::risky"].ignored == ("RISK-1", "REQ-9")
    assert units["c::claimed"].counts_toward == ["REQ-1", "REQ-2"]
    doc = migrate.worksheet(plan)
    assert [c["path"] for g in doc["groups"] for c in g["cases"]] == ["c::claimed", "c::need"]
    assert doc["target_scope"] == [
        {"case": "//p:t#exit-status", "status": "failed", "counts_toward": ["REQ-1", "REQ-2"]}
    ]
    assert "## Target-scope results" in migrate.render_markdown(doc)
    assert doc["targets"] == [
        {"target": "//p:t", "claimed_by": ["REQ-1"], "shared": False, "cases": 1, "per_case_output": True}
    ]


def test_plan_paths_are_relative_below_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert migrate.plan_paths([str(tmp_path / "a"), "/elsewhere/b", "c"]) == ["a", "/elsewhere/b", "c"]


def test_exit_status_taint_is_target_scope_not_a_decision(tmp_path, monkeypatch):
    """rr wrap's exit-status case (libtest and --format junit) and
    rr_evidence's are about the run: listed under target_scope, never a
    group an owner must decide."""
    from rules_requirements import bazel
    from rules_requirements.hooks import wrap

    monkeypatch.setenv("TEST_TMPDIR", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    ev = tmp_path / "ev"
    junit_body = (
        "<testsuite name='bench'><testcase classname='bench' name='a'><properties>"
        "<property name='requirement' value='REQ-1'/></properties></testcase>"
        "<testcase classname='bench' name='b'><properties>"
        "<property name='requirement' value='REQ-2'/></properties></testcase></testsuite>"
    )
    runner = write(
        tmp_path,
        "bin/bench",
        f"#!{sys.executable}\nimport os, sys\n"
        f"open(os.environ.get('XML_OUTPUT_FILE') or {str(tmp_path / 'r.xml')!r}, 'w').write({junit_body!r})\n"
        "sys.exit(3)\n",
    )
    os.chmod(runner, 0o755)
    os.makedirs(ev / "bazel-testlogs" / "hw" / "bench_test")
    wrap.main(
        ["--format", "junit", "--junit-in", str(tmp_path / "r.xml"), "--target", "//hw:bench_test"]
        + ["--junit-xml", str(ev / "bazel-testlogs" / "hw" / "bench_test" / "test.xml"), "--", runner]
    )
    libtest = write(
        tmp_path,
        "bin/parse",
        f"#!{sys.executable}\nimport json, os\n"
        "with open(os.environ['RR_TRACE_FILE'], 'a') as fh:\n"
        "    fh.write(json.dumps({'test': 'parse::a', 'requirements': ['REQ-5']}) + '\\n')\n"
        "    fh.write(json.dumps({'test': 'parse::b', 'requirements': ['REQ-6']}) + '\\n')\n"
        "print('test parse::a ... ok')\nprint('test parse::b ... ok')\nraise SystemExit(101)\n",
    )
    os.chmod(libtest, 0o755)
    os.makedirs(ev / "bazel-testlogs" / "rs" / "parse_test")
    wrap.main(["--junit-xml", str(ev / "bazel-testlogs" / "rs" / "parse_test" / "test.xml"), "--", libtest])
    bazel.main(["run-tests", "--out", str(ev / "rr" / "testlogs"), "--test", "//hw:leaky_test=bin/bench=_main"])

    model, _ = read_model(os.path.join(REPO, "requirements"))
    plan = migrate.census(model, ingest.collect([str(ev)]))
    doc = migrate.worksheet(plan)
    decided_paths = {c["path"] for g in doc["groups"] for c in g["cases"]}
    assert not any("exit-status" in p for p in decided_paths), decided_paths
    scoped = {r["case"]: r["counts_toward"] for r in doc["target_scope"]}
    assert scoped == {
        "//hw:bench_test#bench_test::exit-status": ["REQ-1", "REQ-2"],
        "//hw:leaky_test#//hw:leaky_test::exit-status": ["REQ-1", "REQ-2"],
        "//rs:parse_test#parse::exit-status": ["REQ-5", "REQ-6"],
    }
