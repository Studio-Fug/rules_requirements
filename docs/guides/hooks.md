# Test hooks

A hook lets a test declare which entities it verifies, and at what level. Every
hook produces the same thing: **JUnit XML** in which each `<testcase>` carries
its traces as properties. That keeps the report independent of the test
framework, and lets anything that can write JUnit take part ({doc}`evidence`).

```{admonition} One test case, one requirement
:class: important

A test case verifies **at most one** requirement; a set of test cases may
verify one requirement. Declare one id per test case. A hook only *declares*
the id; which requirement a case verifies is decided by attribution, never by a
hook. The older multi-id forms (`@pytest.mark.rr("REQ-1", "REQ-2")`,
`@rr.verifies("REQ-1", "REQ-2")`, a list passed to `JUnitWriter`) still record
every id in 0.3, but are deprecated and warn with `MultipleRequirementsWarning`;
the case is then quarantined and counts for none of them — see
[Deprecated: several ids per test case](#multi-id-deprecation).
```

| Framework | Declare | Bazel | Without Bazel |
| --------- | ------- | ----- | ------------- |
| pytest | `@pytest.mark.rr("REQ-1", level="sil")` | `rr_py_test` | pip-installed plugin, `pytest --junitxml=...` |
| unittest | `@rr.verifies("REQ-1")` | `py_test` + `rr.unittest_main()` | `rr.unittest_main()` / `--junit-xml` |
| googletest | `RR_VERIFIES("REQ-1");` | `cc_test` + `@rules_requirements//cc:gtest` | `--gtest_output=xml:...` |
| plain-assert C++ | `RR_CASE(name, "REQ-1") { ... }` | `cc_test` + `@rules_requirements//cc:case` | `--rr_junit=...` |
| Rust | `rr::verifies!("REQ-1");` | `rr_rust_test` | `rr wrap -- <test binary>` |
| node:test | `verifies(t, "REQ-1")` or `t.diagnostic("rr.requirement=REQ-1")` | `rr_node_test` | — |
| hardware runs (steps + checks) | `CheckPlan` | `py_test` / `py_binary` | same |
| anything else (Python) | `JUnitWriter` | `py_test` / `py_binary` | write the XML yourself |
| shell scripts | `rr case --requirement REQ-1 ...` | `sh_test` with `@rules_requirements//python:rr` in `data` | `rr case` |
| runners writing their own JUnit | (their own) | `rr_wrapped_test(format = "junit")` | `rr wrap --format junit` |

(junit-properties)=
## The JUnit conventions

| Property | Meaning |
| -------- | ------- |
| `requirement` | The id the test case declares — one per case. (Repeated properties, and comma- or whitespace-separated values, are still read as several ids; that form is deprecated, and such a case is quarantined.) |
| `requirements` | 0.2's name for comma-separated ids (googletest's hook wrote it); still read, like `requirement`. |
| `rr.file` | The source file of the test code that produced the case, relative to the workspace (written by the pytest and unittest hooks, `JUnitWriter`, `CheckPlan` and `rr case --file`). Identifies the same test code run by several targets. |
| `rr.scope` | `target` for a result about the whole run rather than a test case (an `exit-status` error, a report that could not be read). It declares no requirement; it taints every case claimed on its target. |
| `rr.synthetic` | `true` for a target's single whole-run result, written when it produced no per-case results. |
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


@pytest.mark.rr("REQ-1")
def test_heats_below_band(): ...


@pytest.mark.rr("REQ-5", level="hil", artifact={"board_rev": "C"})
def test_cutoff_on_the_bench(): ...


