# SPDX-License-Identifier: AGPL-3.0-or-later
"""The 0.3 gates of the command line: ``rr report`` exit 3 and lanes,
``rr attribution``, ``rr sets``, ``rr check-report`` and per-check
``rr validate`` JUnit."""

import json
import os
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from conftest import junit, write

from rules_requirements import cli

MODEL = (
    "user_needs: [{id: UN-1, title: n}]\n"
    "requirements:\n"
    "  - {id: REQ-1, title: a, satisfies: [UN-1], verified_by: [{target: //p:t, cases: ['suite::a*']}]}\n"
    "  - {id: REQ-2, title: b, satisfies: [UN-1], verified_by: [{target: //p:t, cases: ['suite::b']}]}\n"
    "  - {id: REQ-3, title: c, satisfies: [UN-1], verified_by: [{target: //hitl:t, cases: ['suite::h']}]}\n"
)


def run(capsys, *argv):
    rc = cli.main([str(a) for a in argv])
    out, err = capsys.readouterr()
    return rc, out, err


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BUILD_WORKSPACE_DIRECTORY", raising=False)
    monkeypatch.delenv("BUILD_WORKING_DIRECTORY", raising=False)
    monkeypatch.delenv("XML_OUTPUT_FILE", raising=False)
    write(tmp_path, "req/requirements.yaml", MODEL)
    logs = tmp_path / "bazel-testlogs"
    junit(logs, "p/t/test.xml", [("a1", "passed", [], ""), ("a2", "passed", [], ""), ("b", "passed", [], "")])
    return tmp_path


def evidence(root):
    return ["--evidence", str(root / "bazel-testlogs")]


# --- rr report: exit codes -------------------------------------------------


def test_report_exit_codes_0_1_2_3(capsys, project):
    model = ["--model", "req"]
    rc, _, err = run(capsys, "report", *model, *evidence(project))
    assert rc == 0 and "ATTRIBUTION ERROR" not in err
    rc, _, _ = run(capsys, "report", *model, *evidence(project), "--fail-on", "unverified")
    assert rc == 1  # REQ-3 did not run
    write(project, "bad/m.yaml", "user_needs: [{id: UN-1}]\n")
    rc, _, _ = run(capsys, "report", "--model", "bad", *evidence(project))
    assert rc == 2
    # A multi-tag case: quarantined, exit 3 -- and the reports are written first.
    junit(
        project / "bazel-testlogs", "p/t/test.xml", [("a1", "passed", ["REQ-1", "REQ-2"], ""), ("b", "passed", [], "")]
    )
    out = project / "out"
    rc, _, err = run(capsys, "report", *model, *evidence(project), "--json", out / "r.json", "--md", out / "r.md",
                     "--html", out / "r.html", "--queue-out", out / "q.json")  # fmt: skip
    assert rc == 3
    assert "ATTRIBUTION ERROR: multi-tag: //p:t#suite::a1 declares REQ-1, REQ-2;" in err
    assert "exit 3 (--on-attribution-error=warn to report without failing)" in err
    assert all((out / name).exists() for name in ("r.json", "r.md", "r.html", "q.json"))
    doc = json.loads((out / "r.json").read_text())
    assert doc["summary"]["test_cases_quarantined"] == 1
    assert {r["id"]: r["status"] for r in doc["requirements"]}["REQ-2"] == "INVALID"
    # warn: exit status unaffected, the verdicts identical.
    rc, _, err = run(capsys, "report", *model, *evidence(project), "--json", out / "w.json",
                     "--on-attribution-error", "warn")  # fmt: skip
    assert rc == 0 and "ATTRIBUTION ERROR: multi-tag" in err and "the exit status ignores them" in err
    warned = json.loads((out / "w.json").read_text())
    assert warned["requirements"] == doc["requirements"]
    # --fail-on failed counts INVALID; warn leaves exactly that.
    rc, _, _ = run(
        capsys, "report", *model, *evidence(project), "--fail-on", "failed", "--on-attribution-error", "warn"
    )
    assert rc == 1


