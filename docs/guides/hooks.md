# Test hooks

A hook lets a test declare which entities it verifies, and at what level. Every
hook produces the same thing: **JUnit XML** in which each `<testcase>` carries
its traces as properties. That keeps the report independent of the test
framework, and lets anything that can write JUnit take part ({doc}`evidence`).

```{admonition} One test case, one requirement
:class: important

A test case verifies **at most one** requirement; a set of test cases may
verify one requirement. Declare one id per test case. The older multi-id forms
(`@pytest.mark.rr("REQ-1", "REQ-2")`, `@rr.verifies("REQ-1", "REQ-2")`, a list
passed to `JUnitWriter`) still record every id in 0.2, but are deprecated and
warn with `MultipleRequirementsWarning` — see [Deprecated: several ids per test case](#multi-id-deprecation).
```

| Framework | Declare | Bazel | Without Bazel |
| --------- | ------- | ----- | ------------- |
| pytest | `@pytest.mark.rr("REQ-1", level="sil")` | `rr_py_test` | pip-installed plugin, `pytest --junitxml=...` |
| unittest | `@rr.verifies("REQ-1")` | `py_test` + `rr.unittest_main()` | `rr.unittest_main()` / `--junit-xml` |
| googletest | `RR_VERIFIES("REQ-1");` | `cc_test` + `@rules_requirements//cc:gtest` | `--gtest_output=xml:...` |
| Rust | `rr::verifies!("REQ-1");` | `rr_rust_test` | `rr wrap -- <test binary>` |
| hardware runs (steps + checks) | `CheckPlan` | `py_test` / `py_binary` | same |
| anything else (Python) | `JUnitWriter` | `py_test` / `py_binary` | write the XML yourself |
| shell scripts | `rr case --requirement REQ-1 ...` | `sh_test` with `@rules_requirements//python:rr` in `data` | `rr case` |
| runners writing their own JUnit | (their own) | `rr_wrapped_test(format = "junit")` | `rr wrap --format junit` |

(junit-properties)=
## The JUnit conventions

| Property | Meaning |
| -------- | ------- |
| `requirement` | The id the test case verifies — one per case. (Repeated properties and comma-separated values are still read, as several ids; that form is deprecated.) |
| `requirements` | Comma-separated ids (the form googletest's single-valued `RecordProperty` needs). |
| `rr.file` | The source file of the test code that produced the case, relative to the workspace (written by `JUnitWriter`, `CheckPlan` and `rr case --file`). Identifies the same test code run by several targets. |
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
  comma- or space-separated string, a list, or several markers at one scope
  naming different ids — are deprecated: every id is still recorded, and the
  plugin warns once per declaring test, class or module
  ([details](#multi-id-deprecation)).
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

`rr.verifies(id, level="", artifact=None)` records the trace on the function
or class, and a method's own level wins over its class's. Extra ids, or stacked
decorators naming different ids, are deprecated: every id is still recorded,
with a warning at the decorated definition ([details](#multi-id-deprecation)).
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

`RecordProperty` keeps one value per key, so the ids of a test (call it once,
with one id) are accumulated
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
  `requirements=` keyword) still records every id it holds, verbatim, and warns
  when it names more than one.
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
  as a new suite).

(checkplan)=
## Hardware runs: `CheckPlan`

An on-hardware run is a sequence of *steps* — flash, boot, provision, connect —
each followed by assertions. A step is an action: it verifies nothing by
itself. A *check* is one assertion, recorded as one JUnit case that verifies at
most one requirement. {py:class}`~rules_requirements.hooks.checkplan.CheckPlan`
records a run in those terms, including how it stopped:

```python
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
in the current step or as `"<step>.<check>"`.

How a run is recorded when it stops:

| The run | Recorded |
| ------- | -------- |
| ends normally | Every check as it went. A planned check never recorded is an `error`, "planned check never executed (harness bug)". |
| stops on a **device failure**: any exception after `setup_done()` (entering a step implies it) that `is_infrastructure` does not claim | The check that raised, as failed; every check not run yet — the rest of the step and all later steps — as failed, `not reached: <step> failed: <exception>`. Each requirement fails through its own checks. |
| stops on **rig or setup trouble**: an exception before `setup_done()`, or one `is_infrastructure` claims | One untagged `<suite>::rig` error case; every check not run yet as skipped, `not run: rig trouble: <exception>`. Nothing is failed; checks that already passed stay passed. |
| hits a **harness bug**: an unknown step or check name, a check outside a step, a check recorded twice | An untagged `<suite>::harness` error case, `HarnessError` raised, and every check not run yet as `error`. |

The exception is re-raised in every case, so the harness exits as it would
without the plan. `is_infrastructure` defaults to claiming nothing: pass the
harness's own classifier (reservation errors, ssh's own exit 255, an operator's
`KeyboardInterrupt`...).

```{note}
**v0.2 behaviour on rig trouble.** In 0.2 a skipped case does not stop a
requirement from reading VERIFIED, so on rig trouble the plan also withdraws
the tag of every *passed* check whose requirement is also the tag of a check
that never ran. A partial run therefore leaves such a requirement neither
VERIFIED nor FAILED. From 0.3, where a requirement is verified only when every
case in its verification set passed, the skipped checks do this by themselves
and the withdrawal goes away; the verdict is the same.
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

`rr case` exits 0 whatever the case's status: the script's own exit status
still decides whether the test passed. Appends are not safe from concurrent
processes.

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

With `--format junit` the runner's report is copied to the output as it is.
`--level` becomes the default level of its suites (a case's own `level` wins).
If the runner exits non-zero although no case in its report failed, the same
`exit-status` error case is added as for libtest; if it wrote no report at all,
one error case says so, whatever its exit code.

(multi-id-deprecation)=
## Deprecated: several ids per test case

A test case verifies at most one requirement. In 0.2 every hook still accepts
the older multi-id forms and records every id, exactly as before, but warns
with {py:class}`~rules_requirements.hooks.ids.MultipleRequirementsWarning` (a
`DeprecationWarning`):

| Hook | Deprecated form | Warns |
| ---- | --------------- | ----- |
| pytest | a marker with several ids, or several markers at one scope naming different ids | once per declaring test, class or module, in pytest's warnings summary |
| unittest | `@rr.verifies("A", "B")`, `"A, B"`, or stacked decorators naming different ids | at the decorated definition |
| `JUnitWriter` | a list or tuple naming several ids, positionally or as `requirements=` | at the `add` / `case` call |

Ids that accumulate across scopes — a module-level `pytestmark` plus a
function's own marker, or a class decorator plus a method decorator — are not
a multi-id declaration and do not warn in 0.2.

From 0.3, a case whose evidence names several ids counts for no requirement
(and every requirement it names reads INVALID); 0.4 rejects multi-id
declarations outright. Split such a test into one test per requirement, or
keep the one id it really verifies. To find every remaining use, turn the
warning into an error: `pytest -W error::DeprecationWarning`, or
`python -W error::DeprecationWarning` for a script.

The hooks name the problem with a stable code:

| Code | Meaning |
| ---- | ------- |
| RR-E101 | One case names more than one id. |
| RR-E104 | Malformed id: a comma, whitespace, or empty. |
