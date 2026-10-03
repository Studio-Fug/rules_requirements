# Release notes

This page is the detailed record of each release. `CHANGELOG.md` at the
repository root summarises each version in a few lines and links here.

## 0.2.0 (unreleased)

Per-case runners and migration tooling, towards **one test case, one
requirement**: a test case verifies at most one requirement, and a set of test
cases may verify one requirement. This release is additive — no verdict,
report or model semantics change, and reports built from the same evidence are
byte-identical (the thermostat and integration report goldens are unchanged).

### New: per-case runners

- **`rr_node_test`** ({ref}`node-test`, {ref}`rr-node-test`): a node:test file
  (`test()`, `it()`, `describe()`) becomes one JUnit case per test instead of
  one result for the whole file. Pass your rules_js `js_test` as `rule`; the
  file runs as `js_test` runs it and the target exits with node's own exit
  code. Tests declare their one requirement with `verifies(t, "REQ-1")`
  (`@rules_requirements//js:verifies.cjs`, path in `$RR_NODE_VERIFIES`; it
  throws on a malformed id, RR-E104, or a second id, RR-E101) or with raw
  `rr.requirement=` / `rr.level=` / `rr.artifact.<key>=` diagnostics. Every
  case carries `rr.file`, the file that defines the test. Failures outside any
  test (a load error, a non-zero exit with no failed test, a failing root
  `after()`, a failing `describe` hook) become `error` cases with
  `rr.scope=target`. Needs Node 20 or newer for per-case results; on Node 18,
  or with `RR_NODE_TEST_PLAIN=1`, the file runs plainly and one result with
  `rr.synthetic=true` covers the target. rules_requirements does not depend on
  rules_js (it is a dev dependency for its own fixtures); CI runs the runner on
  Node 18, 20, 22 and 24.
- **`rr_case.h`** and **`@rules_requirements//cc:case`** ({ref}`rr-case-h`):
  one JUnit case per test function for plain-assert C/C++ tests, without
  googletest. `RR_CASE(name[, "REQ-1"])` defines a case, `RR_CHECK(expr)` is an
  `assert()` kept under `NDEBUG`, and `rr::RunCases(argc, argv, suite[,
  {{"name", fn, "REQ-1"}, ...}])` runs them — the list form converts an
  existing `main()` without moving its functions. On POSIX each case runs in
  its own forked child, so a failing `assert()`, a crash or an uncaught
  exception fails that case only; the binary still exits 1 if any case failed.
  More than one id per case does not compile (RR-E101); a malformed id makes
  the case an error (RR-E104). JUnit goes to `$XML_OUTPUT_FILE` or
  `--rr_junit=PATH`; `--rr_list` and `--rr_case=NAME` list cases and run one
  in-process for a debugger; `--test_filter` and sharding are honoured. A run
  killed mid-case still reports the cases that finished and names the one that
  did not. Under `bazel coverage` on Linux, `//cc:case` links libgcov's
  `__gcov_dump` and `__gcov_reset` so each child's counts are kept; a
  toolchain without a gcov runtime turns that off with
  `--@rules_requirements//cc:coverage_hooks=false`. LeakSanitizer leaks fail
  the case that leaked.
- **`rr scan`** recognises `RR_CASE(name, "REQ-1")`, written on one line, as a
  verifies annotation bound to the case.

### New: hooks for one requirement per test case

- **`CheckPlan`** ({ref}`checkplan`): a hardware run as ordered steps (actions,
  owned by no requirement) and checks (one JUnit case each, at most one
  requirement each). A device failure fails every check it kept from running,
  each through its own case; rig or setup trouble records one untagged
  `<suite>::rig` error and skips the checks that never ran; a planned check
  never executed is an error; an operator's `KeyboardInterrupt` or a clean
  `SystemExit` is never the device's; a harness bug records a `<suite>::harness`
  error and raises `HarnessError`. On rig trouble the 0.2 plan also withdraws
  the tag of each passed check whose requirement is the tag of a check that
  never ran, so a partial run reads neither VERIFIED nor FAILED (0.3 drops
  this; the verdict stays the same). Re-exported from
  `rules_requirements.hooks.junit_writer`.
- **`JUnitWriter`**: a singular `requirement=` (one id; a malformed one raises
  RR-E104); `not_reached(names, reason)` for planned cases a device failure kept
  from running; the harness's source file, written as the `rr.file` property
  (by default `sys.argv[0]`, relative to the workspace; `file=""` writes none);
  `write(path, append=True)`, which adds the cases to the file already there
  and replaces it atomically. On POSIX, concurrent appends are serialised by a
  lock on the file, or on a sidecar `.<name>.lock` where the file cannot be
  locked (a read-only file on NFS, a dangling symlink); on Windows they are
  not.
- **`rr case`**: append one test case to a JUnit file (`$XML_OUTPUT_FILE` by
  default) from a shell script, with one `--requirement` at most. It exits 0
  whatever the case's status and 2 when it cannot record the case. Appends are
  locked as for `JUnitWriter.write(append=True)`.