def test_report_lanes_stamp_and_filter_the_queue(capsys, project):
    write(project, "lane.txt", "//p:t\n")
    out = project / "out"
    args = ["report", "--model", "req", *evidence(project), "--json", out / "r.json", "--queue-out", out / "q.json"]
    rc, _, _ = run(capsys, *args)
    plain_queue = json.loads((out / "q.json").read_text())["queue"]
    plain = json.loads((out / "r.json").read_text())
    rc, _, _ = run(capsys, *args, "--lane", "software", "--lane-targets", "lane.txt")
    assert rc == 0
    laned = json.loads((out / "r.json").read_text())
    assert laned["attribution"]["lane"] == "software"
    assert [(r["id"], r["status"]) for r in laned["requirements"]] == [
        (r["id"], r["status"]) for r in plain["requirements"]
    ]  # verdicts never change with a lane
    (req3,) = [r for r in laned["requirements"] if r["id"] == "REQ-3"]
    assert req3["members"][0]["lane_hint"] == "out of lane"
    queue = json.loads((out / "q.json").read_text())["queue"]
    assert ("unverified", "REQ-3") in {(g["kind"], g["entity"]) for g in plain_queue}
    assert ("unverified", "REQ-3") not in {(g["kind"], g["entity"]) for g in queue}
    assert len(queue) == len(plain_queue) - 1


def test_report_sets_lock_and_no_lock(capsys, project):
    lock = write(
        project,
        "other.rrlock",
        "schema: rules_requirements/verification-lock/v1\ncases:\n  //p:t:\n    suite::a1: REQ-1\n    suite::gone: REQ-1\n",
    )
    out = project / "r.json"
    rc, _, _ = run(capsys, "report", "--model", "req", *evidence(project), "--sets-lock", lock, "--json", out)
    doc = json.loads(out.read_text())
    assert doc["attribution"]["lock"] == "other.rrlock"
    req1 = next(r for r in doc["requirements"] if r["id"] == "REQ-1")
    assert req1["status"] == "INCOMPLETE" and req1["set"]["missing"] == 1
    # A configured lock that is broken: --no-lock reads none (and validates none).
    write(project, "req/requirements.yaml", "config: {sets_lock: broken.rrlock}\n" + MODEL)
    write(project, "req/broken.rrlock", "cases: [nope]\n")
    rc, _, _ = run(capsys, "report", "--model", "req", *evidence(project))
    assert rc == 2  # lock-invalid is a model error
    rc, _, _ = run(capsys, "report", "--model", "req", *evidence(project), "--no-lock", "--json", out)
    assert rc == 0 and json.loads(out.read_text())["attribution"]["lock"] is None
    with pytest.raises(SystemExit):
        cli.main(["report", "--model", "req", "--no-lock", "--sets-lock", "x"])


# --- rr attribution ----------------------------------------------------------


def test_attribution_lists_owners_suggests_selectors_and_checks(capsys, project):
    junit(project / "bazel-testlogs", "p/t/test.xml",
          [("a1", "passed", [], ""), ("b", "passed", [], ""), ("c", "passed", ["REQ-2"], ""), ("d", "failed", [], "")])  # fmt: skip
    rc, out, err = run(capsys, "attribution", "--model", "req", *evidence(project), "--suggest")
    assert rc == 0
    rows = [line.split("\t") for line in out.splitlines() if line.startswith("//")]
    assert rows == [
        ["//p:t#suite::a1", "REQ-1", "model", "-", "passed", "-"],
        ["//p:t#suite::b", "REQ-2", "model", "-", "passed", "-"],
        ["//p:t#suite::c", "REQ-2", "tag", "REQ-2", "passed", "-"],
        ["//p:t#suite::d", "-", "-", "-", "failed", "-"],
    ]
    assert '# REQ-2: add to verified_by\n- {target: "//p:t", cases: ["suite::c"]}' in out
    assert "4 case(s): 3 owned, 0 quarantined, 1 unowned (attribution: hybrid)" in err
    rc, out, _ = run(capsys, "attribution", "--model", "req", *evidence(project), "--unowned", "--format", "json")
    doc = json.loads(out)
    assert [r["case"] for r in doc["cases"]] == ["//p:t#suite::d"] and doc["mode"] == "hybrid"
    rc, _, err = run(capsys, "attribution", "--model", "req", *evidence(project), "--check")
    assert rc == 0 and "OK" in err
    # A conflict: two claims select suite::b.
    write(project, "req/requirements.yaml", MODEL.replace("'suite::a*'", "'suite::*'"))
    rc, _, _ = run(capsys, "validate", "req")
    assert rc == 1  # shared-case, statically
    junit(project / "bazel-testlogs", "p/t/test.xml", [("a1", "passed", ["REQ-1", "REQ-3"], "")])
    write(project, "req/requirements.yaml", MODEL)
    rc, _, err = run(capsys, "attribution", "--model", "req", *evidence(project), "--check")
    assert rc == 1 and "ATTRIBUTION ERROR: multi-tag:" in err
    assert "missing-case: REQ-2: selector '//p:t#suite::b' matched no case" in err


