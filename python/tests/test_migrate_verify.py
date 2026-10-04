# SPDX-License-Identifier: AGPL-3.0-or-later
"""`rr migrate verify`: test evidence from after `rr migrate apply`, checked
against the worksheet's decisions (and, with a baseline, against the
evidence from before the rewrite).

The evidence is JUnit written the way each runner writes it: rr's pytest
plugin (``requirement`` properties and ``rr.file``), rr_node_test's reporter
(``stem > describe`` classnames) and cc/rr_case.h (one suite classname),
under a ``bazel-testlogs`` tree so each case is keyed by its build target.
"""

import json
import os
from xml.sax.saxutils import quoteattr

import pytest

from rules_requirements import cli, ingest, migrate
from rules_requirements.case_keys import CaseKey

PY = "//app/tests:config_test"
NODE = "//web:clocksync_test"
CC = "//cc:codec_test"

PY_A = CaseKey(PY, "app.tests.test_config::test_load")
PY_B = CaseKey(PY, "app.tests.test_config.TestSave::test_save")
PY_C = CaseKey(PY, "app.tests.test_config::test_other")  # undecided
NODE_A = CaseKey(NODE, "clocksync > bestSample::keeps the min-RTT sample")
CC_A = CaseKey(CC, "codec::round_trip")
CC_OPEN = CaseKey(CC, "codec::bounds")  # on the worksheet, still open
# Untagged: it counts toward REQ-4 and REQ-5 only through verified_by (both
# name //web:clocksync_test), so apply leaves it alone and the model decides.
NODE_U = CaseKey(NODE, "clocksync > offset::from the formulas")

# Each case's declared ids in the evidence from BEFORE the rewrite: the union
# of every scope's tags.
BEFORE = {
    PY_A: ["REQ-1", "REQ-2"],
    PY_B: ["REQ-1", "REQ-2"],
    PY_C: ["REQ-3"],
    NODE_A: ["REQ-4", "REQ-5"],
    CC_A: ["REQ-1", "REQ-2"],
    CC_OPEN: ["REQ-1", "REQ-2"],
    NODE_U: [],
}
DECIDED = {PY_A: "REQ-1", PY_B: "none", NODE_A: "REQ-5", CC_A: "REQ-2", NODE_U: "REQ-4"}
# What a correct rewrite leaves.
AFTER = {**BEFORE, PY_A: ["REQ-1"], PY_B: [], NODE_A: ["REQ-5"], CC_A: ["REQ-2"]}


def worksheet():
    """One group per contested case, as `rr migrate plan` lists them: its
    tags, or (untagged) the requirements claiming its target."""
    doc = {"schema": migrate.SCHEMA, "groups": []}
    for key, ids in BEFORE.items():
        if key not in DECIDED and key != CC_OPEN:
            continue
        group = {"target": key.target, "group": key.path.rpartition("::")[0]}
        if ids:
            group.update(counts_toward=ids, tags=ids)
        else:
            group.update(counts_toward=["REQ-4", "REQ-5"], target_claims=["REQ-4", "REQ-5"])
        group["owner"] = "?"
        group["cases"] = [{"path": key.path, "status": "passed", "owner": DECIDED.get(key, "?")}]
        doc["groups"].append(group)
    return doc


def _testcase(key, ids):
    """One <testcase> the way the runner of ``key.target`` writes it."""
    classname, _, name = key.path.rpartition("::")
    props = [("requirement", i) for i in ids]
    if key.target == PY:
        props.append(("rr.file", "app/tests/test_config.py"))
    body = "".join(f"<property name={quoteattr(k)} value={quoteattr(v)}/>" for k, v in props)
    return (
        f"<testcase classname={quoteattr(classname)} name={quoteattr(name)} time='0.01'>"
        + (f"<properties>{body}</properties>" if body else "")
        + "</testcase>"
    )


def evidence(root, cases, *, drop=()):
    """``bazel-testlogs/<package>/<name>/test.xml`` per target under ``root``."""
    by_target = {}
    for key, ids in cases.items():
        if key not in drop:
            by_target.setdefault(key.target, []).append(_testcase(key, ids))
    for target, items in by_target.items():
        package, _, name = target[2:].partition(":")
        path = os.path.join(root, "bazel-testlogs", package, name, "test.xml")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f"<testsuites><testsuite name={quoteattr(name)}>{''.join(items)}</testsuite></testsuites>")
    return str(root)