@pytest.mark.requirements("REQ-3")  # alias
def test_parses_units(): ...
```

- The positional argument is the id. Several ids — several arguments, a
  comma- or whitespace-separated string, a list, or several markers at one
  scope naming different ids — are deprecated: every id is still recorded (so
  the case is quarantined), and the plugin warns once per declaring test (all
  its parameters), class or module ([details](#multi-id-deprecation)).
- `level=` names the level the test provides; `artifact=` a mapping of artifact
  identity keys.
- **The nearest scope wins.** Only the nearest scope that names an id is
  recorded, in this order: a `pytest.param(..., marks=...)` mark; the test
  function (its markers, a conftest's `item.add_marker`, `@rr.verifies`); its
  class (a subclass's own declaration before its base class's); the module's
  `pytestmark`, then a package's. A nearer id *replaces* a farther one: a
  module-level `pytestmark = pytest.mark.rr("REQ-10")` is a default that a
  test's own marker overrides, never an id added to it. (0.2 recorded the ids
  of every scope.)
- The nearest marker naming a level wins (`rr` and `requirements` markers alike),
  and artifact keys resolve nearest-first.
- Every case also records `rr.file`, the test file relative to the workspace.
- A raw `record_property("requirement", ...)` (or `"requirements"`) bypasses
  these rules: the plugin drops the property and fails the test with
  **RR-E102**. Declare the id with the marker.
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

`rr.verifies(id, level="", artifact=None)` records the trace on the function
or class. The nearest declaration wins: a method's decorator replaces its
class's, and a subclass's replaces its base class's (a level or artifact key
the nearer declaration does not set is still inherited). A `setUpClass` /
`setUpModule` error and a failing subtest carry the same single id as their
test, and every case records `rr.file`. Extra ids, or stacked decorators
naming different ids, are deprecated: every id is still recorded (so the case
is quarantined), with a warning at the decorated definition
([details](#multi-id-deprecation)).
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
  RR_VERIFIES("REQ-5");                          // the ONE id this test verifies
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

The id is recorded as the test's `requirement` property (0.2 wrote
`requirements`; both are read). Call it once, with one id: several ids, or
several calls naming different ids, are deprecated — `RecordProperty` keeps one
value per key, so they are recorded as one comma list, which is quarantined.
`RR_VERIFIES` in `SetUpTestSuite`, an `Environment` or `main` is still recorded
on the suite, but no test case inherits a suite-level requirement. The helpers
are also available as functions — `rules_requirements::Verifies({...})`,
`Level(...)`, `Artifact(key, value)`.

(rr-case-h)=
## Plain-assert C++: `rr_case.h`

Many C and C++ tests are a `main()` that calls test functions full of
`assert()`: the first failure aborts the binary, and Bazel can only report the
whole target. `rr_case.h` turns such a binary into one JUnit case per test
function, without googletest. The header is C++ (11 or later); a C-style
`assert()` test uses it when compiled as C++:

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
- **No shared state.** Each case starts from the parent as it was before the
  first case: a global, heap object or `chdir` set by one case is gone in the
  next, so a case must not depend on an earlier one (in a plain `main` it
  could). Start no threads before `rr::RunCases` — `fork` copies only the
  calling thread, so a lock another thread held stays locked in the child.
- **Child lifetime.** A child ends with `_exit`, so `atexit` handlers and
  static destructors run once, in the runner, never per case. A hung case
  does not outlive its runner: a `SIGTERM`, `SIGINT` or `SIGHUP` to the
  runner (an `rr_evidence` timeout sends `SIGTERM`, then `SIGKILL` after 2 s)
  kills the running case's child first, and on Linux the child also dies with
  a runner killed by `SIGKILL`. The signal then goes to what the program had
  for it: by default it ends the runner as the signal would have; a handler
  the program installed before `rr::RunCases` runs as it would have (if it
  returns, the runner goes on and reports the killed case as failed); a
  signal the program ignores stays ignored. A case starts with the runner's
  own signal mask and signal dispositions, as they were before
  `rr::RunCases`. Tests run by `rr_evidence` stay in the action's process
  group, so a cancelled build reaches them too.
- **Coverage** (Linux and other ELF targets). A child starts its case's
  counts from zero and writes them before it ends, so a line run in a case
  is counted once per run of it, and a line run before the cases once, in the
  test executable and in every instrumented shared library it loads (by
  default `bazel coverage` links a `cc_test`'s `cc_library` deps as shared
  libraries). That takes libgcov's `__gcov_reset` and `__gcov_dump`, which
  gcc links only on request: `@rules_requirements//cc:case` adds
  `-Wl,-u,__gcov_dump -Wl,-u,__gcov_reset` to the link under `bazel coverage`
  on Linux. A toolchain without a gcov runtime (no libgcov) cannot link those:
  pass `--@rules_requirements//cc:coverage_hooks=false` (default `true`) to
  `bazel coverage` and `//cc:case` adds no link options; the cases' coverage
  is then counted as described next for a build without them. A `--coverage`
  build of your own outside `bazel coverage` needs the same two link options;
  without them, code in a shared library that
  only cases run is not counted at all, and unless the test's own source is
  instrumented (`--instrument_test_targets`), code run before the cases is
  counted once more per case. Verified with gcc 13: shared and static links,
  the test's own source instrumented or not, and `bazel coverage` with Bazel
  7.7.1 and 8.8.1. Not tested: clang, with `--coverage` (compiler-rt defines
  `__gcov_dump` and `__gcov_reset` itself) or with
  `-fprofile-instr-generate` (`__llvm_profile_reset_counters`,
  `__llvm_profile_write_file`; `LLVM_PROFILE_FILE` needs `%p` or `%m`, as
  Bazel sets it). On macOS the children's coverage is lost.
