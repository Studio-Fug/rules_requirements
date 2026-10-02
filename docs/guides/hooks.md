# Test hooks

A hook lets a test declare which entities it verifies, and at what level. Every
hook produces the same thing: **JUnit XML** in which each `<testcase>` carries
its traces as properties. That keeps the report independent of the test
framework, and lets anything that can write JUnit take part ({doc}`evidence`).

| Framework | Declare | Bazel | Without Bazel |
| --------- | ------- | ----- | ------------- |
| pytest | `@pytest.mark.rr("REQ-1", level="sil")` | `rr_py_test` | pip-installed plugin, `pytest --junitxml=...` |
| unittest | `@rr.verifies("REQ-1")` | `py_test` + `rr.unittest_main()` | `rr.unittest_main()` / `--junit-xml` |
| googletest | `RR_VERIFIES("REQ-1");` | `cc_test` + `@rules_requirements//cc:gtest` | `--gtest_output=xml:...` |
| plain-assert C/C++ | `RR_CASE(name, "REQ-1") { ... }` | `cc_test` + `@rules_requirements//cc:case` | `--rr_junit=...` |
| Rust | `rr::verifies!("REQ-1");` | `rr_rust_test` | `rr wrap -- <test binary>` |
| anything else | `JUnitWriter` | `py_test` / `py_binary` | write the XML yourself |

(junit-properties)=
## The JUnit conventions

| Property | Meaning |
| -------- | ------- |
| `requirement` | An id the test case verifies. Repeat the property for several ids; a comma-separated value also works. |
| `requirements` | Comma-separated ids (the form googletest's single-valued `RecordProperty` needs). |
| `level` | The verification level the case provides. Default: the model's `default_provided_level`. |
| `artifact.<key>` | One key of the identity of the artifact exercised (firmware build id, board revision, git SHA...), for staleness checks. |

The properties may be `<property name=... value=.../>` children of the
`<testcase>`'s `<properties>`, attributes of the `<testcase>` element (older
googletest), or `<properties>` of an enclosing `<testsuite>`, which then apply
to every case in the suite — a convenient place for a suite-wide `level` or
artifact identity.

```xml
<testcase classname="tests.test_controller" name="test_heats_below_band" time="0.001">
  <properties>
    <property name="requirement" value="REQ-1"/>
    <property name="level" value="simulation"/>
    <property name="artifact.firmware_build_id" value="fw-2026.09.30-3"/>
  </properties>
</testcase>
```

## pytest

```python
import pytest

pytestmark = pytest.mark.rr("REQ-10")  # every test in the module


@pytest.mark.rr("REQ-1", "REQ-2")  # several ids
def test_heats_below_band(): ...


@pytest.mark.rr("REQ-5", level="hil", artifact={"board_rev": "C"})
def test_cutoff_on_the_bench(): ...


@pytest.mark.requirements("REQ-3, REQ-4")  # alias; comma lists work too
def test_parses_units(): ...
```

- Positional arguments are ids: several arguments, a comma-separated string,
  or a list.
- `level=` names the level the test provides; `artifact=` a mapping of artifact
  identity keys.
- Markers accumulate: a test gets the ids of every `rr`/`requirements` marker on
  it, its class and its module (`pytestmark`), without duplicates.
- For the level, `rr` markers are considered before `requirements` markers and,
  within each, the nearest (function, then class, then module) that names a
  level wins.
- `unittest.TestCase` methods collected by pytest honour `@rr.verifies(...)`
  (below); the decorator's level applies only when no marker names one.

**Installation.** With pip, the plugin registers itself through the `pytest11`
entry point and `pytest --junitxml=results.xml` is all you need. Under Bazel,
use `rr_py_test` ({doc}`bazel`), which runs pytest over the listed files, loads
the plugin and writes the JUnit to `$XML_OUTPUT_FILE`; add your workspace's
pytest to its `deps`. A hand-written Bazel main can do the same:

```python
from rules_requirements.hooks.pytest_runner import main

if __name__ == "__main__":
    raise SystemExit(main(__file__))  # pytest over this file's directory
```

The properties are recorded when the test is collected, so they reach the
JUnit case however the test ends — including tests skipped by
`@pytest.mark.skip` / `skipif` (a skipped test never counts as passing evidence,
but it is listed under its requirement). The nearest declaration naming a
`level` wins: a method's own marker or `@rr.verifies` beats its class's, which
beats a module-level `pytestmark`.

## unittest

```python
import unittest

from rules_requirements import rr


@rr.verifies("REQ-9", level="sil")  # applies to every test in the class
class InterlockTest(unittest.TestCase):
    @rr.verifies("REQ-5")
    def test_trips_at_limit(self): ...


if __name__ == "__main__":
    rr.unittest_main()
```