def check(tmp_path, after, *, baseline=True, allow_missing=False, drop=(), model=None):
    new = ingest.collect([evidence(tmp_path / "new", after, drop=drop)])
    old = ingest.collect([evidence(tmp_path / "old", BEFORE)]) if baseline else None
    return migrate.verify(worksheet(), new, old, allow_missing=allow_missing, model=model)


def model(claims):
    """A model whose requirements name targets in verified_by: ``{req: [target, ...]}``."""
    from rules_requirements.model import Model, Requirement, VerifiedBy

    ids = ["REQ-1", "REQ-2", "REQ-3", "REQ-4", "REQ-5"]
    return Model(
        requirements={
            i: Requirement(id=i, title=i, verified_by=tuple(VerifiedBy(target=t) for t in claims.get(i, [])))
            for i in ids
        }
    )


def test_every_runner_shape_is_keyed_like_the_worksheet(tmp_path):
    """The pytest, node and rr_case evidence shapes all key to the
    worksheet's case paths: a correct rewrite verifies."""
    res = check(tmp_path, AFTER)
    assert res.ok, [o.describe() for o in res.offences]
    assert res.decided == 4 and res.unchanged == 2 and res.new == 0 and not res.missing
    assert res.untagged == [NODE_U]
    # Without a baseline only the decided cases are checked.
    res = check(tmp_path, AFTER, baseline=False)
    assert res.ok and res.decided == 4 and res.unchanged == 0


@pytest.mark.parametrize("key", [PY_A, NODE_A, CC_A], ids=["pytest", "node", "rr_case"])
def test_a_decided_case_with_another_id(tmp_path, key):
    wrong = "REQ-2" if DECIDED[key] == "REQ-1" else ("REQ-4" if key == NODE_A else "REQ-1")
    res = check(tmp_path, {**AFTER, key: [wrong]})
    assert [o.describe() for o in res.offences] == [
        f"{key}: a decided case does not declare exactly its owner (expected [{DECIDED[key]}], found [{wrong}])"
    ]


@pytest.mark.parametrize("key", [PY_A, NODE_A, CC_A], ids=["pytest", "node", "rr_case"])
def test_a_decided_case_still_naming_several_ids(tmp_path, key):
    res = check(tmp_path, {**AFTER, key: BEFORE[key]})
    (off,) = res.offences
    assert off.key == key and off.expected == [DECIDED[key]] and off.found == BEFORE[key]


def test_a_case_decided_none_must_declare_no_id(tmp_path):
    res = check(tmp_path, {**AFTER, PY_B: ["REQ-1"]})
    assert [o.describe() for o in res.offences] == [
        f"{PY_B}: a decided case does not declare exactly its owner (expected [], found [REQ-1])"
    ]


@pytest.mark.parametrize("baseline", [True, False], ids=["baseline", "worksheet"])
def test_a_decided_case_that_lost_its_tags(tmp_path, baseline):
    """A tagged case that ends with no id lost its tag (found [], expected
    its owner), while an untagged case (no id before, none after) counts only
    through verified_by: its owner is a model edit, not an offence. Before
    is the baseline when given, else the worksheet's group tags."""
    res = check(tmp_path, {**AFTER, PY_A: []}, baseline=baseline)
    assert [o.describe() for o in res.offences] == [
        f"{PY_A}: a decided case does not declare exactly its owner (expected [REQ-1], found [])"
    ]
    assert res.untagged == [NODE_U]
    # An untagged case that gains another id than its owner is an offence.
    res = check(tmp_path, {**AFTER, NODE_U: ["REQ-5"]}, baseline=baseline)
    assert [o.key for o in res.offences] == [NODE_U] and not res.untagged
    # ...and one that gains exactly its owner verifies.
    res = check(tmp_path, {**AFTER, NODE_U: ["REQ-4"]}, baseline=baseline)
    assert res.ok and res.decided == 5 and not res.untagged


