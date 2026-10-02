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
import time
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


def _alive(pid):
    """True while `pid` runs (a zombie, which no one may reap here, is dead)."""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
            return fh.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="PR_SET_PDEATHSIG is Linux-only")
def test_a_hung_case_dies_with_its_killed_runner(binary, tmp_path):
    # rr_evidence (and many CI runners) kill only the runner on a timeout,
    # not its process group: the hung case's child must not outlive it.
    pidfile = tmp_path / "child.pid"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TEST", "XML_OUTPUT_FILE", "RR_FIXTURE"))}
    env.update(RR_FIXTURE_FORM="hang", RR_FIXTURE_PIDFILE=str(pidfile), XML_OUTPUT_FILE=str(tmp_path / "out.xml"))
    runner = subprocess.Popen([binary], env=env, cwd=str(tmp_path), stdout=subprocess.DEVNULL)
    child = None
    try:
        deadline = time.monotonic() + 30
        while child is None and time.monotonic() < deadline:
            text = pidfile.read_text() if pidfile.exists() else ""
            child = int(text) if text.endswith("\n") else None
            time.sleep(0.02)
        assert child is not None, "the hanging case never started"
        assert _alive(child)
        runner.kill()
        runner.wait()
        deadline = time.monotonic() + 10
        while _alive(child) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not _alive(child), "the hung case outlived its killed runner"
        assert _cases(tmp_path / "out.xml")["hangs"][1].startswith("did not finish")
    finally:
        runner.kill()
        runner.wait()
        if child is not None and _alive(child):
            os.kill(child, signal.SIGKILL)


def test_ids_must_be_strict_ascii_and_flags_refuse_bad_cases(binary, tmp_path):
    proc, path = _run(binary, tmp_path, RR_FIXTURE_FORM="ids")
    assert proc.returncode == 1
    cases = _cases(path)
    assert cases["dotted_id"][:3] == ("passed", "", "SRS-1.2_a")
    for name in ("nbsp_id", "semicolon_id", "slash_id"):
        status, message, req, _ = cases[name]
        # Ingest splits on commas and any (Unicode) whitespace: never let one
        # case's id read as two.
        assert (status, req) == ("error", None), name
        assert message.startswith("malformed requirement id") and message.endswith("[RR-E104]"), message
    assert cases[""][:2] == ("error", "a case needs a name and a function")
    assert "MUST NOT RUN" not in proc.stdout

    proc, _ = _run(binary, tmp_path, "--rr_list", RR_FIXTURE_FORM="ids")
    assert proc.returncode == 0
    assert "fixture_ids::\n" in proc.stdout  # the nameless case, not a crash
    for flag, why in (
        ("--rr_case=semicolon_id", "malformed requirement id"),
        ("--rr_case=twice", "duplicate case name"),
    ):
        proc, _ = _run(binary, tmp_path, flag, RR_FIXTURE_FORM="ids")
        assert proc.returncode == 1, (flag, proc.returncode)
        assert why in proc.stderr
        assert "MUST NOT RUN" not in proc.stdout
    proc, _ = _run(binary, tmp_path, "--rr_case=dotted_id", RR_FIXTURE_FORM="ids")
    assert proc.returncode == 0