`rr.verifies(*ids, level="", artifact=None)` records the trace on the function
or class; decorators stack, and a method's own level wins over its class's.
`rr.unittest_main()` replaces `unittest.main()`: it runs the module's tests and
writes JUnit to `$XML_OUTPUT_FILE`, so a plain Bazel `py_test` with
`@rules_requirements//python` in its `deps` needs nothing else. Outside Bazel, pass `--junit-xml`:

```console
$ python -m my_tests --junit-xml results.xml        # a module that calls rr.unittest_main()
```

{py:func}`rules_requirements.hooks.unittest.main` also runs discovery, for a
small runner script of your own: it accepts `--discover DIR`, `--pattern GLOB`
(default `test*.py`), `--junit-xml PATH` and `-q`, and returns the exit status.

Expected failures are recorded as passed, unexpected successes as failed, skips
as skipped. When the module runs as `__main__`, its test classes are named after
the file.

## googletest

```cpp
#include "rr_gtest.h"

TEST(Interlock, TripsAtLimit) {
  RR_VERIFIES("REQ-5");                          // ids; call again to add more
  RR_LEVEL("sil");                               // optional
  RR_ARTIFACT("firmware_build_id", kBuildId);    // optional, per key
  ...
}
```

In Bazel, add `@rules_requirements//cc:gtest` to the `cc_test`'s `deps` next to
your own googletest (the library is header-only and deliberately does not
depend on googletest, so it never adds googletest to your module graph).
googletest writes JUnit to `$XML_OUTPUT_FILE` under
`bazel test`, so a plain `cc_test` needs no wrapper; elsewhere run the binary
with `--gtest_output=xml:results.xml`.

`RecordProperty` keeps one value per key, so the ids of a test are accumulated
and recorded as one comma-separated `requirements` property. The helpers are
also available as functions — `rules_requirements::Verifies({...})`,
`Level(...)`, `Artifact(key, value)`.

(rr-case-h)=
## Plain-assert C/C++: `rr_case.h`

Many C and C++ tests are a `main()` that calls test functions full of
`assert()`: the first failure aborts the binary, and Bazel can only report the
whole target. `rr_case.h` turns such a binary into one JUnit case per test
function, without googletest:

```cpp
#include "rr_case.h"

RR_CASE(wifi_settings_vector) {                  // one case
  RR_CHECK(Encode(kSettings) == kWireVector);    // like assert(), kept under NDEBUG
}

RR_CASE(rejects_truncated_frame, "REQ-7") {      // optional: the one id it verifies
  assert(!Decode(kTruncated));                   // plain assert() works too
}

int main(int argc, char** argv) { return rr::RunCases(argc, argv, "improv_codec"); }
```

An existing main converts without moving its test functions — list them:

```cpp
int main(int argc, char** argv) {
  return rr::RunCases(argc, argv, "improv_codec", {
      {"wifi_settings_vector", test_wifi_settings_vector},
      {"rejects_truncated_frame", test_rejects_truncated_frame, "REQ-7"},
  });
}
```

In Bazel, add `@rules_requirements//cc:case` to the `cc_test`'s `deps`; the
library is header-only and has no dependencies. Under `bazel test` the JUnit
goes to `$XML_OUTPUT_FILE`; elsewhere pass `--rr_junit=results.xml`.

- **Isolation.** On POSIX each case runs in its own forked child, with core
  dumps suppressed. A failing `assert()` or `RR_CHECK`, a crash, an uncaught
  exception or a non-zero `exit()` fails that case only — the message says how
  (`terminated by SIGABRT: codec_test.cc:31: RR_CHECK(n == 4) failed`,
  `terminated by SIGSEGV`, `exited with status 1: uncaught exception: ...`) —
  and the next case still runs. The binary exits 1 if any case failed, so the
  target fails exactly as it did before. Without `fork` (Windows) the cases
  run in-process, and a failing `assert()` ends the binary there.
- **Case keys.** Each case is `<testcase classname="<suite>" name="<case>">`,
  reported as `<suite>::<case>` (`improv_codec::wifi_settings_vector`). Case
  names must be unique within a suite; a duplicate is reported as an error.
- **One requirement per case.** A case names at most one id, recorded as its
  `requirement` property. `RR_CASE(name, "REQ-1", "REQ-2")` does not compile
  (`RR-E101`), and an id that is empty or contains a comma or whitespace makes
  the case an error without running it (`RR-E104`). There is no call to add
  ids from inside a case. The id is optional: a case without one is still a
  test case in the report, and a whole-target `verified_by` reference covers it.
- **Killed runs.** The JUnit is rewritten before each case with that case
  recorded as an error, so a binary killed mid-case (a Bazel timeout) still
  reports the cases that finished and names the one that did not.