def test_a_decided_case_missing_from_the_evidence(tmp_path):
    node = [NODE_A, NODE_U]  # every case of //web:clocksync_test: the target did not run
    res = check(tmp_path, AFTER, baseline=False, drop=node)
    assert [o.describe() for o in res.offences] == [
        f"{NODE_A}: a decided case has no result in the evidence (expected [REQ-5], found no result)",
        f"{NODE_U}: a decided case has no result in the evidence (expected [REQ-4], found no result)",
    ]
    # Tolerated with allow_missing (a HITL target CI does not run), with or
    # without a baseline: every case of the target, decided or not.
    res = check(tmp_path, AFTER, baseline=False, allow_missing=True, drop=node)
    assert res.ok and res.missing == node and res.decided == 3
    res = check(tmp_path, AFTER, allow_missing=True, drop=node)
    assert res.ok and res.missing == node and res.unchanged == 2
    res = check(tmp_path / "cc", AFTER, allow_missing=True, drop=[CC_A, CC_OPEN])
    assert res.ok and res.missing == [CC_OPEN, CC_A]
    # Without it, a case the baseline has disappeared.
    res = check(tmp_path, AFTER, drop=node)
    assert [o.describe() for o in res.offences] == [
        f"{NODE_A}: a case of the baseline has no result in the evidence (expected [REQ-5], found no result)",
        f"{NODE_U}: a case of the baseline has no result in the evidence (expected [REQ-4], found no result)",
    ]


@pytest.mark.parametrize("baseline", [True, False], ids=["baseline", "worksheet"])
def test_allow_missing_never_excuses_a_case_missing_from_a_target_that_ran(tmp_path, baseline):
    """A decided case gone from a target that ran was renamed or lost (a
    mangled parametrize, a test dropped by the rewrite): an offence even with
    allow_missing, and its replacement's ids are not what excuses it."""
    renamed = CaseKey(PY, PY_A.path + "_renamed")
    res = check(tmp_path, {**AFTER, renamed: ["REQ-1", "REQ-2"]}, baseline=baseline, allow_missing=True, drop=[PY_A])
    reason = "a case of the baseline" if baseline else "a decided case"
    assert [o.describe() for o in res.offences] == [
        f"{PY_A}: {reason} has no result in the evidence, though its target ran (expected [REQ-1], found no result)"
    ]
    assert not res.missing
    # An undecided case of a target that ran, likewise.
    res = check(tmp_path, AFTER, allow_missing=True, drop=[CC_OPEN])
    assert [o.key for o in res.offences] == [CC_OPEN] and not res.missing
    # A target that ran only as a whole (a target-scope result) ran.
    whole = CaseKey(NODE, "[target]")
    res = check(tmp_path, {**AFTER, whole: []}, baseline=False, allow_missing=True, drop=[NODE_A, NODE_U])
    assert [o.key for o in res.offences] == [NODE_A, NODE_U], [o.describe() for o in res.offences]


_SYNTHETIC = (
    "<testsuites><testsuite name='config_test' tests='1' failures='0' errors='1'>"
    "<testcase name='config_test' status='run' duration='0' time='0'>"
    "<error message='exited with error code 5'></error></testcase>"
    "<system-out>Generated test.log (if the file is not UTF-8, then this may be unreadable):"
    "</system-out></testsuite></testsuites>"
)


@pytest.mark.parametrize(
    "report",
    [
        "<testsuites><testsuite name='config_test' tests='0'></testsuite></testsuites>",
        "<testsuite name='pytest' errors='0' failures='0' skipped='0' tests='0'/>",
        "<testsuites/>",
        _SYNTHETIC,
    ],
    ids=["empty-testsuite", "pytest-collected-nothing", "empty-testsuites", "synthetic-only"],
)
@pytest.mark.parametrize("baseline", [True, False], ids=["baseline", "worksheet"])
def test_a_target_with_a_result_file_ran(tmp_path, report, baseline):
    """A target whose test.xml is in the evidence ran, even when the file
    holds no testcase (pytest collected nothing) or only Bazel's synthetic
    whole-run result: allow_missing does not excuse its missing cases."""
    root = evidence(tmp_path / "new", AFTER, drop=[PY_A, PY_B, PY_C])
    path = os.path.join(root, "bazel-testlogs", "app/tests", "config_test", "test.xml")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(report)
    new = ingest.collect([root])
    old = ingest.collect([evidence(tmp_path / "old", BEFORE)]) if baseline else None
    res = migrate.verify(worksheet(), new, old, allow_missing=True)
    expected = [PY_A, PY_C, PY_B] if baseline else [PY_A, PY_B]
    assert sorted(o.key for o in res.offences) == sorted(expected), [o.describe() for o in res.offences]
    assert all("though its target ran" in o.reason for o in res.offences)
    assert not res.ok and not res.missing