# --- rr sets -----------------------------------------------------------------


def test_sets_lock_round_trip(capsys, project):
    write(project, "req/requirements.yaml", "config: {sets_lock: verification.rrlock}\n" + MODEL)
    lock = project / "req" / "verification.rrlock"
    rc, _, err = run(capsys, "sets", "check", "--model", "req", *evidence(project))
    assert rc == 1 and "no lock there" in err
    rc, out, err = run(capsys, "sets", "lock", "--model", "req", *evidence(project))
    assert rc == 0 and not lock.exists() and '+    "suite::a1": REQ-1' in out and "pass --write" in err
    rc, _, err = run(capsys, "sets", "lock", "--model", "req", *evidence(project), "--write")
    assert rc == 0 and lock.exists()
    text = lock.read_text()
    assert '"suite::a1": REQ-1' in text and '"suite::b": REQ-2' in text and '"suite::h": REQ-3' in text
    rc, out, _ = run(capsys, "sets", "check", "--model", "req", *evidence(project))
    assert rc == 0 and "agrees with the evidence" in out
    rc, out, _ = run(capsys, "sets", "show", "REQ-1", "--model", "req", *evidence(project))
    assert rc == 0 and out.startswith("REQ-1: VERIFIED · set 2/2 passed")
    # A test disappears: the check fails; re-locking keeps it unless removals are allowed.
    junit(project / "bazel-testlogs", "p/t/test.xml", [("a1", "passed", [], ""), ("b", "passed", [], "")])
    rc, _, err = run(capsys, "sets", "check", "--model", "req", *evidence(project))
    assert rc == 1 and "missing-case: REQ-1: lock entry '//p:t#suite::a2'" in err
    rc, _, err = run(capsys, "sets", "lock", "--model", "req", *evidence(project), "--write")
    assert rc == 0 and "- //p:t#suite::a2: REQ-1 (kept" in err and "suite::a2" in lock.read_text()
    rc, _, _ = run(capsys, "sets", "lock", "--model", "req", *evidence(project), "--write", "--allow-removals")
    assert rc == 0 and "suite::a2" not in lock.read_text()
    rc, _, _ = run(capsys, "sets", "check", "--model", "req", *evidence(project))
    assert rc == 0
    # A new, unlocked member.
    junit(project / "bazel-testlogs", "p/t/test.xml", [("a1", "passed", [], ""), ("a3", "passed", [], ""),
                                                       ("b", "passed", [], "")])  # fmt: skip
    rc, _, err = run(capsys, "sets", "check", "--model", "req", *evidence(project))
    assert rc == 1 and "[unlocked-member] //p:t#suite::a3" in err
    # A quarantine refuses the lock: it has no owner to record.
    junit(project / "bazel-testlogs", "p/t/test.xml", [("a1", "passed", ["REQ-1", "REQ-2"], "")])
    before = lock.read_text()
    rc, _, err = run(capsys, "sets", "lock", "--model", "req", *evidence(project), "--write")
    assert rc == 3 and "refusing to lock quarantined cases" in err and lock.read_text() == before


def test_sets_needs_a_lock_and_a_known_entity(capsys, project):
    rc, _, err = run(capsys, "sets", "lock", "--model", "req", *evidence(project))
    assert rc == 2 and "no lock" in err
    rc, _, err = run(capsys, "sets", "show", "RISK-9", "--model", "req", *evidence(project))
    assert rc == 2
    out = project / "elsewhere.rrlock"
    rc, _, _ = run(capsys, "sets", "lock", "--model", "req", *evidence(project), "--sets-lock", out, "--write")
    assert rc == 0 and out.exists()
    # An empty file is a lock to start from (rr_sets_lock_test needs the file); a check of it fails.
    empty = write(project, "empty.rrlock", "")
    rc, _, err = run(capsys, "sets", "check", "--model", "req", *evidence(project), "--sets-lock", empty)
    assert rc == 2 and "lock-invalid" in err
    rc, _, _ = run(capsys, "sets", "lock", "--model", "req", *evidence(project), "--sets-lock", empty, "--write")
    assert rc == 0 and "suite::a1" in Path(empty).read_text()
    # --out writes elsewhere, relative to the workspace root.
    copy = project / "copy.rrlock"
    rc, _, _ = run(capsys, "sets", "lock", "--model", "req", *evidence(project), "--sets-lock", empty, "--write",
                   "--out", "copy.rrlock")  # fmt: skip
    assert rc == 0 and copy.read_text() == Path(empty).read_text()


# --- rr check-report ---------------------------------------------------------