def test_rr_case_records_where_each_case_is_defined(binary, tmp_path):
    _, path = _run(binary, tmp_path, RR_FIXTURE_FORM="registry")
    with open(_FIXTURE_SRC, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    for case in ET.parse(str(path)).getroot().iter("testcase"):
        assert case.get("file", "").endswith("rr_case_fixture.cc")
        assert lines[int(case.get("line")) - 1].startswith("RR_CASE(" + case.get("name"))
    _, path = _run(binary, tmp_path)
    assert all(case.get("file") is None for case in ET.parse(str(path)).getroot().iter("testcase"))


_LOCALES = ("de_DE.UTF-8", "de_DE.utf8", "fr_FR.UTF-8", "fr_FR.utf8", "nl_NL.UTF-8", "ru_RU.UTF-8")


def test_times_do_not_follow_the_numeric_locale(binary, tmp_path):
    for name in _LOCALES:
        proc, path = _run(binary, tmp_path, RR_FIXTURE_FORM="locale", RR_FIXTURE_LOCALE=name)
        if proc.returncode != 77:
            break
    else:
        pytest.skip("no locale with a decimal comma is installed")
    assert proc.returncode == 1
    root = ET.parse(str(path)).getroot()
    times = [root.get("time")] + [el.get("time") for el in root.iter() if el.tag in ("testsuite", "testcase")]
    assert all(re.fullmatch(r"\d+\.\d{3}", t) for t in times), times


@pytest.mark.parametrize("ids", [5, 6, 15])
def test_any_number_of_ids_fails_with_rr_e101(tmp_path, ids):
    listed = ", ".join(f'"REQ-{i}"' for i in range(ids))
    proc = _compile(tmp_path, f'#include "rr_case.h"\nRR_CASE(many, {listed}) {{}}\n' + _MAIN)
    assert proc.returncode != 0
    assert "a test case verifies at most one requirement [RR-E101]" in proc.stderr


def test_a_parenthesised_id_list_does_not_compile(tmp_path):
    # A comma expression would silently record only "REQ-2".
    proc = _compile(tmp_path, '#include "rr_case.h"\nRR_CASE(both, ("REQ-1", "REQ-2")) {}\n' + _MAIN, "-Wno-error")
    assert proc.returncode != 0
    assert "not a parenthesised list" in proc.stderr and "[RR-E101]" in proc.stderr


def test_werror_unused_value_rejects_a_comma_expression_in_the_list_form(tmp_path):
    # The documented remedy: the list form itself cannot see the comma.
    source = (
        '#include "rr_case.h"\nstatic void f() {}\n'
        "int main(int argc, char** argv) {\n"
        '  return rr::RunCases(argc, argv, "s", {{"x", f, ("REQ-1", "REQ-2")}});\n}\n'
    )
    assert _compile(tmp_path, source, "-Wno-error", "-Wno-unused-value").returncode == 0
    proc = _compile(tmp_path, source, "-Wno-error", "-Werror=unused-value")
    assert proc.returncode != 0 and "unused-value" in proc.stderr


_LEAKS = """#include <cstdlib>
#include "rr_case.h"
static void* volatile sink;
RR_CASE(leaks) { sink = std::malloc(1000); sink = nullptr; }
RR_CASE(clean) { void* p = std::malloc(10); std::free(p); }
int main(int argc, char** argv) {
  if (std::getenv("PRELEAK") != nullptr) {  // leaked before any case runs
    sink = std::malloc(77);
    sink = nullptr;
  }
  return rr::RunCases(argc, argv, "lsan");
}
"""


def _run_with_leak_sanitizer(tmp_path, **env):
    if not sys.platform.startswith("linux"):
        pytest.skip("LeakSanitizer is checked per case on ELF targets")
    proc = _compile(tmp_path, _LEAKS, "-fsanitize=address", "-fno-omit-frame-pointer")
    if proc.returncode != 0:
        pytest.skip("no AddressSanitizer runtime: " + proc.stderr[-200:])
    xml = tmp_path / "x.xml"
    full = dict(os.environ, XML_OUTPUT_FILE=str(xml), ASAN_OPTIONS="detect_leaks=1", **env)
    run = subprocess.run([str(tmp_path / "case_test")], capture_output=True, text=True, env=full)
    if "LeakSanitizer has encountered a fatal error" in run.stdout + run.stderr:
        pytest.skip("LeakSanitizer cannot run here (ptrace restricted)")
    return run, _cases(xml)


def test_a_leaking_case_fails_under_leak_sanitizer(tmp_path):
    run, cases = _run_with_leak_sanitizer(tmp_path)
    assert cases["clean"][0] == "passed"
    assert cases["leaks"][0] == "failure", run.stdout
    assert cases["leaks"][1] == "exited with status 1: rr_case: LeakSanitizer found memory leaked by this case"
    assert run.returncode == 1


def test_a_leak_from_before_the_cases_fails_the_binary_and_blames_no_case(tmp_path):
    run, cases = _run_with_leak_sanitizer(tmp_path, PRELEAK="1")
    output = run.stdout + run.stderr
    # The runner's own leak cannot be told apart from a case's in a child, so
    # the children do not check: no case fails for it, but the binary does.
    assert cases["clean"][:2] == ("passed", "")
    assert cases["leaks"][:2] == ("passed", "")
    assert "leaked by this case" not in output
    assert output.count("rr_case: LeakSanitizer found memory leaked before any case ran") == 1, output
    assert "[==========] lsan: 2 case(s), 0 failed, memory leaked before any case ran\n" in run.stdout
    assert run.returncode != 0


_COVERED = """#include <cstdio>
#include <cstdlib>
#include "rr_case.h"
static int before_cases(int x) { return x + 1; }
static int only_in_a_case(int x) { return x * 3; }
static void at_exit() { std::printf("ATEXIT\\n"); }
struct Static {
  ~Static() { std::printf("STATIC DTOR\\n"); }
} static_object;
RR_CASE(calls_it) { RR_CHECK(only_in_a_case(2) == 6); }
RR_CASE(other) {}
RR_CASE(third) {}
int main(int argc, char** argv) {
  std::atexit(at_exit);
  RR_CHECK(before_cases(1) == 2);
  return rr::RunCases(argc, argv, "cov");
}
"""


def _gcc_with_gcov():
    gcov = shutil.which("gcov")
    cxx = _compiler()
    version = subprocess.run([cxx, "--version"], capture_output=True, text=True).stdout
    if not gcov or "clang" in version.lower():
        pytest.skip("needs gcc and gcov")
    return gcov


@pytest.mark.parametrize("coverage", [False, True], ids=["plain", "gcc-coverage"])
def test_exit_handlers_and_static_destructors_run_once_not_per_case(tmp_path, coverage):
    if coverage:
        _gcc_with_gcov()
    proc = _compile(tmp_path, _COVERED, *(["--coverage"] if coverage else []))
    if proc.returncode != 0:
        pytest.skip("no gcov runtime: " + proc.stderr[-200:])
    run = subprocess.run([str(tmp_path / "case_test")], capture_output=True, text=True, cwd=str(tmp_path), env={})
    assert run.returncode == 0, run.stdout
    # Once in total, in the runner, after its summary line: never in a child.
    assert run.stdout.count("ATEXIT") == 1 and run.stdout.count("STATIC DTOR") == 1, run.stdout
    assert run.stdout.index("[==========]") < run.stdout.index("ATEXIT")


def test_gcc_coverage_counts_each_line_once_per_run_of_it(tmp_path):
    gcov = _gcc_with_gcov()
    proc = _compile(tmp_path, _COVERED, "--coverage")
    if proc.returncode != 0:
        pytest.skip("no gcov runtime: " + proc.stderr[-200:])
    run = subprocess.run([str(tmp_path / "case_test")], capture_output=True, text=True, cwd=str(tmp_path), env={})
    assert run.returncode == 0, run.stdout
    report = subprocess.run(
        [gcov, "-t", "-o", str(tmp_path), str(tmp_path / "case_test.cc")],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
    ).stdout

    def count(suffix):
        line = next(ln for ln in report.splitlines() if ln.rstrip().endswith(suffix))
        return line.split(":", 1)[0].strip().rstrip("*")

    # Run in one case's child only, and before the cases in the runner only:
    # one count each, however many children were forked after it.
    assert count("{ return x * 3; }") == "1", report
    assert count("{ return x + 1; }") == "1", report
    assert count('{ std::printf("ATEXIT\\n"); }') == "1", report


def _wait_for_pid(pidfile, proc):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        text = pidfile.read_text() if pidfile.exists() else ""
        if text.endswith("\n"):
            return int(text)
        assert proc.poll() is None, "the runner ended before its hanging case started"
        time.sleep(0.02)
    raise AssertionError("the hanging case never started")


def _gone(pid, seconds=10):
    deadline = time.monotonic() + seconds
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.02)
    return not _alive(pid)