@pytest.mark.parametrize("baseline", [True, False], ids=["baseline", "worksheet"])
def test_a_target_with_no_result_file_did_not_run(tmp_path, baseline):
    """No result file at all for the target (CI does not run it): excused by
    allow_missing, an offence without it."""
    new = ingest.collect([evidence(tmp_path / "new", AFTER, drop=[PY_A, PY_B, PY_C])])
    old = ingest.collect([evidence(tmp_path / "old", BEFORE)]) if baseline else None
    res = migrate.verify(worksheet(), new, old, allow_missing=True)
    assert res.ok and set(res.missing) == ({PY_A, PY_B, PY_C} if baseline else {PY_A, PY_B})
    assert not migrate.verify(worksheet(), new, old).ok


def test_untagged_cases_with_a_model(tmp_path):
    """With a model, a decided case that declares no id is owned through
    verified_by only when its target's claimers are exactly its owner; else
    it is pending a model edit. A tagged case whose target another
    requirement still claims keeps counting toward it: listed, not refused."""
    res = check(tmp_path, AFTER, model=model({"REQ-4": [NODE], "REQ-5": [NODE]}))
    assert res.ok and res.untagged == [] and res.pending == [(NODE_U, ["REQ-4", "REQ-5"])]
    assert res.also_claimed == [(NODE_A, ["REQ-4"])]
    res = check(tmp_path, AFTER, model=model({"REQ-4": [NODE]}))
    assert res.ok and res.untagged == [NODE_U] and res.pending == []
    assert res.also_claimed == [(NODE_A, ["REQ-4"])]
    res = check(tmp_path, AFTER, model=model({"REQ-5": [NODE]}))
    assert res.pending == [(NODE_U, ["REQ-5"])] and res.also_claimed == []
    # Without a model, its owner is not checked.
    res = check(tmp_path, AFTER)
    assert res.untagged == [NODE_U] and res.pending == [] and res.also_claimed == []


@pytest.mark.parametrize("key", [PY_C, CC_OPEN], ids=["undecided", "open"])
def test_an_undecided_case_whose_ids_changed(tmp_path, key):
    res = check(tmp_path, {**AFTER, key: ["REQ-1"]})
    assert [o.describe() for o in res.offences] == [
        f"{key}: an undecided case's ids changed (expected {_ids(BEFORE[key])}, found [REQ-1])"
    ]
    # Order is not identity: the same ids in another order are unchanged.
    res = check(tmp_path, {**AFTER, key: list(reversed(BEFORE[key]))})
    assert res.ok
    # Without a baseline there is nothing to compare an undecided case with.
    assert check(tmp_path, {**AFTER, key: ["REQ-1"]}, baseline=False).ok


def test_an_undecided_case_that_disappeared(tmp_path):
    res = check(tmp_path, AFTER, drop=[PY_C])
    assert [o.describe() for o in res.offences] == [
        f"{PY_C}: a case of the baseline has no result in the evidence (expected [REQ-3], found no result)"
    ]
    # A case only the new evidence has is not an offence.
    extra = CaseKey(PY, "app.tests.test_config::test_new")
    res = check(tmp_path, {**AFTER, extra: ["REQ-9"]})
    assert res.ok and res.new == 1


def _ids(ids):
    return "[" + ", ".join(ids) + "]"