- **`rr wrap --format junit --junit-in PATH`** and
  **`rr_wrapped_test(format = "junit", junit_in = ...)`**: pass on the JUnit a
  runner writes to a fixed path, adding the same exit-status taint as for
  libtest; a missing or non-JUnit report becomes one error case.

### New: migration tooling

- **Case keys and `rr cases`**: a test case is identified by `<target>#<path>`
  (the label that ran it and `<classname>::<name>`, NFC, with `[rr:ID]` name
  tags stripped). Bazel's generated report for a target that wrote no JUnit is
  recognised and becomes the single `[target]` case. Attempts, runs, shards and
  evidence roots fold into one row per key (the final attempt decides; an
  earlier failure marks it flaky). `rr cases --evidence ... [--target T]
  [--json]` lists every key with status, declared ids, flags and source file,
  without a model.
- **`rr migrate plan`**: the attribution worksheet. It computes today's union
  semantics over a model and its evidence and lists every case that counts
  toward two or more entities, grouped by target and test module/class, plus
  every target named by two or more requirements. Owners record one decision
  per group (`owner: REQ-11`, `none` or `?`) with per-case overrides. Output is
  YAML (`.rrplan`), JSON (`--json`) and Markdown (`--md`); `--merge` carries
  earlier decisions over; `model_edits` lists the `verified_by` removals and
  splits the decisions imply. Schema: `schema/worksheet.schema.json`, also
  published with the docs.
- **`rr migrate apply WORKSHEET --stage tags`**: rewrites pytest markers
  (decorators, module and class `pytestmark`) and `@rr.verifies` so each
  decided test names exactly its owner, laid out as black would and in the
  file's own encoding and line endings. It fails closed: whatever it cannot
  resolve — an undecided multi-id test, a declaration it cannot read, a test
  bound or imported dynamically, a file another file imports from or
  subclasses — refuses the change with exit 1, and by default nothing is
  written (`--partial` writes the files not linked to a refusal). Before
  anything is written, every rewrite is verified with a pytest collection
  check: the tests are collected in the original tree and in a copy holding
  the rewritten files, and any test whose ids differ from the worksheet's
  decision (or from its own, for an undecided test) refuses the run.
  `--dry-run` prints a diff; `--only PATH` limits the rewrite.
- A draft guide, docs/guides/migrating-to-per-case.md, walks through the
  steps: collect evidence, plan, decide, rewrite the tags, edit the model.
- Ingest records the enclosing `<testsuite>` name as `TestCase.suite` (a new
  field after `properties`, so positional construction keeps working) and marks
  Bazel's generated result `rr.synthetic=true`.

### New: other

- `rr --version` prints the installed version.

### Changed

- `rr_evidence` runs each test in the action's process group, so a cancelled
  build or the action's own timeout reaches the test and everything it
  started. On its own timeout the test gets `SIGTERM`, then `SIGKILL` after a
  2 s grace; the test's output is read on a thread, so a process the test left
  behind holding the output pipe never blocks the action. No verdict changes.

### Deprecated

Declaring several requirement ids for one test case. Every id is still
recorded, exactly as before, but the hooks warn with
`rules_requirements.hooks.ids.MultipleRequirementsWarning`, a
`DeprecationWarning` ({ref}`multi-id-deprecation`):

- a pytest `rr` / `requirements` marker naming several ids (or several markers
  at one scope naming different ids);
- `@rr.verifies` with several ids, or stacked decorators naming different ids;
- a `JUnitWriter` list, tuple or other iterable naming several ids,
  positionally or as `requirements=`. Lists of zero or one id are accepted
  silently.

The pytest warning is raised as the first test a marker applies to sets up, so
`-W error::DeprecationWarning` errors that test rather than the whole session.

From 0.3 a case naming several ids counts for no requirement; 0.4 rejects such
declarations.

### Deferred to 0.3.0

- `rr migrate plan --agent` (agent-proposed owners): owners decide every case
  by hand in 0.2.0.
- Label normalization: treating `@@//p:n`, `@//p:n`, `//p` and canonical
  `~` / `+` module-repository names as one target when the model is matched
  to evidence, with a `bad-target` check for anything else.

### Compatibility

- Additive: existing models, evidence, hooks, macros and reports behave as in
  0.1.0, and the report goldens are byte-identical. Everything new is opt-in.
- `JUnitWriter` accepts both `requirement=` and the old positional list or
  `requirements=`.
- Multi-id declarations keep their union semantics and only warn; a project
  without `filterwarnings = error` sees no test change.
- 0.3.0 is the semantic release: a case whose evidence names several ids is
  quarantined (it counts for no requirement), two requirements naming one
  target becomes a model error, and the model gains per-case selectors. Run
  `rr migrate plan` and `rr migrate apply --stage tags` on 0.2.0 first; every
  step is valid under 0.2.0's rules.