@pytest.mark.skipif(not os.path.isdir("/proc/self"), reason="reads /proc")
@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT, signal.SIGHUP], ids=lambda s: s.name)
def test_a_signal_to_the_runner_kills_its_running_case(binary, tmp_path, sig):
    # Without PR_SET_PDEATHSIG (as on every POSIX system but Linux), only the
    # runner's handler can end the case's child.
    pidfile = tmp_path / "child.pid"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TEST", "XML_OUTPUT_FILE", "RR_FIXTURE"))}
    env.update(
        RR_FIXTURE_FORM="hang",
        RR_FIXTURE_PIDFILE=str(pidfile),
        RR_FIXTURE_NO_PDEATHSIG="1",
        XML_OUTPUT_FILE=str(tmp_path / "out.xml"),
    )
    runner = subprocess.Popen([binary], env=env, cwd=str(tmp_path), stdout=subprocess.DEVNULL)
    child = None
    try:
        child = _wait_for_pid(pidfile, runner)
        runner.send_signal(sig)
        assert runner.wait(timeout=10) == -sig  # ended as the signal would have ended it
        assert _gone(child), "the case's child outlived its signalled runner"
        assert _cases(tmp_path / "out.xml")["hangs"][1].startswith("did not finish")
    finally:
        runner.kill()
        runner.wait()
        if child is not None and _alive(child):
            os.kill(child, signal.SIGKILL)