def test_check_report_cli(capsys, project):
    out = project / "r.json"
    run(capsys, "report", "--model", "req", *evidence(project), "--json", out)
    rc, stdout, _ = run(capsys, "check-report", out)
    assert rc == 0 and "rr check-report: OK: 3 case(s): 3 owned by 2 entities, 0 quarantined, 0 unowned" in stdout
    doc = json.loads(out.read_text())
    req2 = next(r for r in doc["requirements"] if r["id"] == "REQ-2")
    req2["members"].append({"case": "//p:t#suite::a1", "target": "//p:t", "selector": "*", "via": "model",
                            "state": "passed", "owned": True})  # fmt: skip
    req2["set"]["members"] += 1
    req2["set"]["passed"] += 1
    out.write_text(json.dumps(doc))
    rc, _, err = run(capsys, "check-report", out)
    assert rc == 1 and "//p:t#suite::a1 appears under 2 entities (REQ-1, REQ-2)" in err
    # A duplicate object key could name two owners for one case: exit 1.
    run(capsys, "report", "--model", "req", *evidence(project), "--json", out)
    text = out.read_text()
    at = text.index('"owner": "REQ-1"')
    out.write_text(text[:at] + '"owner": "REQ-2", ' + text[at:])
    rc, _, err = run(capsys, "check-report", out)
    assert rc == 1 and "'owner' appears twice" in err
    out.write_text(json.dumps({"schema": "rules_requirements/report/v1"}))
    rc, _, err = run(capsys, "check-report", out)
    assert rc == 2 and "report/v2" in err


# --- rr validate: one JUnit case per check family ----------------------------


def test_validate_writes_one_junit_case_per_check_family(capsys, project, monkeypatch):
    xml = project / "test.xml"
    monkeypatch.setenv("XML_OUTPUT_FILE", str(xml))
    rc, _, _ = run(capsys, "validate", "req")
    assert rc == 0
    cases = ET.parse(xml).getroot().iter("testcase")
    got = {(c.get("classname"), c.get("name")): [ch.tag for ch in c] for c in cases}
    assert set(got) == {("rr.validate", f) for f in ("shape", "references", "coverage-rules", "claims", "lock")}
    assert not any("failure" in tags for tags in got.values())
    write(project, "req/requirements.yaml", MODEL.replace("'suite::a*'", "'suite::*'"))
    rc, _, _ = run(capsys, "validate", "req")
    assert rc == 1
    failed = {c.get("name") for c in ET.parse(xml).getroot().iter("testcase") if c.find("failure") is not None}
    assert failed == {"claims"}
    # The cases ingest as per-case evidence of the validation target.
    from rules_requirements import ingest

    keys = sorted(f"{c.classname}::{c.name}" for c in ingest.collect([str(xml)]).cases)
    assert keys == [f"rr.validate::{f}" for f in sorted(("shape", "references", "coverage-rules", "claims", "lock"))]
    monkeypatch.delenv("XML_OUTPUT_FILE")
    other = project / "j.xml"
    rc, _, _ = run(capsys, "validate", "req", "--junit", other)
    assert rc == 1 and os.path.exists(other)


def test_fail_on_failed_counts_an_invalid_requirement_that_rolls_up_into_no_failure(capsys, project):
    """REQ-2 is INVALID (its case declares REQ-2 and an unknown id: multi-tag)
    and satisfies no need (requirement-orphan made a warning), so nothing
    else reads FAILED: --fail-on failed exits 1 for the INVALID alone,
    --fail-on none exits 0."""
    write(project, "req/requirements.yaml", "config: {rules: {requirement-orphan: warning}}\n" + MODEL.replace(
        "{id: REQ-2, title: b, satisfies: [UN-1], ", "{id: REQ-2, title: b, "))  # fmt: skip
    junit(project / "bazel-testlogs", "p/t/test.xml",
          [("a1", "passed", [], ""), ("b", "passed", ["REQ-2", "REQ-404"], "")])  # fmt: skip
    out = project / "r.json"
    common = ["report", "--model", "req", *evidence(project), "--on-attribution-error", "warn"]
    rc, _, err = run(capsys, *common, "--json", out, "--fail-on", "none")
    assert rc == 0, err
    statuses = {e["id"]: e["status"] for s in ("user_needs", "requirements") for e in json.loads(out.read_text())[s]}
    assert statuses["REQ-2"] == "INVALID" and "FAILED" not in statuses.values(), statuses
    rc, _, _ = run(capsys, *common, "--fail-on", "failed")
    assert rc == 1
