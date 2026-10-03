# Changelog

A short summary of each version. The detailed record — every option, code and
behaviour — is the [release notes](docs/release-notes.md) (published at
<https://studio-fug.github.io/rules_requirements/release-notes.html>); where
the two differ, the release notes are right and this file is the bug.

## 0.2.0 (unreleased)

Per-case runners and migration tooling, towards one test case, one
requirement. [Details](docs/release-notes.md#020-unreleased).

### Added

- `rr_node_test`: node:test files with one JUnit case per test
  (`verifies(t, id)`), Node 20+; one synthetic result on older Node.
- `rr_case.h` / `@rules_requirements//cc:case`: one JUnit case per test
  function for plain-assert C/C++ tests, each case in a forked child.
- `CheckPlan`: hardware runs as steps and single-requirement checks, with
  device, rig and harness stops recorded apart.
- `JUnitWriter`: `requirement=` (one id), `not_reached()`, `rr.file`,
  `write(append=True)`.
- `rr case` (one case from a shell script) and `rr wrap --format junit` /
  `rr_wrapped_test(format = "junit")`.
- Migration tooling: case keys and `rr cases`; `rr migrate plan` (the
  attribution worksheet); `rr migrate apply --stage tags` (rewrites Python
  test tags to the decided owners, verified with a pytest collection check).
- `rr --version`.

### Changed

- `rr_evidence` keeps tests in the action's process group; its timeout sends
  `SIGTERM`, then `SIGKILL`, and never waits on a pipe an escaped process holds.

### Deprecated

- Several requirement ids for one test case (pytest markers, `@rr.verifies`,
  `JUnitWriter` lists): still recorded, now with a
  `MultipleRequirementsWarning` (`DeprecationWarning`).

### Deferred to 0.3.0

- `--agent` for `rr migrate plan`.
- Label normalization when matching the model to evidence.

### Compatibility

- Additive: no verdict, report or model change; the report goldens are
  unchanged.
- 0.3.0 will quarantine multi-id cases (they count for no requirement), make
  a target named by two requirements a model error, and add per-case selectors.

## 0.1.0

The toolkit as first published: model, validation, evidence ingestion,
tracing, reports, hooks, Bazel rules, the thermostat example, the docs site and
the `rr serve` web editor.
