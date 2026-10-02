# SPDX-License-Identifier: AGPL-3.0-or-later
"""cc/rr_case.h: per-case JUnit for plain-assert C/C++ tests.

The fixture binary (data/rr_case_fixture.cc) is built by Bazel and passed in
$RR_CASE_FIXTURE; from a plain checkout it is compiled with the host C++
compiler, and the tests skip when there is none. The compile-time checks
always need a compiler.
"""

import os
import re
import shutil
import signal
import subprocess
import sys
from xml.etree import ElementTree as ET

import pytest

from rules_requirements.ingest.junit import JUnitIngestor

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(os.path.dirname(_HERE))
_INCLUDE = os.path.join(_REPO, "cc")
_FIXTURE_SRC = os.path.join(_HERE, "data", "rr_case_fixture.cc")
_WARNINGS = ["-Wall", "-Wextra", "-Wpedantic", "-Wshadow", "-Wconversion", "-Wsign-conversion", "-Werror"]

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="rr_case.h forks per case on POSIX only")


def _compiler():
    cxx = os.environ.get("CXX") or shutil.which("c++") or shutil.which("g++") or shutil.which("clang++")
    if not cxx or not os.path.exists(os.path.join(_INCLUDE, "rr_case.h")):
        pytest.skip("no C++ compiler (or cc/rr_case.h) available")
    return cxx


def _compile(tmp_path, source, *flags, std="c++11"):
    src = tmp_path / "case_test.cc"
    src.write_text(source, encoding="utf-8")
    out = tmp_path / "case_test"
    return subprocess.run(
        [_compiler(), f"-std={std}", *_WARNINGS, *flags, "-I", _INCLUDE, str(src), "-o", str(out)],
        capture_output=True,
        text=True,
    )