def _bazel_main(*argv):
    import rules_requirements

    path = os.path.dirname(os.path.dirname(os.path.abspath(rules_requirements.__file__)))
    code = f"import sys; sys.path.insert(0, {path!r}); from rules_requirements import bazel; sys.exit(bazel.main({list(argv)!r}))"
    return [sys.executable, "-c", code]


def _hang_spec(tmp_path, binary, *extra_env):
    pidfile = tmp_path / "child.pid"
    args = ["--test", f"//pkg:hang={binary}=_main", "--env", "//pkg:hang=RR_FIXTURE_FORM=hang"]
    args += ["--env", f"//pkg:hang=RR_FIXTURE_PIDFILE={pidfile}"]
    for env in extra_env:
        args += ["--env", f"//pkg:hang={env}"]
    return pidfile, args


@pytest.mark.skipif(not os.path.isdir("/proc/self"), reason="reads /proc")
def test_killing_the_rr_evidence_action_kills_the_test_and_its_case(binary, tmp_path):
    # Bazel kills an action by killing its process group: the test, and the
    # case its runner forked, must still be in it.
    pidfile, spec = _hang_spec(tmp_path, binary)
    out = tmp_path / "testlogs"
    action = subprocess.Popen(
        _bazel_main("run-tests", "--out", str(out), "--timeout", "120", *spec),
        cwd=str(tmp_path),
        start_new_session=True,
        stderr=subprocess.DEVNULL,
    )
    child = runner = None
    try:
        child = _wait_for_pid(pidfile, action)
        with open(f"/proc/{child}/stat", encoding="utf-8") as fh:
            runner = int(fh.read().rsplit(")", 1)[1].split()[1])
        assert runner != action.pid and _alive(runner)
        os.killpg(action.pid, signal.SIGKILL)
        action.wait()
        assert _gone(runner), "the test outlived its killed rr_evidence action"
        assert _gone(child), "the case's child outlived its killed rr_evidence action"
    finally:
        for pid in (child, runner):
            if pid is not None and _alive(pid):
                os.kill(pid, signal.SIGKILL)
        if action.poll() is None:
            action.kill()
            action.wait()


@pytest.mark.skipif(not os.path.isdir("/proc/self"), reason="reads /proc")
def test_an_rr_evidence_timeout_ends_the_running_case(binary, tmp_path):
    from rules_requirements import bazel

    pidfile, spec = _hang_spec(tmp_path, binary, "RR_FIXTURE_NO_PDEATHSIG=1")
    out = tmp_path / "testlogs"
    cwd = os.getcwd()
    os.chdir(str(tmp_path))
    try:
        assert bazel.main(["run-tests", "--out", str(out), "--timeout", "2", *spec]) == 0
    finally:
        os.chdir(cwd)
    child = int(pidfile.read_text())
    try:
        assert _gone(child), "the hung case outlived the timeout"
        log = (out / "pkg" / "hang" / "test.log").read_text()
        assert log.endswith("TIMEOUT after 2.0s") and "[ RUN      ] fixture_hang::hangs" in log
        assert _cases(out / "pkg" / "hang" / "test.xml")["hangs"][1].startswith("did not finish")
    finally:
        if _alive(child):
            os.kill(child, signal.SIGKILL)
