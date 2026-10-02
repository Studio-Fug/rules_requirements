# SPDX-License-Identifier: AGPL-3.0-or-later
"""rr_node_test's runner (js/) on the tests/node fixtures, outside Bazel.

The entry point template is expanded here the way ``rr/private/node.bzl``
expands it, run with a real ``node`` (``$RR_NODE``, else the one on ``PATH``)
and its JUnit read back with the JUnit ingestor. CI runs this module on Node
20, 22 and 24, so a change in node:test's event model fails here first.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import textwrap
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
    return int(out.strip().lstrip("v").split(".")[0])


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
    """run_fixture(name or (name, source), args=(), level="", env=None, xml=True) -> Run."""

    pkg = tmp_path / "_main" / "tests" / "node"
    pkg.mkdir(parents=True)
    (tmp_path / "_main" / "js").mkdir()
    shutil.copy(_repo_file("js/verifies.cjs"), tmp_path / "_main" / "js" / "verifies.cjs")
    template = _read("js/node_test_main.cjs.tpl")
    reporter = _repo_file("js/rr_node_reporter.mjs")

    def run(fixture, args=(), level="", env=None, xml=True):
        if isinstance(fixture, tuple):
            fixture, source = fixture
            (pkg / f"{fixture}.test.cjs").write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
        else:
            shutil.copy(_repo_file(f"tests/node/{fixture}.test.cjs"), pkg / f"{fixture}.test.cjs")
        name = f"{fixture}_test"
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
            "RUNFILES_DIR": str(tmp_path),
            "TEST_TMPDIR": str(tmp_path),
            **({"XML_OUTPUT_FILE": str(out)} if xml else {}),
            **(env or {}),
        }
        proc = subprocess.run(
            [_NODE, str(main)],
            cwd=tmp_path / "_main",
            env=full_env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        plain = subprocess.run(
            [_NODE, str(pkg / f"{fixture}.test.cjs"), *args],
            cwd=tmp_path / "_main",
            env={"PATH": full_env["PATH"], "RR_NODE_VERIFIES": str(tmp_path / "_main" / "js" / "verifies.cjs")},
            capture_output=True,
            timeout=120,
            check=False,
        )
        return Run(proc.returncode, str(out), proc.stdout, proc.stderr, plain.returncode)

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
    ]


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