@pytest.fixture(scope="module")
def binary(tmp_path_factory):
    prebuilt = os.environ.get("RR_CASE_FIXTURE")
    if prebuilt:
        return os.path.abspath(prebuilt)
    out = tmp_path_factory.mktemp("rr_case") / "rr_case_fixture"
    proc = subprocess.run(
        [_compiler(), "-std=c++11", *_WARNINGS, "-I", _INCLUDE, _FIXTURE_SRC, "-o", str(out)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return str(out)


def _run(binary, tmp_path, *args, xml=True, **env):
    full = {k: v for k, v in os.environ.items() if not k.startswith(("TEST", "XML_OUTPUT_FILE", "RR_FIXTURE"))}
    full.update(env)
    path = tmp_path / "out.xml"
    if xml:
        full["XML_OUTPUT_FILE"] = str(path)
    proc = subprocess.run(
        [binary, *args], capture_output=True, encoding="utf-8", errors="replace", env=full, cwd=str(tmp_path)
    )
    return proc, path


def _cases(path):
    """{name: (status, message, requirement)}, in file order (last wins)."""
    out = {}
    for case in ET.parse(str(path)).getroot().iter("testcase"):
        status, message = "passed", ""
        for tag in ("failure", "error"):
            el = case.find(tag)
            if el is not None:
                status, message = tag, el.get("message", "")
        reqs = [p.get("value") for p in case.iter("property") if p.get("name") == "requirement"]
        out[case.get("name")] = (status, message, reqs[0] if reqs else None, case.get("classname"))
    return out


def test_every_case_runs_isolated_with_its_own_result(binary, tmp_path):
    proc, path = _run(binary, tmp_path)
    assert proc.returncode == 1
    cases = _cases(path)
    assert list(cases)[:8] == [
        "passes",
        "prints_then_passes",
        "assert_fails",
        "check_fails",
        "segfaults",
        "throws",
        "exits_nonzero",
        "xml_unsafe_output",
    ]
    assert {c[3] for c in cases.values()} == {"fixture"}
    assert cases["prints_then_passes"][:3] == ("passed", "", None)
    assert cases["kills_runner"][0] == "passed"  # every case after the failures still ran

    status, message, req, _ = cases["assert_fails"]
    assert (status, req) == ("failure", "REQ-2")
    assert message.startswith("terminated by SIGABRT: ") and "1 + 1 == 3" in message
    status, message, req, _ = cases["check_fails"]
    assert (status, req) == ("failure", "REQ-2")
    assert message.startswith("terminated by SIGABRT: ")
    assert re.search(r"rr_case_fixture\.cc:\d+: RR_CHECK\(2 \+ 2 == 5\) failed$", message)
    assert cases["segfaults"][1] in ("terminated by SIGSEGV", "terminated by SIGBUS")
    assert cases["throws"][1] == "exited with status 1: uncaught exception: boom"
    assert cases["exits_nonzero"][1] == "exited with status 3: about to exit"
    # Arbitrary output is made safe for XML (the file above parsed).
    assert cases["xml_unsafe_output"][1] == 'exited with status 2: bad <&"> ? ? bytes'

    # Output is relayed in order, and failures are summarized in the log.
    assert "hello from stdout\nhello from stderr\n" in proc.stdout
    assert "[  FAILED  ] fixture::check_fails: terminated by SIGABRT" in proc.stdout
    assert "[==========] fixture: 13 case(s), 10 failed" in proc.stdout
    assert not [p for p in os.listdir(str(tmp_path)) if p.startswith("core")]


def test_a_case_verifies_at_most_one_requirement(binary, tmp_path):
    proc, path = _run(binary, tmp_path)
    cases = _cases(path)
    assert cases["passes"][2] is None  # the duplicate (below) is the last "passes" in the file
    for name, detail in (
        ("comma_id", 'requirement "REQ-1,REQ-2" names more than one id'),
        ("space_id", 'malformed requirement id "REQ-1 REQ-2"'),
        ("empty_id", "empty requirement id"),
    ):
        status, message, req, _ = cases[name]
        # Never recorded as a requirement, never run.
        assert (status, req) == ("error", None)
        assert message.startswith(detail) and message.endswith("[RR-E104]")
    assert "MUST NOT RUN" not in proc.stdout
    assert cases["passes"][:2] == ("error", "duplicate case name: each case of a suite needs its own")


def test_junit_is_ingested_as_suite_case_keys_with_single_ids(binary, tmp_path):
    _, path = _run(binary, tmp_path)
    got = {(c.classname, c.name): (c.status, c.requirements) for c in JUnitIngestor().ingest(str(path))}
    assert got[("fixture", "check_fails")] == ("failed", ("REQ-2",))
    assert got[("fixture", "comma_id")] == ("error", ())
    assert all(len(reqs) <= 1 for _, reqs in got.values())


def test_registry_form_runs_rr_case_definitions_in_order(binary, tmp_path):
    proc, path = _run(binary, tmp_path, RR_FIXTURE_FORM="registry")
    assert proc.returncode == 1
    cases = _cases(path)
    assert list(cases) == ["registered_plain", "registered_tagged", "registered_fails"]
    assert cases["registered_plain"][:3] == ("passed", "", None)
    assert cases["registered_tagged"][:3] == ("passed", "", "REQ-9")
    assert cases["registered_fails"][1].endswith("RR_CHECK(false) failed")
    assert {c[3] for c in cases.values()} == {"fixture_registry"}


def test_all_passing_cases_exit_zero(binary, tmp_path):
    proc, path = _run(binary, tmp_path, TESTBRIDGE_TEST_ONLY="fixture::prints_*,kills_runner")
    assert proc.returncode == 0, proc.stdout
    assert list(_cases(path)) == ["prints_then_passes", "kills_runner"]


def test_test_filter_globs_names_and_keys(binary, tmp_path):
    proc, path = _run(binary, tmp_path, TESTBRIDGE_TEST_ONLY="*_fails,fixture::throw?")
    assert proc.returncode == 1
    assert list(_cases(path)) == ["assert_fails", "check_fails", "throws"]


def test_sharding_partitions_cases_and_touches_the_status_file(binary, tmp_path):
    seen = []
    for index in range(3):
        status = tmp_path / f"status{index}"
        shard_dir = tmp_path / f"shard{index}"
        shard_dir.mkdir()
        _, path = _run(
            binary,
            shard_dir,
            TEST_TOTAL_SHARDS="3",
            TEST_SHARD_INDEX=str(index),
            TEST_SHARD_STATUS_FILE=str(status),
        )
        assert status.exists()
        seen.extend(case.get("name") for case in ET.parse(str(path)).getroot().iter("testcase"))
    _, path = _run(binary, tmp_path)
    everything = [case.get("name") for case in ET.parse(str(path)).getroot().iter("testcase")]
    assert sorted(seen) == sorted(everything)
    assert len(seen) == 13


def test_a_killed_run_still_reports_finished_cases_and_names_the_running_one(binary, tmp_path):
    proc, path = _run(binary, tmp_path, TESTBRIDGE_TEST_ONLY="segfaults,kills_runner", RR_FIXTURE_KILL="1")
    assert proc.returncode == -signal.SIGKILL
    cases = _cases(path)
    assert cases["segfaults"][0] == "failure"
    assert cases["kills_runner"][0] == "error"
    assert cases["kills_runner"][1].startswith("did not finish")


def test_list_and_single_case_flags(binary, tmp_path):
    proc, path = _run(binary, tmp_path, "--rr_list", RR_FIXTURE_FORM="registry")
    assert proc.returncode == 0
    assert proc.stdout.splitlines() == [
        "fixture_registry::registered_plain",
        "fixture_registry::registered_tagged [REQ-9]",
        "fixture_registry::registered_fails",
    ]
    assert not path.exists()

    proc, path = _run(binary, tmp_path, "--rr_case=prints_then_passes")
    assert proc.returncode == 0
    assert "hello from stdout" in proc.stdout
    assert not path.exists()  # in-process debugging run: no JUnit
    proc, _ = _run(binary, tmp_path, "--rr_case=check_fails")
    assert proc.returncode == -signal.SIGABRT  # not isolated: the binary aborts
    proc, _ = _run(binary, tmp_path, "--rr_case=nope")
    assert proc.returncode == 2
    assert "no case named nope" in proc.stderr
    proc, _ = _run(binary, tmp_path, "--rr_bogus")
    assert proc.returncode == 2
    assert "unknown flag --rr_bogus" in proc.stderr
    proc, _ = _run(binary, tmp_path, "--unrelated", TESTBRIDGE_TEST_ONLY="kills_runner")
    assert proc.returncode == 0  # other flags belong to the test


def test_junit_flag_writes_without_bazel(binary, tmp_path):
    target = tmp_path / "results.xml"
    proc, path = _run(binary, tmp_path, f"--rr_junit={target}", xml=False, TESTBRIDGE_TEST_ONLY="passes")
    assert proc.returncode == 1  # the duplicate "passes" is an error
    assert not path.exists()
    assert [c.get("name") for c in ET.parse(str(target)).getroot().iter("testcase")] == ["passes", "passes"]


_MAIN = 'int main(int argc, char** argv) { return rr::RunCases(argc, argv, "s"); }\n'


def test_two_ids_on_one_case_do_not_compile(tmp_path):
    proc = _compile(tmp_path, '#include "rr_case.h"\nRR_CASE(both, "REQ-1", "REQ-2") {}\n' + _MAIN)
    assert proc.returncode != 0
    assert "a test case verifies at most one requirement [RR-E101]" in proc.stderr
    proc = _compile(
        tmp_path,
        '#include "rr_case.h"\nstatic void f() {}\n'
        'int main(int argc, char** argv) { return rr::RunCases(argc, argv, "s", {{"x", f, "REQ-1", "REQ-2"}}); }\n',
    )
    assert proc.returncode != 0


@pytest.mark.parametrize(
    "std, flags",
    [("c++14", ["-DNDEBUG", "-O2"]), ("c++17", []), ("c++20", ["-fno-exceptions"])],
)
def test_header_is_warning_clean(tmp_path, std, flags):
    source = (
        '#include "rr_case.h"\n'
        'RR_CASE(checks) { RR_CHECK(1 + 1 == 2); }\nRR_CASE(tagged, "REQ-1") {}\n'
        "RR_CASE(fails_under_ndebug_too) { RR_CHECK(1 + 1 == 3); }\n" + _MAIN
    )
    proc = _compile(tmp_path, source, *flags, std=std)
    assert proc.returncode == 0, proc.stderr
    run = subprocess.run(
        [str(tmp_path / "case_test")], capture_output=True, text=True, env={"XML_OUTPUT_FILE": str(tmp_path / "x.xml")}
    )
    assert run.returncode == 1  # RR_CHECK is not compiled out by NDEBUG
    assert [c[0] for c in _cases(tmp_path / "x.xml").values()] == ["passed", "passed", "failure"]
