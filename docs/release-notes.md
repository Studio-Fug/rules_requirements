# Release notes

## 0.2.0 (unreleased)

Per-case runners and migration tooling, towards **one test case, one
requirement**: a test case verifies at most one requirement, and a set of test
cases may verify one requirement. This release is additive — no verdict,
report or model semantics change, and reports built from the same evidence are
byte-identical.

### New

- **`CheckPlan`** ({ref}`checkplan`): a hardware run as ordered steps (actions,
  owned by no requirement) and checks (one JUnit case each, at most one
  requirement each). A device failure fails every check it kept from running,
  each through its own case; rig or setup trouble records one untagged
  `<suite>::rig` error and skips the checks that never ran; a planned check
  never executed is an error. Re-exported from
  `rules_requirements.hooks.junit_writer`.
- **`JUnitWriter`**: a singular `requirement=` (one id; a malformed one raises
  RR-E104); `not_reached(names, reason)` for planned cases a device failure kept
  from running; the harness's source file, written as the `rr.file` property
  (by default `sys.argv[0]`, relative to the workspace); `write(path,
  append=True)`.
- **`rr case`**: append one test case to a JUnit file from a shell script, with
  one `--requirement` at most.
- **`rr wrap --format junit --junit-in PATH`** and
  **`rr_wrapped_test(format = "junit", junit_in = ...)`**: pass on the JUnit a
  runner writes to a fixed path, adding the same exit-status taint as for
  libtest.

### Deprecated

Declaring several requirement ids for one test case. Every id is still
recorded, exactly as before, but the hooks warn with
`rules_requirements.hooks.ids.MultipleRequirementsWarning`, a
`DeprecationWarning` ({ref}`multi-id-deprecation`):

- a pytest `rr` / `requirements` marker naming several ids (or several markers
  at one scope naming different ids);
- `@rr.verifies` with several ids, or stacked decorators naming different ids;
- a `JUnitWriter` list or tuple naming several ids, positionally or as
  `requirements=`. Lists of zero or one id are accepted silently.

From 0.3 a case naming several ids counts for no requirement; 0.4 rejects such
declarations.