- **Leaks.** Under LeakSanitizer (ELF targets) a case that leaks memory fails
  (`exited with status 1: rr_case: LeakSanitizer found memory leaked by this
  case`). The runner checks once before the first case: memory leaked before
  any case ran (in `main` or a static initializer) fails the binary without
  failing any case (`rr_case: LeakSanitizer found memory leaked before any
  case ran`), and the cases' own checks are then skipped, since a child's
  leak could no longer be told apart from it. On macOS the children's leak
  checks are lost.
- **Case keys.** Each case is `<testcase classname="<suite>" name="<case>">`,
  reported as `<suite>::<case>` (`improv_codec::wifi_settings_vector`). Case
  names must be unique within a suite; a duplicate is reported as an error.
- **One requirement per case.** A case names at most one id, recorded as its
  `requirement` property. `RR_CASE(name, "REQ-1", "REQ-2")` does not compile
  (`RR-E101`; with 16 or more ids the error is an unrelated one), nor does
  `RR_CASE(name, ("REQ-1", "REQ-2"))`. In the list form that comma
  expression compiles, with no warning at all unless `-Wall` or
  `-Wunused-value` is on, and records just `REQ-2`, so build such tests with
  `-Werror=unused-value`. An id that is
  empty or holds anything but ASCII letters, digits, `_`, `-` and `.` makes
  the case an error without running it (`RR-E104`). `rr_case.h` accepts ids
  matching `[A-Za-z0-9_.-]+` only: a project whose `config.id_pattern` allows
  other characters must use another hook. There is no call to add ids from
  inside a case. The id is optional: a case without one is still a
  test case in the report, and a whole-target `verified_by` reference covers it.
- **Evidence and annotations.** `RR_CASE(name, "REQ-1")` written on one line
  is also an annotation for `rr scan`; the list form's ids
  (`{"name", fn, "REQ-7"}`) are evidence only, like an `RR_CASE` that a
  formatter splits across lines. `RR_CASE` also records where the case is
  defined, as the `<testcase>`'s `file` and `line`.
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
    rr::verifies!("REQ-5");                       // the ONE id this test verifies
    assert!(interlock::trips(35.0));
}

