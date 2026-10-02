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
| Rust | `rr::verifies!("REQ-1");` | `rr_rust_test` | `rr wrap -- <test binary>` |
| node:test | `verifies(t, "REQ-1")` or `t.diagnostic("rr.requirement=REQ-1")` | `rr_node_test` | — |
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

The other root-level hooks fail tests instead: a failing root `before()` fails
every top-level test with the hook's error, and every top-level `describe`
gets a `<hooks>` case carrying it (its tests are cancelled); a failing root
`beforeEach()` / `afterEach()` fails every test it runs for.

A file that registers no test at all writes an empty suite (`tests="0"`).
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
