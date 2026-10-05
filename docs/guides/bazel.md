# Bazel rules

```starlark
load(
    "@rules_requirements//rr:defs.bzl",
    "rr_annotations_check",
    "rr_annotations_test",
    "rr_editor",
    "rr_evidence",
    "rr_golden_test",
    "rr_model",
    "rr_node_test",
    "rr_py_test",
    "rr_report",
    "rr_rust_test",
    "rr_sets_lock_test",
    "rr_wrapped_test",
)
```

See {doc}`../getting-started` for the `MODULE.bazel` setup. Besides the rules,
the module provides these targets:

| Target | What |
| ------ | ---- |
| `@rules_requirements//python` | The Python library (`py_library`, standard library only). |
| `@rules_requirements//python:rr` | The `rr` CLI; also `@rules_requirements//:rr` and simply `@rules_requirements` (`bazel run @rules_requirements -- validate requirements/`). |
| `@rules_requirements//cc:gtest` | The googletest hook (`#include "rr_gtest.h"`). |
| `@rules_requirements//cc:case` | Per-case JUnit for plain-assert C++ tests (`#include "rr_case.h"`). |
| `@rules_requirements//cc:coverage_hooks` | Flag, default `true`: under `bazel coverage` on Linux, `//cc:case` links libgcov's dump/reset hooks. `--@rules_requirements//cc:coverage_hooks=false` for a toolchain without a gcov runtime ({ref}`rr-case-h`). |
| `@rules_requirements//rust:rr` | The Rust hook crate (`rr`). |
| `@rules_requirements//js:verifies.cjs` | The node:test `verifies(t, id)` helper (dependency-free CommonJS). |
| `@rules_requirements//:schema/rules_requirements.schema.json` | The model's JSON Schema. |
| `@rules_requirements//:schema/report.v2.schema.json` | The JSON report's schema (`rules_requirements/report/v2`). |

Under `bazel run`, the CLI resolves relative paths against the directory you ran
Bazel from.

## Model

### `rr_model`

```starlark
rr_model(
    name = "model",
    srcs = glob(["requirements/**/*.yaml"]),
    strict = False,
    lock = "requirements/verification.rrlock",
)
```

Declares the model files and, unless `validate = False`, a `<name>_test` that
runs `rr validate` on them. The test writes one JUnit case per check family
(`rr.validate::shape`, `::references`, `::coverage-rules`, `::claims`,
`::lock`), so a model test claimed by a requirement is per-case evidence.

| Attribute | Default | |
| --------- | ------- | - |
| `srcs` | required | Model files (`.yaml`, `.yml`, `.json`), merged. |
| `strict` | `False` | Treat validation warnings as errors in `<name>_test`. |
| `validate` | `True` | Create `<name>_test`. |
| `lock` | `None` | The verification-set lock (`rr sets lock --write`). `<name>_test` checks it statically against the claims (`lock-invalid`, `lock-owner-changed`, `lock-stale`), and `rr_report` pins the sets with it. A model whose `config.sets_lock` names a lock needs it here: a Bazel action only sees its declared inputs. |
| `visibility` | | Visibility of the model target. |
| `**kwargs` | | Forwarded to the validation test (`tags`, `size`, ...). |

The target provides `RrModelInfo(srcs, lock)` and its files as `DefaultInfo`
(the lock is in its runfiles, not its files).

(rr-annotations-test)=
### `rr_annotations_test`

```starlark
rr_annotations_test(
    name = "annotations_test",
    model = ":model",
    srcs = glob(["src/**/*.py", "src/**/*.rs"]),
)
```

Fails if an annotation in `srcs` names an id the model does not define
({doc}`annotations`). Only the listed files are scanned, so the test is
hermetic and cached.

### `rr_annotations_check`

```starlark
rr_annotations_check(
    name = "check_annotations",
    model = ":model",
)
```

The non-hermetic counterpart for annotations that span many packages:
`bazel run :check_annotations` scans the whole source tree `bazel run` was
invoked in (git-aware, so ignored files are skipped) and fails on any
annotation naming an unknown id. Extra `rr scan` flags go after `--`
(`-- --list`, `-- --exclude 'third_party/*'`).

### `rr_editor`

```starlark
rr_editor(
    name = "editor",
    model = ":model",
    paths = ["requirements"],  # optional: open the directory (one-object-per-file layouts)
)
```

`bazel run :editor` starts the web editor ({doc}`web-editor`) on the model.
Model and evidence paths are relative to the workspace root, so the editor edits
the real files, never runfiles copies.