#[test]
fn cutoff_is_logged() {
    rr::verifies!("REQ-6"; level = "sil");        // ...with a level
    assert!(interlock::logs_trip(35.0));
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
thread libtest runs it on, plus the id (`"requirement":"REQ-1"`) and level — to
the file named by `$RR_TRACE_FILE`. A deprecated call naming several ids writes
0.2's list form (`"requirements":[...]`, still read), and two calls naming
different ids write two lines: either way the case is quarantined. Without that variable the macro does nothing, so plain
`cargo test` is unaffected. The wrapper sets the variable, runs the tests,
parses libtest's standard output and writes JUnit with the traces attached:

- in Bazel, {ref}`rr_rust_test <rr-rust-test>` (a `rust_test` plus the
  wrapper), or {ref}`rr_wrapped_test <rr-wrapped-test>` around any libtest
  binary;
- elsewhere, `rr wrap --junit-xml rust.xml -- <test binary or command>` (see
  below).

A trace recorded on a thread the test spawns itself carries that thread's name,
matches no test and is dropped (with a warning); call the macro from the test's
own thread.

(node-test)=
## node:test

A JavaScript or TypeScript test file run by Node's built-in test runner
(`node:test`: `test()`, `it()`, `describe()`) becomes one JUnit case per test
with {ref}`rr_node_test <rr-node-test>`. Without it, a rules_js `js_test` runs
the file as a plain script and Bazel records one synthetic result for the whole
file — every `test()` in it shares one verdict.

```starlark
load("@aspect_rules_js//js:defs.bzl", "js_test")
load("@rules_requirements//rr:defs.bzl", "rr_node_test")

rr_node_test(
    name = "clocksync_test",
    rule = js_test,                                  # your rules_js js_test
    test = ":dist-test/tests/clocksync.test.js",     # compiled from TypeScript
    data = [":web_tests_js", ":dist_test_pkg_json"],
)
```

The file runs exactly as `js_test` runs it — `node <file>`, with rules_js's
node flags — and the target exits with node's own exit code, so the runner
never turns a failing test green or a passing one red. Two reporters are
attached: `spec` writes the usual log, and rules_requirements' reporter
records the cases, which are written to `$XML_OUTPUT_FILE` once node exits:

| node:test | JUnit case |
| --------- | ---------- |
| a test without subtests (`test()`, `it()`, `t.test()`) | `classname` = the file's stem plus the enclosing `describe`s and parent tests, joined with ` > `; `name` = the test's name |
| a `describe`, or a test with subtests | no case of its own — its leaves are the cases |
| passed / failed | `passed` / `failed`; a failure's message is the first line, its text the stack |
| `{ skip }`, `{ todo }`, `t.skip()`, `t.todo()` | `skipped`, with the reason as message |
| `describe.skip()`, `describe(..., { skip })` | one `skipped` case named after the describe — node never reports the tests inside it |

So `describe("bestSample", ...)` around `it("keeps the min-RTT sample", ...)`
in `clocksync.test.js` is the case `clocksync > bestSample::keeps the min-RTT
sample`. Two tests with one name stay two cases. Every case carries an
`rr.file` property: the workspace-relative file that defines the test — the
test file, or a helper module it requires. For TypeScript compiled to
JavaScript, that is the compiled file (`dist-test/...`), not the `.ts` source.

**Declaring traces.** A test declares what it verifies with a diagnostic:

```js
const { test } = require("node:test");
// rr_node_test sets RR_NODE_VERIFIES; the fallback keeps the file loadable
// elsewhere (`node --test`, an IDE), without the helper's guards.
const { verifies } = process.env.RR_NODE_VERIFIES
  ? require(process.env.RR_NODE_VERIFIES)
  : { verifies: (t, id) => t.diagnostic(`rr.requirement=${id}`) };

test("bestSample keeps the min-RTT sample", (t) => {
  verifies(t, "REQ-13");                  // one id; optional level: verifies(t, "REQ-13", "sil")
  // the same, by hand:
  // t.diagnostic("rr.requirement=REQ-13");
  // t.diagnostic("rr.level=sil");
  // t.diagnostic("rr.artifact.board_rev=C");
});
```

`rr.requirement=`, `rr.level=` and `rr.artifact.<key>=` diagnostics become the
case's `requirement`, `level` and `artifact.<key>` properties (other
diagnostics are left alone). They belong to the test that writes them — also
under `describe(..., { concurrency })` — and a diagnostic that cannot be tied
to a test, such as one from a `before()` / `after()` hook, is never guessed
onto one: it is reported as a warning in the test log. (A `beforeEach()` /
`afterEach()` hook's `t` is the test's own context, so its diagnostics do
belong to that test.) The `level` attribute of `rr_node_test` is the
default for cases that declare none.