def test_cli(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sheet = tmp_path / "attribution.rrplan.json"
    sheet.write_text(json.dumps(worksheet()), encoding="utf-8")
    old = evidence(tmp_path / "old", BEFORE)
    good = evidence(tmp_path / "good", AFTER)
    bad = evidence(tmp_path / "bad", {**AFTER, PY_A: ["REQ-1", "REQ-2"], PY_C: []}, drop=[CC_A])

    def run(*argv):
        rc = cli.main(["migrate", "verify", "--worksheet", str(sheet), *argv])
        out, err = capsys.readouterr()
        return rc, out, err

    rc, out, err = run("--evidence", good, "--baseline", old)
    assert rc == 0 and out == "", out
    assert (
        "ok: 4 decided case(s) declare exactly their owner; 1 declare no id (count only through verified_by); "
        "2 undecided case(s) kept their ids; 0 case(s) are new"
    ) in err
    assert "note: 1 decided case(s) declare no id, before and after" in err and f"\n  {NODE_U}\n" in err
    assert "their owner is not checked (verify reads declared ids only" in err

    rc, out, err = run("--evidence", bad, "--baseline", old)
    assert rc == 1 and "3 case(s) contradict the worksheet" in err
    assert out.splitlines() == [
        f"{PY_A}: a decided case does not declare exactly its owner (expected [REQ-1], found [REQ-1, REQ-2])",
        f"{PY_C}: an undecided case's ids changed (expected [REQ-3], found [])",
        f"{CC_A}: a case of the baseline has no result in the evidence (expected [REQ-2], found no result)",
    ]

    rc, out, err = run("--evidence", bad, "--allow-missing")
    assert rc == 1 and out.splitlines() == [
        f"{PY_A}: a decided case does not declare exactly its owner (expected [REQ-1], found [REQ-1, REQ-2])",
        f"{CC_A}: a decided case has no result in the evidence, though its target ran (expected [REQ-2], found no "
        "result)",
    ]
    hitl = evidence(tmp_path / "hitl", AFTER, drop=[CC_A, CC_OPEN])
    rc, out, err = run("--evidence", hitl, "--baseline", old, "--allow-missing")
    assert rc == 0 and out == "", out
    assert (
        "warning: 2 case(s) of 1 target(s) with no result file in the evidence (--allow-missing: not run there): "
        f"not verified ({CC})\n  {CC_OPEN}\n  {CC_A}\n"
    ) in err

    # With the model, the untagged case's owner is checked through verified_by.
    req = tmp_path / "req"
    req.mkdir()
    (req / "reqs.yaml").write_text(
        "user_needs: [{id: UN-1, title: n}]\nrequirements:\n"
        + "".join(f"  - {{id: REQ-{i}, title: r, satisfies: [UN-1]}}\n" for i in (1, 2, 3))
        + "".join(f"  - {{id: REQ-{i}, title: r, satisfies: [UN-1], verified_by: ['{NODE}']}}\n" for i in (4, 5)),
        encoding="utf-8",
    )
    rc, out, err = run("--evidence", good, "--model", str(req))
    assert rc == 0 and out == "", err
    assert "1 pending a verified_by edit" in err and f"\n  {NODE_U}: counts toward [REQ-4, REQ-5]\n" in err
    assert f"\n  {NODE_A}: also [REQ-4]\n" in err

    # Evidence whose files are not under a bazel-testlogs / testlogs directory
    # names no build target: every case would read as missing.
    import shutil

    shutil.copytree(os.path.join(good, "bazel-testlogs"), str(tmp_path / "ci-testlogs"))
    for flag in ("--evidence", "--baseline"):
        argv = ["--evidence", good, "--baseline", old]
        argv[argv.index(flag) + 1] = str(tmp_path / "ci-testlogs")
        rc, out, err = run(*argv)
        assert rc == 2 and out == "", out
        assert f"rr migrate verify: {flag}: the worksheet's cases belong to build targets" in err
        assert "keep the evidence under a directory named bazel-testlogs or testlogs" in err
    # The guide's layout keys them.
    shutil.copytree(os.path.join(good, "bazel-testlogs"), str(tmp_path / "ci" / "bazel-testlogs"))
    rc, out, err = run("--evidence", str(tmp_path / "ci" / "bazel-testlogs"), "--baseline", old)
    assert rc == 0 and out == "", err

    # Usage errors: no evidence, a worksheet that does not read or does not check.
    rc, _, err = run("--evidence", str(tmp_path / "nowhere"))
    assert rc == 2 and "rr migrate verify: no evidence found in" in err
    rc, _, err = run("--evidence", good, "--baseline", str(tmp_path / "nowhere"))
    assert rc == 2 and "rr migrate verify --baseline: no evidence found in" in err
    doc = worksheet()
    doc["groups"][0]["cases"][0]["owner"] = "REQ-1, REQ-2"
    sheet.write_text(json.dumps(doc), encoding="utf-8")
    rc, _, err = run("--evidence", good)
    assert rc == 2 and "names more than one id" in err
    rc = cli.main(["migrate", "verify", "--worksheet", str(tmp_path / "none.rrplan"), "--evidence", good])
    assert rc == 2 and "cannot read worksheet" in capsys.readouterr().err