| Attribute | Default | |
| --------- | ------- | - |
| `model` | required | The `rr_model` target. |
| `paths` | the model's files | Workspace-relative files or directories to open instead. |
| `evidence` | `["bazel-testlogs"]` | Workspace-relative evidence paths. |
| `args` | `[]` | Extra `rr serve` flags (`--port=9000`, `--no-llm`, ...). |
| `deps` | `[]` | Extra Python dependencies: your pip hub's `anthropic` (e.g. `@pypi//anthropic`) enables the LLM-backed agent workflows. |

## Test hooks

### `rr_py_test`

```starlark
rr_py_test(
    name = "controller_test",
    srcs = ["tests/test_controller.py", "tests/conftest.py"],
    deps = [":thermostat", requirement("pytest")],
)
```

A `py_test` that runs pytest with the `rr` marker plugin and writes JUnit to
`$XML_OUTPUT_FILE`. pytest is not bundled: add your workspace's pytest to
`deps`.

| Attribute | Default | |
| --------- | ------- | - |
| `srcs` | required | Files named `test_*.py` or `*_test.py` are passed to pytest; other files (`conftest.py`, helpers) are support files. If no file matches, all `srcs` are passed. |
| `deps` | `[]` | Dependencies, including pytest. `@rules_requirements//python` is added. |
| `args` | `[]` | Extra pytest arguments, baked into the generated main (see [below](#generated-mains)). |
| `data` | `[]` | Runtime data. |
| `**kwargs` | | Forwarded to `py_test` (`size`, `imports`, `tags`, ...). |

(rr-wrapped-test)=
### `rr_wrapped_test`

```starlark
rr_wrapped_test(
    name = "parser_test",
    test = ":parser_test_bin",     # usually tagged "manual"
    level = "sil",
)
```

Runs a test executable through `rr wrap` ({doc}`hooks`), converting its output
to traceability JUnit and preserving its exit code.

A runner that writes JUnit itself, to a fixed path (a Go or JavaScript test
runner, a hardware harness using `CheckPlan`), uses `format = "junit"`; the
wrapper passes its report on to Bazel and adds the exit-status taint:

```starlark
rr_wrapped_test(
    name = "bench_test",
    test = ":bench_runner",
    format = "junit",
    junit_in = "${TEST_TMPDIR}/bench/junit.xml",   # where the runner writes
    level = "hitl",
)
```

| Attribute | Default | |
| --------- | ------- | - |
| `test` | required | The test executable. |
| `format` | `"libtest"` | Its output format: `libtest`, or `junit`. |
| `junit_in` | `""` | With `format = "junit"` (and only then, required): the path the executable writes its JUnit to. `$VARS` are expanded at run time; relative paths are relative to the test's working directory. |
| `level` | `""` | Level for cases that do not declare one. |
| `args` | `[]` | Extra arguments for the executable. |
| `**kwargs` | | Forwarded to the wrapper `py_test`. |

(rr-rust-test)=
### `rr_rust_test`

```starlark
load("@rules_rust//rust:defs.bzl", "rust_test")

rr_rust_test(
    name = "setpoint_test",
    rule = rust_test,
    crate = ":setpoint",
    deps = ["@rules_requirements//rust:rr"],
)
```

A Rust test whose `rr::verifies!(...)` calls become JUnit traces. Creates
`<name>_bin` — the real `rust_test`, tagged `manual` — and `<name>`, the wrapper
that `bazel test` runs. rules_requirements does not load rules_rust itself:
pass the `rust_test` rule as `rule`.

| Attribute | Default | |
| --------- | ------- | - |
| `rule` | required | The `rust_test` rule. |
| `level` | `""` | Level for cases that do not declare one. |
| `tags`, `size`, `timeout`, `flaky`, `visibility` | | Applied to the wrapper test. |
| `args` | `[]` | Arguments for the test binary (after `--`), e.g. `--test-threads=1`. |
| `env`, `env_inherit` | | Environment of the wrapper, inherited by the test binary. |
| `**kwargs` | | Forwarded to the `rust_test` (`srcs`, `crate`, `deps`, `edition`, ...). |

The wrapper also turns a crash into evidence: when the binary fails, a test
that recorded traces but never reported a result (an abort mid-run) becomes an
`error` case carrying its own declared id, and a binary that exits non-zero
after every test passed gets an `exit-status` error case. That case declares no
requirement: it is target-scope (`rr.scope=target`) and taints every case
claimed on the target.
It understands `--nocapture` output (the result on a line of its own). Call
`rr::verifies!` on the test's own thread: traces from spawned threads or async
runtimes match no test and are dropped (the wrapper warns about them).

(rr-node-test)=
### `rr_node_test`

```starlark
# MODULE.bazel: bazel_dep(name = "aspect_rules_js", version = "3.2.2")  # or newer;
# its default Node 22 toolchain is enough: no toolchain or npm setup is needed.
load("@aspect_rules_js//js:defs.bzl", "js_test")

rr_node_test(
    name = "clocksync_test",
    rule = js_test,
    test = ":dist-test/tests/clocksync.test.js",
    data = [":web_tests_js", ":dist_test_pkg_json"],
)
```

A node:test file with one JUnit case per test ({ref}`node-test`). The macro
creates `<name>`, a `js_test` whose entry point is a generated
`<name>.rr_node_main.cjs`: it runs the test file in a child node — with the
same node flags, environment and exit code — and writes the JUnit. A copy of
the reporter, `<name>.rr_node_reporter.mjs`, sits next to it (rules_js runs
entry points from the output tree). rules_requirements does not load rules_js
itself: pass the `js_test` rule as `rule`. Target names are yours, so swapping
a `js_test` for an `rr_node_test` changes no label, `test_suite` or CI command.

| Attribute | Default | |
| --------- | ------- | - |
| `rule` | required | The `js_test` rule from `@aspect_rules_js//js:defs.bzl`. |
| `test` | required | The node:test file: a source file of this package, or a generated one (e.g. a `ts_project` output). |
| `data` | `[]` | Runtime data — the rest of the compiled sources, their `package.json`. |
| `args` | `[]` | Arguments for the test file, baked into the entry point (`$(location)` of `data` is expanded). |
| `level` | `""` | Level for cases that do not declare one. |
| `**kwargs` | | Forwarded to the `js_test` (`size`, `tags`, `env`, `timeout`, ...). |

googletest needs no macro: a `cc_test` depending on
`@rules_requirements//cc:gtest` writes traced JUnit by itself, and so does a
plain-assert `cc_test` depending on `@rules_requirements//cc:case`
({ref}`rr_case.h <rr-case-h>`).

## Evidence and reports

### `rr_evidence`

```starlark
rr_evidence(
    name = "evidence",
    tests = [":controller_test", ":interlock_test", ":setpoint_test"],
)
```

Runs the tests **inside a build action** and collects their JUnit in a directory
laid out like `bazel-testlogs` (`<name>/testlogs/<pkg>/<test>/test.xml`, with
each test's output in a `test.log` beside it), so every result keeps its test's
label. Failing tests do not fail the build — they are evidence, and the report
shows them as FAILED. The output is cached like any other action: the tests run
again only when something they depend on changes.

| Attribute | Default | |
| --------- | ------- | - |
| `tests` | required | Test targets to run. |
| `timeout` | `300` | Per-test timeout in seconds; a test that exceeds it gets `SIGTERM` (then `SIGKILL` 2 s later) and is recorded as failed. |
| `local` | `False` | Add `no-remote-exec` to the action. |
| `testonly` | `True` | |

Each test runs with its runfiles directory (`<exe>.runfiles/<workspace>`) as
working directory and an environment modelled on `bazel test`: `TEST_SRCDIR`,
`RUNFILES_DIR`, `TEST_WORKSPACE`, `TEST_TARGET`, `XML_OUTPUT_FILE`,
`TEST_TMPDIR` (also `HOME` and `TMPDIR`), `TEST_UNDECLARED_OUTPUTS_DIR`,
`PATH` and `LANG`, plus the test's own `env` attribute. A test that writes no
JUnit gets one synthetic test case carrying its exit status, as under
`bazel test`; a test that exits non-zero although its report shows no failure
gets an extra `exit-status` error case. A test's `args` attribute is not
available to other rules, which is why the macros in this module bake their
arguments into a generated `main` instead.

The tests are built in the **exec** configuration, because the action runs them
on the build machine. (Using the target configuration would reuse the binaries
`bazel test` builds, but with Bazel 7's test-configuration trimming a non-test
rule that depends on tests conflicts with the tests' own actions.) The cost is a
second build of the tests. Tests that need the network, devices, or anything else
the sandbox does not provide — hardware-in-the-loop suites, typically — do not
belong in `rr_evidence`: run them with `bazel test` and aggregate the real
`bazel-testlogs` with the CLI (see {doc}`integration`). The target provides
`RrEvidenceInfo(testlogs)`.

### `rr_report`

```starlark
rr_report(
    name = "report",
    model = [":model"],
    evidence = [":evidence", "evidence/panel_inspection.rr.yaml"],
    srcs = glob(["src/**/*.py"]),
)
```

Renders the report as `<name>.html`, `<name>.json` and `<name>.md`, addressable
as `:<name>.json` and so on.

| Attribute | Default | |
| --------- | ------- | - |
| `model` | required | `rr_model` targets or model files. |
| `evidence` | `[]` | `rr_evidence` targets, JUnit files, records files. |
| `srcs` | `[]` | Sources to scan for annotations: adds implementation links and `no-implementation` gaps. |
| `formats` | `["html", "json", "md"]` | Which outputs to build. |
| `title` | `""` | Report title (default: the project name). |
| `strict` | `False` | Fail on model **and attribution** warnings too (`rr report --strict`): every warning-level attribution issue — `coarse-claim`, `same-path-multiple-owners`, `unscoped-evidence`, `tag-mismatch`, ... — then fails the build, with no report written. |
| `current_build` | `{}` | Current artifact identity; evidence recorded against another is stale. |
| `check` | `"json" in formats` | Also create `<name>_check_test`: `rr check-report` re-proves from `<name>.json` alone that no test case is owned by two entities. On by default whenever the JSON report is built; `check = False` opts out; `check = True` without `"json"` in `formats` is an error. |
| `lane` | `""` | The lane the evidence comes from (`rr report --lane`), stamped into the report. |
| `lane_targets` | `None` | A file listing the targets that lane runs, one label per line (`--lane-targets`): not-run members of other targets read "out of lane". Verdicts never change. |
| `on_attribution_error` | `"fail"` | A quarantined test case (several ids, several claimants, one test code with several owners) fails the build (`rr report` exits 3); `"warn"` builds the report anyway, where the case still counts for nobody and every entity it names reads INVALID. |
| `testonly` | `True` | The evidence comes from tests. |

The build fails if the model is invalid, if a test case is quarantined (unless
`on_attribution_error = "warn"`), and if an attribution issue is an error
(`lock-owner-changed`, a rule set to `error`, or with `strict` any warning).
An `rr_model` with a `lock` pins the sets. The target provides
`RrReportInfo(json, lane, on_attribution_error, lock)`.

### `rr_sets_lock_test`

```starlark
rr_sets_lock_test(
    name = "lock_test",
    model = ":model",  # rr_model(lock = "verification.rrlock")
    evidence = [":evidence"],
)
```

Runs `rr sets check` on the lock the model pins (`rr_model(lock)`, the one
`rr_report` reads): fails on a case the lock expects but the evidence does
not hold (`missing-case`), an owned case the lock does not list
(`unlocked-member`), an owner change and a stale entry. `lock` names the lock
only for a model that has none (model files, or an `rr_model` without
`lock`); analysis fails when it differs from the model's. `bazel run
:lock_test.update` rewrites that lock in the source tree from the same evidence
(`rr sets lock --write`; append `-- --allow-removals` to drop entries the
evidence no longer has, after checking the run was complete). For hermetic
projects whose evidence comes from `rr_evidence`; with real `bazel-testlogs`
run `rr sets check` / `rr sets lock` from the CLI. To start a lock, create an
empty `lock` file and run the `.update` target.

The thermostat example wires the three together the way a product should: an
`rr_model` with `lock`, an `rr_report` with the default
`on_attribution_error = "fail"` and `check = True`, and an
`rr_sets_lock_test` over the same evidence, so `bazel test //...` fails on a
shared claim (`:model_test`), a quarantined case (`:report`), a report that
breaks the partition (`:report_check_test`) and a set that changed without
its lock (`:sets_lock_test`):

```{literalinclude} ../../examples/thermostat/BUILD.bazel
:language: starlark
:start-at: "EVIDENCE = ["
:end-before: "rr_golden_test("
```

### `rr_golden_test`

```starlark
rr_golden_test(
    name = "report_golden_test",
    src = ":report.json",
    golden = "report.golden.json",
)
```

Compares a generated file with a checked-in golden and prints a unified diff
when they differ. `bazel run :report_golden_test.update` rewrites the golden
from the current output. Pinning `report.json` (or `report.md`) this way makes
every change to traceability — a requirement losing its evidence, a test
starting to fail, a risk's status changing — show up in code review.

(generated-mains)=
## Generated mains

The helper tests (`<model>_test`, `rr_annotations_test`, `rr_py_test`,
`rr_wrapped_test`, `rr_node_test`, `rr_golden_test`) do not use the `args` attribute: Bazel
passes `args` only under `bazel test` / `bazel run`, so a test run by
`rr_evidence` would silently lose them. Instead each macro generates a small
`<name>.rr_main.py` with the arguments baked in (runfiles-relative paths,
resolved against the working directory at run time), and uses it as the test's
`main` (`rr_node_test`: `<name>.rr_node_main.cjs`, its `entry_point`, with the
test file's path baked in too). The tests therefore behave identically under `bazel test`, `bazel run`
and `rr_evidence`.

## Compatibility

Tested with Bazel 7.7.1 and 8.8.1 using bzlmod. The module's dependency floors
are `rules_python` 2.0.3, `rules_cc` 0.2.22, `rules_rust` 0.71.3 and `googletest`
1.17.0; newer versions in your workspace win. Python 3.9 or newer.
`rr_node_test` is tested with `aspect_rules_js` 3.2.2 (its default Node 22) and
its runner with Node 18 (the fallback), 20, 22 and 24; rules_js is not a dependency of the module.