`verifies(t, id, level?)` (`@rules_requirements//js:verifies.cjs`; under
`rr_node_test` its path is in `$RR_NODE_VERIFIES`) adds two guards to the
diagnostic: the id must be one id — no comma, no whitespace (`RR-E104`) — and a
test that already verifies one requirement cannot claim a second
(`RR-E101`); either mistake throws inside the test, which then fails. A test
case verifies at most one requirement: if raw diagnostics name several ids
anyway, every one is written (never a silent pick) and the test log carries an
`RR-E101` warning.

**Failures outside any test** are recorded as `error` cases with the property
`rr.scope=target` — they belong to the whole target, not to a test:

| Case | When |
| ---- | ---- |
| `<stem>::<load>` | node exited non-zero before reporting any test: the file threw while loading, or the process died. |
| `<stem>::<exit-status>` | node exited non-zero (or was killed) although no test failed — e.g. an unhandled rejection after the tests. |
| `<stem>::<file>` | a root-level `after()` hook failed. Node 22 and newer still exit 0 here, so Bazel passes the target; the report does not. |
| `<chain>::<hooks>` | a `describe` or a parent test failed outside its subtests (its own hook or body). |
| `<stem>::<incomplete>` | node exited 0 after node:test started reporting but before it finished — `process.exit(0)` in a test, which drops that test and every later one from the report. (An exit before node:test reported anything cannot be told from a file without tests.) |

The other root-level hooks fail tests instead: a failing root `before()` fails
every top-level test with the hook's error, and every top-level `describe`
gets a `<hooks>` case carrying it (its tests are cancelled); a failing root
`beforeEach()` / `afterEach()` fails every test it runs for.

A file that registers no test at all writes an empty suite (`tests="0"`).
Two tests that report as one case key — a `describe("a > b")` next to a
`describe("a")` holding a `describe("b")`, or `::` inside a name — get an
`rr_node_test: warning` in the test log naming both; rename one.
Because these cases name no requirement, they fail every whole-target
`verified_by` reference to the target, not the requirements its individual
tests name.

