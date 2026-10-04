# Changelog

A short summary of each version. The detailed record — every option, code and
behaviour — is the [release notes](docs/release-notes.md) (published at
<https://studio-fug.github.io/rules_requirements/release-notes.html>); where
the two differ, the release notes are right and this file is the bug.

## 0.2.1 (2026-10-05)

Unblocks applying a worksheet in Bazel projects.
[Details](docs/release-notes.md#021-2026-10-05).

### Added

- `rr migrate verify`: checks the test evidence of a run after
  `rr migrate apply` against the worksheet — every decided case declares
  exactly its owner; with `--baseline`, every other case keeps its ids and
  none disappears (`--allow-missing` for decided cases CI does not run). The
  verification path where the collection check cannot run (Bazel `py_test`s):
  apply with `--no-collect-check`, push, verify against the CI evidence.

### Fixed

- `rr migrate apply`: the static guards skip the `if __name__ == "__main__":`
  block and the functions only it reaches, which never run when pytest
  imports the module; an import call in a script-style test's `main()` no
  longer refuses every file. Nothing is skipped when the file may rebind
  `__name__`, looks names up dynamically at import, or launches its tests
  in-process from the block.

## 0.2.0 (2026-10-04)

Per-case runners and migration tooling, towards one test case, one
requirement. [Details](docs/release-notes.md#020-2026-10-04).

### Added

- `rr_node_test`: node:test files with one JUnit case per test
  (`verifies(t, id)`), Node 20+; one synthetic result on older Node.
- `rr_case.h` / `@rules_requirements//cc:case`: one JUnit case per test
  function for plain-assert C/C++ tests, each case in a forked child. Under
  `bazel coverage` on Linux it links libgcov's dump/reset hooks;
  `--@rules_requirements//cc:coverage_hooks=false` turns that off for a
  toolchain without a gcov runtime.
- `CheckPlan`: hardware runs as steps and single-requirement checks, with
  device, rig and harness stops recorded apart.
- `JUnitWriter`: `requirement=` (one id), `not_reached()`, `rr.file`,
  `write(append=True)`.
- `rr case` (one case from a shell script) and `rr wrap --format junit` /
  `rr_wrapped_test(format = "junit")`.
- Migration tooling: case keys and `rr cases`; `rr migrate plan` (the
  attribution worksheet); `rr migrate apply --stage tags` (rewrites Python
  test tags to the decided owners). Every write, `--dry-run` and `--partial`
  included, is first proven by a pytest collection check in a copy of the
  tree, and refused if any test's ids would change other than as decided;
  `--python` and `--pytest-args` set up that collection, `--no-collect-check`
  skips it. A symlink reaching a test outside the tree that pytest would
  follow refuses; tests collected only in another environment are outside the
  check.
- `rr --version`.
- `rr scan` recognises `RR_CASE(name, "ID")` as a verifies annotation.
- Ingest: the public `TestCase.suite` field; Bazel's generated result, and our
  writers' whole-target results, are marked `rr.synthetic=true` (one
  `[target]` case), their exit-status errors `rr.scope=target`; a testcase's
  `file` attribute fills `rr.file`.
- Public modules `rules_requirements.case_keys` and
  `rules_requirements.hooks.ids`; the worksheet schema
  (`schema/worksheet.schema.json`), published with the docs.

### Changed

- `rr_evidence` keeps tests in the action's process group; its timeout sends
  `SIGTERM`, then `SIGKILL`, and never waits on a pipe an escaped process holds.
- `JUnitWriter`: a bare id string is one id (0.1.0 split it into characters);
  `""` is still no requirement.
- unittest: a test whose subtest failed also gets a failed case of its own.

### Fixed

- `rr_wrapped_test` accepts a `py_binary` as `test` under Bazel 7, and still
  accepts a checked-in script or a genrule output, as 0.1.0 did.
- Labels of targets named `run_*` / `shard_*` / `attempt_*`, or in a package
  with a `testlogs` directory, are recovered correctly from `bazel-testlogs`.
- The pytest runner pins `--rootdir` to the runfiles tree under Bazel.
- `rr::RunCases` called twice in one binary keeps both suites in the JUnit.
- `rr migrate apply` resolves cases only to pytest test modules, and never a
  non-Python case to a Python file.
- `rr wrap --junit-xml ... -- CMD` and `rr wrap --help` work (the CLI
  rejected options before the command).

### Deprecated

- Several requirement ids for one test case (pytest markers, `@rr.verifies`,
  `JUnitWriter` lists): still recorded, now with a
  `MultipleRequirementsWarning` (`DeprecationWarning`); googletest
  `RR_VERIFIES` and Rust `rr::verifies!` warn on stderr (RR-E101).

### Deferred to 0.3.0

- `--agent` for `rr migrate plan`.
- Label normalization when matching the model to evidence.

### Compatibility

- Additive: no verdict, report or model change; the report goldens are
  unchanged. The new warnings are `DeprecationWarning`s: a project with
  `filterwarnings = error` gets its multi-id tests erroring, which reads as
  FAILED evidence.
- 0.3.0 will quarantine multi-id cases (they count for no requirement), make
  a target named by two requirements a model error, and add per-case selectors.

## 0.1.0

The toolkit as first published: model, validation, evidence ingestion,
tracing, reports, hooks, Bazel rules, the thermostat example, the docs site and
the `rr serve` web editor.
