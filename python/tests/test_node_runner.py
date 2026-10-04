# SPDX-License-Identifier: AGPL-3.0-or-later
"""rr_node_test's runner (js/) on the tests/node fixtures, outside Bazel.

The entry point template is expanded here the way ``rr/private/node.bzl``
expands it, run with a real ``node`` (``$RR_NODE``, else the one on ``PATH``)
and its JUnit read back with the JUnit ingestor. CI runs this module on Node
18, 20, 22 and 24 (with ``$RR_NODE_MIN`` set, so a missing node fails rather
than skips), so a change in node:test's event model fails here first.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import textwrap
import time
from xml.etree import ElementTree as ET

import pytest

from rules_requirements.hooks.junit_writer import xml_safe
from rules_requirements.ingest import TestCase
from rules_requirements.ingest.junit import JUnitIngestor

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_NODE = os.environ.get("RR_NODE") or shutil.which("node")


def _repo_file(rel):
    for base in (_ROOT, os.environ.get("TEST_SRCDIR", "") + "/_main"):
        path = os.path.join(base, rel)
        if os.path.exists(path):
            return path
    pytest.skip(f"{rel} not available")


def _read(rel):
    with open(_repo_file(rel), encoding="utf-8") as fh:
        return fh.read()


def _node_major():
    if not _NODE:
        return 0
    try:
        out = subprocess.run([_NODE, "--version"], capture_output=True, text=True, check=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return 0
    m = re.match(r"\s*v?(\d+)\.", out)
    return int(m.group(1)) if m else 0


_MAJOR = _node_major()
needs_node = pytest.mark.skipif(not _MAJOR, reason="node is not installed")
needs_reporters = pytest.mark.skipif(_MAJOR < 20, reason="needs node >= 20 (--test-reporter)")


def _expand(template, values):
    """Like node.bzl: every ``"{{NAME}}"`` literal becomes a JSON value."""
    for key, value in values.items():
        template = template.replace(f'"{{{{{key}}}}}"', json.dumps(value))
    return template


class Run:
    def __init__(self, code, xml, stdout, stderr, plain_code):
        self.code, self.xml, self.stdout, self.stderr = code, xml, stdout, stderr
        self.plain_code = plain_code  # what `node <file>` alone exits with

    @property
    def cases(self) -> list[TestCase]:
        return list(JUnitIngestor().ingest(self.xml))

    def by_path(self):
        return {f"{c.classname}::{c.name}": c for c in self.cases}

    def paths(self):
        return [f"{c.classname}::{c.name}" for c in self.cases]


@pytest.fixture
def run_fixture(tmp_path):
    """run_fixture(name or (name, source), args=(), level="", env=None, xml=True,
    helpers=(), bazel_layout=False) -> Run.

    ``helpers`` are further tests/node files copied next to the test;
    ``bazel_layout`` puts the runfiles tree where rules_js has it, inside
    ``bazel-out/<cfg>/bin``. ``run_fixture.prepare(...)`` (same arguments)
    returns ``(argv, env, cwd, xml_path)`` without running anything.
    """

    template = _read("js/node_test_main.cjs.tpl")
    reporter = _repo_file("js/rr_node_reporter.mjs")

    def prepare(fixture, args=(), level="", env=None, xml=True, helpers=(), bazel_layout=False):
        if isinstance(fixture, tuple):
            fixture, source = fixture
        else:
            source = None
        name = f"{fixture}_test"
        root = tmp_path
        if bazel_layout:
            root = tmp_path / "bazel-out" / "k8-fastbuild" / "bin" / "tests" / "node" / f"{name}_" / f"{name}.runfiles"
        pkg = root / "_main" / "tests" / "node"
        pkg.mkdir(parents=True, exist_ok=True)
        (root / "_main" / "js").mkdir(exist_ok=True)
        shutil.copy(_repo_file("js/verifies.cjs"), root / "_main" / "js" / "verifies.cjs")
        if source is not None:
            (pkg / f"{fixture}.test.cjs").write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
        else:
            shutil.copy(_repo_file(f"tests/node/{fixture}.test.cjs"), pkg / f"{fixture}.test.cjs")
        for helper in helpers:
            shutil.copy(_repo_file(f"tests/node/{helper}"), pkg / helper)
        shutil.copy(reporter, pkg / f"{name}.rr_node_reporter.mjs")
        main = pkg / f"{name}.rr_node_main.cjs"
        main.write_text(
            _expand(
                template,
                {
                    "ARGS": json.dumps(list(args)),
                    "LEVEL": level,
                    "REPORTER_REL": f"{name}.rr_node_reporter.mjs",
                    "TARGET": f"//tests/node:{name}",
                    "TEST_REL": f"{fixture}.test.cjs",
                    "TEST_RUNFILES": f"_main/tests/node/{fixture}.test.cjs",
                    "TEST_SHORT": f"tests/node/{fixture}.test.cjs",
                    "VERIFIES_RUNFILES": "_main/js/verifies.cjs",
                },
            ),
            encoding="utf-8",
        )
        out = tmp_path / f"{name}.xml"
        full_env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "RUNFILES_DIR": str(root),
            "TEST_TMPDIR": str(tmp_path),
            **({"XML_OUTPUT_FILE": str(out)} if xml else {}),
            **(env or {}),
        }
        return [_NODE, str(main)], full_env, root / "_main", str(out)

    def run(fixture, args=(), **kw):
        argv, full_env, cwd, out = prepare(fixture, args, **kw)
        proc = subprocess.run(argv, cwd=cwd, env=full_env, capture_output=True, text=True, timeout=120, check=False)
        name = fixture[0] if isinstance(fixture, tuple) else fixture
        # `node <file>` alone, outside rr_node_test (no RR_NODE_VERIFIES).
        plain = subprocess.run(
            [_NODE, str(cwd / "tests" / "node" / f"{name}.test.cjs"), *args],
            cwd=cwd,
            env={"PATH": full_env["PATH"]},
            capture_output=True,
            timeout=120,
            check=False,
        )
        return Run(proc.returncode, out, proc.stdout, proc.stderr, plain.returncode)

    run.prepare = prepare  # type: ignore[attr-defined]
    return run


def test_template_placeholders_match_the_rule():
    template = _read("js/node_test_main.cjs.tpl")
    rule = _read("rr/private/node.bzl")
    placeholders = set(re.findall(r'"\{\{([A-Z_]+)\}\}"', template))
    assert placeholders == {
        "ARGS",
        "LEVEL",
        "REPORTER_REL",
        "TARGET",
        "TEST_REL",
        "TEST_RUNFILES",
        "TEST_SHORT",
        "VERIFIES_RUNFILES",
    }
    for name in placeholders:
        assert f'"{name}":' in rule, f"node.bzl does not fill {name}"


@needs_reporters
def test_nesting_subtests_and_concurrency(run_fixture):
    r = run_fixture("nesting")
    assert r.code == 0, r.stderr
    # One case per leaf; describes and parent tests are scopes, not cases.
    assert r.paths() == [
        "nesting::top level",
        "nesting > outer::in a describe",
        "nesting > outer > inner::two levels down",
        "nesting > parent with subtests::sub a",
        "nesting > parent with subtests > sub b::sub sub",
        "nesting > concurrent::slow",
        "nesting > concurrent::fast",
        "nesting > concurrent::medium",
    ]
    cases = r.by_path()
    assert all(c.status == "passed" for c in r.cases)
    # Under `concurrency`, each diagnostic still lands on its own test.
    for name in ("slow", "fast", "medium"):
        assert cases[f"nesting > concurrent::{name}"].requirements == (f"REQ-{name}",)
    assert cases["nesting::top level"].requirements == ()
    assert cases["nesting::top level"].properties == {"rr.file": "tests/node/nesting.test.cjs"}
    # spec still writes the human-readable log.
    assert "top level" in r.stdout
    root = ET.parse(r.xml).getroot().find("testsuite")
    assert root is not None
    assert (root.get("name"), root.get("tests"), root.get("failures")) == ("//tests/node:nesting_test", "8", "0")


@needs_reporters
def test_skip_and_todo_are_skipped(run_fixture):
    r = run_fixture("skip_todo")
    assert r.code == 0
    assert [(c.name, c.status, c.message) for c in r.cases] == [
        ("runs", "passed", ""),
        ("skipped with a reason", "skipped", "no hardware"),
        ("skipped", "skipped", ""),
        ("todo with a reason", "skipped", "not written yet"),
        ("skipped at run time", "skipped", "decided inside the test"),
        ("todo at run time", "skipped", "marked inside the test"),
        ("skipped suite", "skipped", ""),
        ("skipped suite with a reason", "skipped", "no bench"),
    ]
    # Node reports nothing of a skipped describe's tests: the describe is the
    # case (in its own chain), so what it claims reads skipped, not missing.
    assert r.paths()[-2:] == ["skip_todo::skipped suite", "skip_todo > outer::skipped suite with a reason"]
    assert all("never reported" not in c.name for c in r.cases)


@needs_reporters
def test_an_empty_describe_is_no_case(run_fixture):
    r = run_fixture(
        (
            "empty_suite",
            """
            const { describe, test } = require("node:test");
            describe("no tests yet", () => {});
            test("a", () => {});
            """,
        )
    )
    assert r.code == 0
    assert r.paths() == ["empty_suite::a"]


@needs_reporters
def test_duplicate_names_stay_separate_cases(run_fixture):
    r = run_fixture("duplicates")
    assert r.code == r.plain_code == 1
    assert [(c.name, c.status) for c in r.cases] == [("same name", "passed"), ("same name", "failed")]
    assert "the second one fails" in r.cases[1].message


@needs_reporters
def test_diagnostics_become_properties_of_their_own_case(run_fixture):
    r = run_fixture("diagnostics")
    assert r.code == 1  # two cases fail on purpose
    cases = r.by_path()
    raw = cases["diagnostics::raw diagnostics"]
    assert (raw.requirements, raw.level, raw.artifact) == (("REQ-1",), "hil", {"board_rev": "C"})
    helper = cases["diagnostics::the verifies helper"]
    assert (helper.requirements, helper.level, helper.status) == (("REQ-2",), "sil", "passed")
    second = cases["diagnostics::verifies refuses a second id"]
    assert second.status == "failed"
    assert second.requirements == ("REQ-3",)
    assert "already verifies REQ-3 [RR-E101]" in second.message
    listed = cases["diagnostics::verifies refuses an id list"]
    assert (listed.status, listed.requirements) == ("failed", ())
    assert "[RR-E104]" in listed.message
    assert cases["diagnostics::no diagnostics"].requirements == ()
    assert cases["diagnostics > after a sibling's diagnostics::first"].requirements == ("REQ-7",)
    assert cases["diagnostics > after a sibling's diagnostics::second"].requirements == ()
    # A diagnostic from a hook is never guessed onto a case.
    assert "uncorrelated rr diagnostic 'rr.requirement=REQ-from-a-hook'" in r.stderr
    assert all("REQ-from-a-hook" not in c.requirements for c in r.cases)
    # Not even a root after() hook's, which node reports right after the last
    # case, at nesting 0.
    assert "uncorrelated rr diagnostic 'rr.requirement=REQ-from-a-root-after-hook'" in r.stderr
    assert cases["diagnostics > after a sibling's diagnostics::second"].requirements == ()
    # The documented import still loads the file outside rr_node_test.
    assert r.plain_code == 0


@needs_reporters
def test_a_root_after_hook_diagnostic_in_a_one_line_file_is_no_case(run_fixture):
    # Minified or bundled: the hook and the test share line 1 (and nesting 0).
    r = run_fixture(
        (
            "one_line",
            'const { test, after } = require("node:test"); test("only case", () => {}); '
            'after((t) => { t.diagnostic("rr.requirement=REQ-hook"); });\n',
        )
    )
    assert r.code == 0
    assert [(c.name, c.requirements) for c in r.cases] == [("only case", ())]
    assert "uncorrelated rr diagnostic 'rr.requirement=REQ-hook'" in r.stderr


@needs_reporters
def test_one_line_file_keeps_each_tests_own_diagnostics(run_fixture):
    r = run_fixture(
        (
            "one_line_tests",
            'const { test } = require("node:test"); '
            'test("a", (t) => { t.diagnostic("rr.requirement=REQ-a"); }); '
            'test("b", (t) => { t.diagnostic("rr.requirement=REQ-b"); });\n',
        )
    )
    assert r.code == 0
    assert [(c.name, c.requirements) for c in r.cases] == [("a", ("REQ-a",)), ("b", ("REQ-b",))]


def _reporter_rows(tmp_path, events):
    """Feeds a synthetic node:test event stream to the reporter; its rows."""
    out = tmp_path / "rows.jsonl"
    driver = tmp_path / "drive.mjs"
    driver.write_text(
        "import fs from 'node:fs';\n"
        "import { pathToFileURL } from 'node:url';\n"
        "const { default: rr } = await import(pathToFileURL(process.argv[2]).href);\n"
        "const events = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));\n"
        "async function* source() { for (const e of events) yield e; }\n"
        "for await (const _ of rr(source())) {}\n",
        encoding="utf-8",
    )
    stream = tmp_path / "events.json"
    stream.write_text(json.dumps(events), encoding="utf-8")
    subprocess.run(
        [_NODE, str(driver), _repo_file("js/rr_node_reporter.mjs"), str(stream)],
        env={"PATH": os.environ.get("PATH", ""), "RR_CASES_OUT": str(out), "RR_TEST_FILE": "/w/f.test.cjs"},
        check=True,
        timeout=60,
    )
    return [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]


_F = "/w/f.test.cjs"


def _ev(kind, nesting=0, line=2, column=1, file=_F, **data):
    return {"type": kind, "data": {"nesting": nesting, "line": line, "column": column, "file": file, **data}}


def _case(name="a", nesting=0, line=2, column=1):
    return [
        _ev("test:start", nesting, line, column, name=name),
        _ev("test:pass", nesting, line, column, name=name, details={"duration_ms": 1}),
    ]


def _diag(nesting=0, line=2, column=1, file=_F):
    return _ev("test:diagnostic", nesting, line, column, file, message="rr.requirement=REQ-X")


# Each of these diagnostics follows case `a` (nesting 0, line 2, column 1) but
# is not its own: every check of the correlation is needed.
_NOT_ITS_OWN = {
    "other nesting": [_diag(nesting=1)],
    "other line": [_diag(line=3)],
    "other column": [_diag(column=40)],
    "other file": [_diag(file="/w/helper.cjs")],
    "no file": [_diag(file=None)],
    "after a plan (a root after hook)": [_ev("test:plan", 0, 2, 1, count=1), _diag()],
    "after the next test started": [_ev("test:start", 0, 5, 1, name="b"), _diag()],
    "after a describe passed": [
        _ev("test:start", 0, 7, 1, name="d"),
        _ev("test:pass", 0, 7, 1, name="d", details={"type": "suite"}),
        _diag(),
    ],
    "after a root hook failed": [
        _ev("test:fail", 0, 1, 1, name=_F, details={"error": {"message": "hook broke"}}),
        _diag(),
    ],
}


@needs_reporters
@pytest.mark.parametrize("events", list(_NOT_ITS_OWN.values()), ids=list(_NOT_ITS_OWN))
def test_reporter_correlates_a_diagnostic_only_with_its_own_case(tmp_path, events):
    rows = _reporter_rows(tmp_path, _case() + events)
    assert [r["kind"] for r in rows if r["kind"] in ("diag", "warning")] == ["warning"], rows


@needs_reporters
def test_reporter_correlates_a_diagnostic_with_its_case(tmp_path):
    rows = _reporter_rows(tmp_path, _case() + [_diag()])
    assert [(r["kind"], r.get("case")) for r in rows] == [("start", None), ("case", None), ("diag", 0), ("end", None)]


@needs_reporters
def test_failures_outside_cases_are_target_scope_errors(run_fixture):
    r = run_fixture("failures")
    assert r.code == 1
    assert [(p, c.status, c.properties.get("rr.scope", "")) for p, c in r.by_path().items()] == [
        ("failures::fails", "failed", ""),
        ("failures::passes", "passed", ""),
        ("failures > before hook fails::never runs", "failed", ""),
        ("failures > before hook fails::<hooks>", "error", "target"),
        ("failures > parent fails after its subtests::sub passes", "passed", ""),
        ("failures > parent fails after its subtests::<hooks>", "error", "target"),
        # The parent of a failing subtest only reports "a subtest failed".
        ("failures > parent of a failing subtest::sub fails", "failed", ""),
    ]
    cases = r.by_path()
    assert cases["failures > before hook fails::<hooks>"].message == "Error: setup broke"
    assert cases["failures > parent fails after its subtests::<hooks>"].message == "Error: parent body broke"
    assert cases["failures::fails"].message.startswith("AssertionError")
    # The full stack is the failure's text; the attribute is its first line.
    failure = ET.parse(r.xml).getroot().find(".//testcase[@name='fails']/failure")
    assert failure is not None and "\n" in (failure.text or "")


@needs_reporters
def test_late_unhandled_rejection_is_an_exit_status_error(run_fixture):
    r = run_fixture("late_rejection")
    assert r.code == r.plain_code == 1
    assert [(c.name, c.status) for c in r.cases] == [("passes", "passed"), ("<exit-status>", "error")]
    exit_case = r.cases[1]
    assert exit_case.classname == "late_rejection"
    assert exit_case.properties == {"rr.scope": "target", "rr.file": "tests/node/late_rejection.test.cjs"}
    assert "node exited 1 although no test failed" in exit_case.message


@needs_reporters
def test_load_error_keeps_the_exit_code(run_fixture):
    r = run_fixture("load_error")
    assert r.code == r.plain_code == 7  # node's code for an uncaught exception at load
    assert [(c.classname, c.name, c.status) for c in r.cases] == [("load_error", "<load>", "error")]
    assert r.cases[0].properties["rr.scope"] == "target"
    assert "node exited 7 before reporting any test" in r.cases[0].message
    assert "boom at load" in r.stderr  # node's own report reaches the log


@needs_reporters
def test_failing_root_after_hook_taints_a_passing_run(run_fixture):
    r = run_fixture("root_after_hook")
    # Node 22+ exits 0 here, so Bazel passes the target (Node 20 exits 1)...
    assert r.code == r.plain_code == (1 if _MAJOR == 20 else 0)
    assert [(c.name, c.status) for c in r.cases] == [("passes", "passed"), ("<file>", "error")]
    assert r.cases[1].properties["rr.scope"] == "target"  # ...the report does not
    assert r.cases[1].message == "Error: after hook fails"


@needs_reporters
def test_zero_tests_is_an_empty_suite(run_fixture):
    r = run_fixture("zero_tests")
    assert r.code == 0
    assert r.cases == []
    suite = ET.parse(r.xml).getroot().find("testsuite")
    assert suite is not None and suite.get("tests") == "0" and not list(suite)


@needs_reporters
def test_baked_args_and_default_level(run_fixture):
    r = run_fixture("args", args=["--mode", "fast"], level="sil")
    assert r.code == 0, r.stdout
    assert [(c.name, c.status, c.level) for c in r.cases] == [("sees its arguments", "passed", "sil")]


@needs_reporters
def test_a_declared_level_beats_the_default(run_fixture):
    r = run_fixture("diagnostics", level="simulation")
    cases = r.by_path()
    assert cases["diagnostics::raw diagnostics"].level == "hil"
    assert cases["diagnostics::no diagnostics"].level == "simulation"


@needs_reporters
def test_several_ids_are_all_written_with_a_warning(run_fixture):
    r = run_fixture(
        (
            "multi",
            """
            const { test } = require("node:test");
            test("names two", (t) => {
              t.diagnostic("rr.requirement=REQ-1");
              t.diagnostic("rr.requirement=REQ-2");
            });
            test("names a list", (t) => {
              t.diagnostic("rr.requirement=REQ-3, REQ-4");
            });
            """,
        )
    )
    assert r.code == 0
    # Never a silent pick: ingest sees every id (and the stderr says why not).
    assert [c.requirements for c in r.cases] == [("REQ-1", "REQ-2"), ("REQ-3", "REQ-4")]
    assert "multi::names two names REQ-1, REQ-2; a test case verifies at most one requirement [RR-E101]" in r.stderr
    props = ET.parse(r.xml).getroot().findall(".//testcase[@name='names two']/properties/property[@name='requirement']")
    assert [p.get("value") for p in props] == ["REQ-1", "REQ-2"]


@needs_reporters
def test_xml_escaping_matches_junit_writer(run_fixture):
    names = [
        'quote " amp & lt < gt >',
        "bell \x07 and nul \x00",
        "tab\tnewline\nreturn\r",
        "emoji \U0001f600",
        "nonchar \ufffe",
        "lone surrogate \ud800",
    ]
    source = "const { test } = require('node:test');\n" + "".join(
        f"test({json.dumps(n)}, () => {{}});\n" for n in names
    )
    r = run_fixture(("escaping", source))
    assert r.code == 0
    assert [c.name for c in r.cases] == [xml_safe(n) for n in names]


@needs_reporters
def test_a_crash_keeps_the_signal_exit_code(run_fixture):
    r = run_fixture(
        (
            "crash",
            """
            const { test } = require("node:test");
            test("dies", () => { process.kill(process.pid, "SIGKILL"); });
            """,
        )
    )
    assert r.code == 128 + 9
    assert r.plain_code == -9  # (subprocess's spelling of "killed by signal 9")
    assert [(c.name, c.status) for c in r.cases] == [("<load>", "error")]
    assert "was killed by SIGKILL" in r.cases[0].message


@needs_reporters
def test_without_xml_output_file_it_only_runs(run_fixture, tmp_path):
    r = run_fixture("duplicates", xml=False)
    assert r.code == 1
    assert not os.path.exists(r.xml)
    assert not list(tmp_path.glob("rr_node_test-*"))  # its scratch directory is gone


@needs_node
def test_plain_mode_writes_one_synthetic_result(run_fixture):
    # What Node < 20 gets (no --test-reporter), forced with RR_NODE_TEST_PLAIN.
    ok = run_fixture("nesting", env={"RR_NODE_TEST_PLAIN": "1"})
    assert ok.code == 0
    assert [(c.classname, c.name, c.status, c.properties) for c in ok.cases] == [
        ("//tests/node:nesting_test", "nesting_test", "passed", {"rr.synthetic": "true"})
    ]
    bad = run_fixture("load_error", env={"RR_NODE_TEST_PLAIN": "1"})
    assert bad.code == 7
    assert [(c.name, c.status) for c in bad.cases] == [("load_error_test", "failed")]
    assert "node exited 7" in bad.cases[0].message


@needs_node
@pytest.mark.skipif(_MAJOR >= 20, reason="node >= 20 reports per case")
def test_node_before_20_falls_back_to_one_synthetic_result(run_fixture):
    r = run_fixture("nesting")
    assert r.code == 0
    assert [(c.name, c.status, c.properties) for c in r.cases] == [("nesting_test", "passed", {"rr.synthetic": "true"})]


@needs_reporters
@pytest.mark.parametrize("bazel_layout", [False, True], ids=["runfiles", "rules_js-bin-tree"])
def test_rr_file_of_a_test_defined_in_a_helper_module(run_fixture, bazel_layout):
    # Under rules_js the runfiles tree lies inside bazel-out/<cfg>/bin: the
    # helper's rr.file is its workspace path, not one inside the runfiles.
    r = run_fixture("uses_helper", helpers=["helper_cases.cjs"], bazel_layout=bazel_layout)
    assert r.code == 0, r.stderr
    assert [(c.name, c.requirements, c.properties["rr.file"]) for c in r.cases] == [
        ("own", (), "tests/node/uses_helper.test.cjs"),
        ("defined in a helper", ("REQ-9",), "tests/node/helper_cases.cjs"),
    ]


@needs_reporters
def test_a_failing_root_before_hook_fails_the_tests_not_the_file(run_fixture):
    # As docs/guides/hooks.md says: no `<file>` case; each top-level test
    # fails, and each describe gets a `<hooks>` error with the hook's message.
    r = run_fixture(
        (
            "root_before",
            """
            const { before, describe, test } = require("node:test");
            before(() => { throw new Error("root before broke"); });
            test("a", () => {});
            describe("d", () => { test("b", () => {}); });
            """,
        )
    )
    assert r.code == r.plain_code == 1
    assert [(p, c.status) for p, c in r.by_path().items()] == [
        ("root_before::a", "failed"),
        ("root_before > d::b", "failed"),
        ("root_before > d::<hooks>", "error"),
    ]
    assert r.by_path()["root_before::a"].message == "Error: root before broke"
    assert r.by_path()["root_before > d::<hooks>"].message == "Error: root before broke"


@needs_reporters
def test_a_thrown_non_error_is_shown_not_object_object(run_fixture):
    r = run_fixture(
        (
            "non_error",
            """
            const { test } = require("node:test");
            test("object", () => { throw { code: 1, why: "plain object" }; });
            test("string", () => { throw "a string"; });
            """,
        )
    )
    assert r.code == 1
    assert [c.message for c in r.cases] == ["{ code: 1, why: 'plain object' }", "a string"]


@needs_reporters
@pytest.mark.parametrize("broken", ["XML_OUTPUT_FILE", "TEST_TMPDIR"])
def test_reporting_trouble_never_changes_the_exit_code(run_fixture, tmp_path, broken):
    missing = str(tmp_path / "no" / "such" / "dir")
    env = {broken: missing + "/test.xml" if broken == "XML_OUTPUT_FILE" else missing}
    r = run_fixture("nesting", env=env)
    assert r.code == 0, r.stderr
    assert "rr_node_test: warning:" in r.stderr
    assert "top level" in r.stdout  # the tests did run
    bad = run_fixture("duplicates", env=env)
    assert bad.code == 1


@needs_reporters
@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_a_termination_signal_reaches_the_test_process(run_fixture, tmp_path, sig):
    pid_file = tmp_path / "child.pid"
    argv, env, cwd, xml = run_fixture.prepare(
        (
            "waits",
            """
            const { test } = require("node:test");
            const fs = require("node:fs");
            test("waits", async () => {
              fs.writeFileSync(process.env.PID_FILE, String(process.pid));
              await new Promise((resolve) => setTimeout(resolve, 60000));
            });
            """,
        ),
        env={"PID_FILE": str(pid_file)},
    )
    proc = subprocess.Popen(argv, cwd=cwd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 30
        while not (pid_file.exists() and pid_file.read_text()) and time.monotonic() < deadline:
            time.sleep(0.05)
        child = int(pid_file.read_text())
        proc.send_signal(sig)
        code = proc.wait(timeout=30)
    finally:
        proc.kill()
        proc.wait()
    # The wrapper waits for the child it signalled and exits as it did...
    assert code == 128 + sig
    # ...so no test process outlives the target (a SIGTERM'd wrapper used to).
    deadline = time.monotonic() + 10
    while _alive(child) and time.monotonic() < deadline:
        time.sleep(0.05)
    try:
        assert not _alive(child)
    finally:
        if _alive(child):
            os.kill(child, signal.SIGKILL)


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie (exited, not yet reaped by init) counts as gone.
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
            return fh.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return True


def test_the_node_ci_asks_for_is_there():
    # CI sets RR_NODE_MIN to its matrix's Node, so a node that is missing or
    # cannot be read fails the job instead of skipping every test above.
    want = os.environ.get("RR_NODE_MIN")
    if not want:
        pytest.skip("RR_NODE_MIN is not set")
    assert int(want) <= _MAJOR, f"{_NODE!r} is Node {_MAJOR or 'unknown'}, CI wants >= {want}"


@needs_reporters
def test_a_clean_exit_before_reporting_ends_is_a_target_scope_error(run_fixture):
    """process.exit(0) in a test drops it, every later test and maybe earlier
    ones from the report: the run must not read as passing."""
    r = run_fixture(
        (
            "exits_early",
            """
            const { test } = require("node:test");
            test("first", () => {});
            test("bails", async () => {
              await new Promise((resolve) => setTimeout(resolve, 200));
              process.exit(0);
            });
            test("never", () => {
              throw new Error("never runs");
            });
            """,
        )
    )
    assert r.code == 0
    incomplete = [c for c in r.cases if c.name == "<incomplete>"]
    assert len(incomplete) == 1 and incomplete[0].status == "error"
    assert incomplete[0].properties["rr.scope"] == "target"
    assert "before node:test finished reporting" in incomplete[0].message
    assert "never" not in [c.name for c in r.cases]


@needs_reporters
def test_a_complete_run_is_not_incomplete(run_fixture):
    for fixture in ("nesting", "zero_tests", "args"):
        r = run_fixture(fixture)
        assert "<incomplete>" not in [c.name for c in r.cases], fixture


@needs_reporters
def test_two_tests_reporting_as_one_case_key_warn(run_fixture):
    r = run_fixture(
        (
            "collide",
            """
            const { describe, test } = require("node:test");
            describe("parse > edge", () => {
              test("empty", () => {});
            });
            describe("parse", () => {
              describe("edge", () => {
                test("empty", () => {});
              });
            });
            """,
        )
    )
    assert r.code == 0
    assert r.paths() == ["collide > parse > edge::empty", "collide > parse > edge::empty"]
    assert "both report as 'collide > parse > edge::empty' (one case key)" in r.stderr
    assert "tests/node/collide.test.cjs:3:3 and tests/node/collide.test.cjs:7:5" in r.stderr