**Node versions.** The reporter needs `--test-reporter`, so Node 20 or newer
(rules_js's default toolchain is Node 22). On older Node, or with
`RR_NODE_TEST_PLAIN=1` in the test's environment, the file runs plainly and
the runner writes one result for the whole target with the property
`rr.synthetic=true` — what a plain `js_test` gives. rules_requirements' CI runs
the runner on Node 18 (the fallback), 20, 22 and 24.

The runner never decides the verdict itself: if it cannot write the report
(an unwritable `$XML_OUTPUT_FILE` directory, a full disk) it warns in the log
and still exits with node's code, and it passes `SIGTERM`, `SIGINT` and
`SIGHUP` on to the test process, so a timeout or an interrupt never leaves
that process running.

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
    with report.case("flash_and_boot", requirement="REQ-13"):
        flash(dut)  # raises -> recorded as failed, then re-raised
    with report.case("provision", requirement="REQ-29", level="hil"):
        provision(dut)
    report.add("ota_update", requirement="REQ-30", status="skipped", message="no OTA server on this rig")
finally:
    report.write(os.environ.get("XML_OUTPUT_FILE", "bench_e2e.xml"))
```

- `case(name, requirement=None, level="", artifact=None, *, classname="",
  file=None)` is a context manager that records the case as passed, or as
  failed with the exception's text if the block raises (the exception
  propagates, so control flow is unchanged).
- `add(name, requirement=None, status, message, duration, level, artifact,
  classname, *, file=None)` records a result directly; `status` is `passed`,
  `failed`, `error` or `skipped`.
- `requirement` is **one** id, or `None`. A string that is not one id — a comma
  or whitespace inside it, or empty — raises `ValueError` (RR-E104). The
  pre-0.2 list form (`report.case("x", ["REQ-13", "REQ-21"])`, or the
  `requirements=` keyword) still records every id it holds, verbatim, with a
  `DeprecationWarning` (a `MultipleRequirementsWarning` when it names more than
  one: the case is then quarantined).
- `cases` is a read-only tuple of frozen cases (`case.requirement`, and
  `case.requirements`, a read-only alias of the declared ids): a case cannot
  be removed or re-attributed once recorded. Record another case instead.
- `not_reached(names, reason, classname="", *, tags=None)` records planned cases
  a device failure kept from running, each as its own failed case
  `not reached: <reason>`; `tags` maps a name to the one id it verifies.
- The writer's `artifact` identity is stamped on every case (merged with any
  per-case `artifact`), which is what makes stale bench results detectable.
- The writer's `file` — by default the running script, `sys.argv[0]`, relative
  to the workspace (the `*.runfiles/<repo>/` or `bazel-out/<cfg>/bin/` prefix
  stripped) — is written as each case's `rr.file` property; pass `file=""` to
  write none, or `file=` per case to override it.
- `write(path, append=False)` writes the JUnit; with `append=True` the cases
  are added to the file already at `path` (to the suite of the same name, else
  as a new suite), replacing it atomically; on POSIX systems concurrent
  appends are serialised by a lock on the file — or, where the file itself
  cannot be locked (a read-only file on NFS, a dangling symlink), on a sidecar
  `.<name>.lock` next to it, removed again. A symlink to a file is followed
  for the lock, then replaced by the new file like the file itself.

(checkplan)=
## Hardware runs: `CheckPlan`

An on-hardware run is a sequence of *steps* — flash, boot, provision, connect —
each followed by assertions. A step is an action: it verifies nothing by
itself. A *check* is one assertion, recorded as one JUnit case that verifies at
most one requirement. {py:class}`~rules_requirements.hooks.checkplan.CheckPlan`
records a run in those terms, including how it stopped:

```python
import os

from rules_requirements.hooks.checkplan import CheckPlan
from rules_requirements.hooks.junit_writer import JUnitWriter

STEPS = {
    "flash_boot": ("ble_advertising",),
    "improv_provision": ("provisioned",),
    "websocket_checks": ("ws_connect", "build_info", "rename"),
    "run": ("completed",),
}
TAGS = {  # "<step>.<check>" -> ONE id
    "flash_boot.ble_advertising": "REQ-13",
    "improv_provision.provisioned": "REQ-13",
    "websocket_checks.ws_connect": "REQ-13",
    "websocket_checks.build_info": "REQ-35",
    "websocket_checks.rename": "REQ-13",
    "run.completed": "REQ-23",
}

report = JUnitWriter("hitl_e2e", default_level="hitl")
plan = CheckPlan(report, STEPS, tags=TAGS, is_infrastructure=is_rig_trouble)
try:
    with plan.run():
        reserve_rig()  # setup: any failure here is rig or setup trouble
        plan.setup_done()
        with plan.step("flash_boot"):
            flash(dut)  # the action: a failure here is the device's
            with plan.check("ble_advertising"):
                assert BLE_MARKER in serial_log()
        with plan.step("improv_provision"):
            provision(dut)
            plan.passed("provisioned")
        with plan.step("websocket_checks"):
            ...
        with plan.step("run"):
            plan.passed("completed")
finally:
    report.write(os.environ["XML_OUTPUT_FILE"])
```

Each check becomes the case `<suite>.<step>::<check>` —
`hitl_e2e.websocket_checks::rename` — carrying its tag, if any. `check(name)`
records a pass, or a failure with the exception's text (and re-raises);
`passed`, `failed` and `skipped` record a result directly, by the check's name
in the current step or as `"<step>.<check>"`. Inside the check's own
`with plan.check(name):` block, such a result is the check's only case — the
block's end adds no pass, and a later exception no failure:

```python
with plan.check("board_caps"):
    if descriptor is None:
        plan.skipped("board_caps", "no capability descriptor on this board")
    else:
        assert descriptor.ok
```

Step names cannot contain `.`, which separates the step from the check.

How a run is recorded when it stops:

| The run | Recorded |
| ------- | -------- |
| ends normally | Every check as it went. A planned check never recorded is an `error`, "planned check never executed (harness bug)". |
| stops on a **device failure**: any exception after `setup_done()` (entering a step implies it) that `is_infrastructure` does not claim | The check that raised, as failed; every check not run yet — the rest of the step and all later steps — as failed, `not reached: <step> failed: <exception>`. Each requirement fails through its own checks. |
| stops on **rig or setup trouble**: an exception before `setup_done()`, or one `is_infrastructure` claims | One untagged `<suite>::rig` error case; every check not run yet as skipped, `not run: rig trouble: <exception>`. Nothing is failed; checks that already passed stay passed. |
| is **interrupted or exits cleanly**: `KeyboardInterrupt`, or `SystemExit` with code 0 or `None` | As rig trouble, whatever `is_infrastructure` says. A clean exit after every check was recorded adds nothing. (`SystemExit` with another code is classified like any exception.) |
| hits a **harness bug**: an unknown step or check name, a check outside a step, a check recorded twice | An untagged `<suite>::harness` error case, `HarnessError` raised, and every check not run yet as `error`. |

The exception is re-raised in every case, so the harness exits as it would
without the plan. `is_infrastructure` defaults to claiming nothing else: pass
the harness's own classifier (reservation errors, ssh's own exit 255...).

```{note}
**Rig trouble and partial runs.** A requirement is verified only when every
case in its verification set passed, so after rig trouble the skipped checks
leave a requirement that also has passed checks INCOMPLETE: neither VERIFIED
nor FAILED. The passed checks keep their tags. (0.2, without verification
sets, withdrew the tags of those passed checks instead; 0.3 dropped that.)
```

## Shell harnesses: `rr case`

`rr case` appends one test case to a JUnit file — `$XML_OUTPUT_FILE` by default
— creating it if needed, so a shell script can record its own results:

```bash
rr case --name "flash ok" --status passed --requirement REQ-21 --level hitl
rr case --name "boot banner" --status failed --message "no banner after 30 s" \
        --requirement REQ-22 --artifact firmware_build_id="$BUILD_ID" --file "$0"
```

| Option | Meaning |
| ------ | ------- |
| `--name NAME` | The case's name (required). |
| `--status` | `passed` (default), `failed`, `error` or `skipped`. |
| `--requirement ID` | The **one** id the case verifies; given twice it is an error (RR-E101), as is a malformed id (RR-E104). |
| `--classname`, `--suite` | Default: the suite, which defaults to the name part of `$TEST_TARGET`, else `rr`. |
| `--level`, `--artifact KEY=VALUE` | As for the other hooks. |
| `--message`, `--duration` | Failure or skip message; seconds. |
| `--out PATH` | The JUnit file (default `$XML_OUTPUT_FILE`). |
| `--file PATH` | The test code's source file, recorded as `rr.file`. |

`rr case` exits 0 whatever the case's status — the script's own exit status
still decides whether the test passed — and 2 when it cannot record the case
(no output file, more than one id, a malformed id or `--artifact`, an
unreadable existing file). The file is replaced atomically, and on POSIX
systems concurrent appends (`rr case ... &`) are serialised by a lock on it
(or on a sidecar `.<name>.lock`, as for `JUnitWriter`), so none is lost; on
Windows they are not.

## `rr wrap`

`rr wrap` (also `python -m rules_requirements.hooks.wrap`) runs a command,
echoes its output, converts it to traceability JUnit and exits with the
command's own exit code, so it never turns a failing test green or a passing one
red:

```console
$ rr wrap --junit-xml rust.xml -- ./target/debug/deps/setpoint-1a2b3c --test-threads=4
$ rr wrap --format junit --junit-in out/junit.xml -- ./run_bench.sh
```

| Option | Meaning |
| ------ | ------- |
| `--format` | The command's output: `libtest` (default) for Rust test binaries, or `junit` for a runner that writes JUnit itself to `--junit-in`. |
| `--junit-in PATH` | With `--format junit`: where the runner writes its report. `$VARS` are expanded (`'${TEST_TMPDIR}/junit.xml'`); a relative path is relative to the working directory. A report left there by an earlier run is deleted before the command starts. |
| `--junit-xml PATH` | Where to write JUnit (default `$XML_OUTPUT_FILE`). |
| `--suite NAME` | Suite name (default: the name part of `--target`, else the binary's file name). |
| `--target LABEL` | The test's label, used to name the suite. |
| `--level LEVEL` | Level for cases that did not declare one. |

The wrapped command runs with `$RR_TRACE_FILE` set (in `$TEST_TMPDIR` when
available) and without `$XML_OUTPUT_FILE`, so it cannot overwrite the JUnit the
wrapper writes. If no test results can be parsed — the binary crashed before
running tests, say — the wrapper records one synthetic case carrying the exit
code and the tail of the output, so the failure is visible in the report.

If the command exits non-zero although every reported test passed (a
sanitizer, a crash after the last test), an `exit-status` error case is added.
It declares **no** requirement — not the ids the run traced — and is
target-scope (`rr.scope=target`): it taints every case claimed on the target,
so each requirement fails through its own cases. A test that traced and then
died without reporting a result is recorded as an error carrying its own
declared id. `rr_evidence`'s `test.exit.xml` works the same way.

With `--format junit` the runner's report is copied to the output as it is.
`--level` becomes the default level of its suites (a case's own `level` wins).
If the runner exits non-zero although no case in its report failed, the same
`exit-status` error case is added as for libtest; if it wrote no report at all,
or a well-formed file that is not JUnit, one error case says so, whatever its
exit code. A report without a single case from a run that exited 0 is
replaced by one synthetic passed case, as for a libtest binary that printed
nothing, so the run still leaves evidence.

(multi-id-deprecation)=
## Deprecated: several ids per test case

A test case verifies at most one requirement. In 0.3 every hook still accepts
the older multi-id forms and records every id, but warns:
the Python hooks with
{py:class}`~rules_requirements.hooks.ids.MultipleRequirementsWarning` (a
`DeprecationWarning`), the others with an RR-E101 line on stderr:

| Hook | Deprecated form | Warns |
| ---- | --------------- | ----- |
| pytest | a marker with several ids, several markers at one scope naming different ids, a marker and `@rr.verifies` on the same function or class naming different ids, or a `pytest.param` mark with several ids | once per declaring test (all its parameters), class or module, at the marker's line, when the first test it applies to sets up; listed in pytest's warnings summary |
| unittest | `@rr.verifies("A", "B")`, `"A, B"`, or stacked decorators naming different ids | at the decorated definition |
| `JUnitWriter` | a list, tuple or other iterable naming several ids, positionally or as `requirements=` | at the `add` / `case` call |
| googletest | `RR_VERIFIES("A", "B")`, or several `RR_VERIFIES` calls in one test naming different ids | on stderr (in the test log), when the test gains its second id |
| Rust | `rr::verifies!("A", "B")`, or several calls in one test naming different ids | `rr wrap` / `rr_rust_test`, on stderr, once per test |

Ids at several scopes — a module-level `pytestmark` plus a function's own
marker, a `pytest.param` mark plus the function's marker, a class decorator
plus a method decorator, or a subclass's `@rr.verifies` plus its base class's —
are not a multi-id declaration and do not warn: the nearest one wins, and only
its id is recorded.

A case whose evidence names several ids is quarantined: it counts for no
requirement, and every requirement it names reads INVALID. 0.4 rejects
multi-id declarations outright. Split such a test into one test per requirement, or
keep the one id it really verifies. To find every remaining use, turn the
warning into an error: `pytest -W error::DeprecationWarning`, or
`python -W error::DeprecationWarning` for a script. Under pytest a marker
declaration then errors the first test it applies to, at setup, and the rest of
the session runs; but `@rr.verifies` warns when it decorates, at import, and a
`JUnitWriter` call made at import time warns there too, so an escalated
warning from either is a collection error that interrupts the whole session.

The hooks name the problem with a stable code:

| Code | Meaning |
| ---- | ------- |
| RR-E101 | One case names more than one id. |
| RR-E102 | A raw `requirement` property bypassed the single-id API (pytest `record_property`). |
| RR-E103 | `RR_VERIFIES` was called outside a running test (0.4). |
| RR-E104 | Malformed id: a comma, whitespace, or empty. |