- **Selection.** `bazel test --test_filter=GLOB[,GLOB...]` runs the cases whose
  name or `suite::case` key matches (`*`, `?`), and `shard_count` is honoured.

| Flag | Meaning |
| ---- | ------- |
| `--rr_list` | Print every case key (`suite::case [REQ-1]`) and exit. |
| `--rr_case=NAME` | Run one case in-process, without fork or JUnit — for a debugger. |
| `--rr_junit=PATH` | Write the JUnit here instead of `$XML_OUTPUT_FILE`. |

Other arguments are left to the test. `rr_case.h` records no level: a tagged
case provides the model's `default_provided_level`.

## Rust

```rust
#[test]
fn cutoff_at_limit() {
    rr::verifies!("REQ-5");                       // ids
    rr::verifies!("REQ-6"; level = "sil");        // ...with a level
    assert!(interlock::trips(35.0));
}
```

The `rr` crate is `@rules_requirements//rust:rr` in Bazel. With Cargo, add it
as a development dependency — the package is `rules_requirements` in the
repository's `rust/` directory, and its library is named `rr`:

```toml
[dev-dependencies]
rules_requirements = { git = "https://github.com/Studio-Fug/rules_requirements" }
```

libtest has no stable machine-readable output, so the macro records traces out
of band: each call appends one JSON line — the test's name, taken from the
thread libtest runs it on, plus the ids and level — to the file named by
`$RR_TRACE_FILE`. Without that variable the macro does nothing, so plain
`cargo test` is unaffected. The wrapper sets the variable, runs the tests,
parses libtest's standard output and writes JUnit with the traces attached:

- in Bazel, {ref}`rr_rust_test <rr-rust-test>` (a `rust_test` plus the
  wrapper), or {ref}`rr_wrapped_test <rr-wrapped-test>` around any libtest
  binary;
- elsewhere, `rr wrap --junit-xml rust.xml -- <test binary or command>` (see
  below).

A trace recorded on a thread the test spawns itself carries that thread's name
and is not attributed to the test; call the macro from the test's own thread.

## Hand-rolled harnesses: `JUnitWriter`

Hardware-in-the-loop and end-to-end runners are often plain programs rather than
framework test suites. {py:class}`~rules_requirements.hooks.junit_writer.JUnitWriter`
gives them the same output:

```python
import os

from rules_requirements.hooks.junit_writer import JUnitWriter

report = JUnitWriter(
    "bench_e2e",
    default_level="hitl",  # what this harness provides
    artifact={"firmware_build_id": build_id, "dut_git_sha": sha},
)
try:
    with report.case("flash_and_boot", ["REQ-13", "REQ-21"]):
        flash(dut)  # raises -> recorded as failed, then re-raised
    with report.case("provision", ["REQ-29"], level="hil"):
        provision(dut)
    report.add("ota_update", ["REQ-30"], status="skipped", message="no OTA server on this rig")
finally:
    report.write(os.environ.get("XML_OUTPUT_FILE", "bench_e2e.xml"))
```

- `case(name, requirements, level="", artifact=None)` is a context manager that
  records the phase as passed, or as failed with the exception's text if the
  block raises (the exception propagates, so control flow is unchanged).
- `add(name, requirements, status, message, duration, level, artifact,
  classname)` records a result directly; `status` is `passed`, `failed`,
  `error` or `skipped`.
- The writer's `artifact` identity is stamped on every case (merged with any
  per-case `artifact`), which is what makes stale bench results detectable.

## `rr wrap`

`rr wrap` (also `python -m rules_requirements.hooks.wrap`) runs a command,
echoes its output, converts it to traceability JUnit and exits with the
command's own exit code, so it never turns a failing test green or a passing one
red:

```console
$ rr wrap --junit-xml rust.xml -- ./target/debug/deps/setpoint-1a2b3c --test-threads=4
```

| Option | Meaning |
| ------ | ------- |
| `--format libtest` | The command's output format (currently `libtest`). |
| `--junit-xml PATH` | Where to write JUnit (default `$XML_OUTPUT_FILE`). |
| `--suite NAME` | Suite name (default: the name part of `--target`, else the binary's file name). |
| `--target LABEL` | The test's label, used to name the suite. |
| `--level LEVEL` | Level for cases that did not declare one. |

The wrapped command runs with `$RR_TRACE_FILE` set (in `$TEST_TMPDIR` when
available) and without `$XML_OUTPUT_FILE`, so it cannot overwrite the JUnit the
wrapper writes. If no test results can be parsed — the binary crashed before
running tests, say — the wrapper records one synthetic case carrying the exit
code and the tail of the output, so the failure is visible in the report.
