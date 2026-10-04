# Release notes

This page is the detailed record of each release. `CHANGELOG.md` at the
repository root summarises each version in a few lines and links here.

## 0.2.1 (2026-10-05)

A patch release for projects migrating to one requirement per test case
whose tests run under Bazel. Nothing else changes: verdicts, reports and the
report goldens are byte-identical to 0.2.0.

### New: `rr migrate verify`

- **`rr migrate verify --worksheet W.rrplan --evidence NEW [--baseline OLD]
  [--allow-missing] [--model DIR]`** ({ref}`migrate-verify`) checks the test
  evidence of a run after `rr migrate apply` against the worksheet, case by
  case key: every decided case must declare exactly its owner (no id for
  `none`); with `--baseline`, every other case must declare the ids it had
  before (in any order; a target-scope result's are not compared) and no
  case of the baseline may be missing. A case with no result is an error;
  `--allow-missing` (a HITL or manual target CI does not run) makes it a
  warning when its target has no result at all in the new evidence, never
  when the target ran (a case renamed or lost by the rewrite). A decided
  case that declared no id before and declares none after counts only
  through `verified_by` (a model edit apply does not make): it is listed in
  a note, not an error; with a model (`--model`, or the worksheet's), the
  note says whether `verified_by` gives its target exactly its owner or the
  case is pending a model edit, and also lists decided cases whose target
  `verified_by` still gives to other requirements. Without a model only
  declared ids are checked. One line per offending case (expected and found
  ids) and exit `1`; exit `0` with a summary; exit `2` for an unreadable or
  invalid worksheet, evidence paths holding none, or evidence that names no
  build target while the worksheet's cases belong to build targets (JUnit
  files outside a `bazel-testlogs` / `testlogs` directory are keyed by
  suite name). It reads every evidence
  shape `rr` ingests (pytest, `rr_node_test`, `rr_case.h`, googletest, ...).
  It is the verification path where the collection check cannot run — Bazel
  `py_test` targets importing through their runfiles: apply with
  `--no-collect-check`, push, then verify against the CI evidence before
  merging.

### Fixed

- `rr migrate apply --stage tags`: the static guards no longer judge code
  that never runs when pytest imports a test module — the body of an
  `if __name__ == "__main__":` block (either operand order, either quote; not
  its `else`) and the bodies of module-level functions reachable only from
  it. A script-style test module whose `main()` loads a helper with
  `importlib.util.spec_from_file_location` refused every rewritten file in
  the tree, because an import call the codemod cannot name may import any of
  them. Such a function is still judged when anything that may run at import
  time names it (a module-level call, an alias, a `getattr` / `globals()`
  string, a test, a default argument), when it is decorated, rebound or named
  like a test or a hook, or when another scanned file imports it, passes its
  module around, reads it from `sys.modules` or names it on a pytest item's
  `.module` / `.obj`. Nothing in the file is excluded when it may rebind
  `__name__`, when code that may run at import looks names up dynamically
  (`globals()`, `vars()`, `eval`, a computed `getattr`,
  `inspect.getmembers`, `__dict__`, `sys.modules`, a frame), or when the
  block launches the tests in-process (`pytest.main()`, `unittest.main()`:
  a `py_test` whose main is the file runs the block before collection); nor
  anywhere when another scanned file may reach any module's functions
  without importing it (a computed `sys.modules` read, a frame, `eval` /
  `exec`, a `pytest_pycollect_makeitem` hook, a computed lookup on a pytest
  item's module).

## 0.2.0 (2026-10-04)

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
  `after()`, a failing `describe` hook, a clean exit before node:test finished
  reporting) become `error` cases with `rr.scope=target`; two tests that report
  as one case key get a warning in the log. Needs Node 20 or newer for per-case
  results; on Node 18,
  or with `RR_NODE_TEST_PLAIN=1`, the file runs plainly and one result with
  `rr.synthetic=true` covers the target. rules_requirements does not depend on
  rules_js (it is a dev dependency for its own fixtures): a consumer adds
  `bazel_dep(name = "aspect_rules_js", version = "3.2.2")` (or newer), whose
  default Node toolchain is enough. CI runs the runner on Node 18, 20, 22 and
  24.
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
  did not. `rr::RunCases` may be called more than once in one binary (new
  `RR_CASE` cases next to a converted list): the JUnit holds every call's
  suite. Under `bazel coverage` on Linux, `//cc:case` links libgcov's
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
  bound or imported dynamically at import or class-creation time, a file
  another file imports from or subclasses — refuses the change with exit 1,
  and by default nothing is written (`--partial` writes the files not linked
  to a refusal). Code in test, fixture and `setUp`-style hook bodies runs
  after collection and is not refused. `--dry-run` prints a diff; `--only
  PATH` limits the rewrite; `--model` checks the decided owners exist.
- **The collection check** (on by default): whenever apply would write
  anything, `--partial` and `--dry-run` included, it copies the tree under
  `--root` to a temporary directory, writes the rewritten files there (never
  in place), and runs `python -m pytest --collect-only` in both trees from
  `--root`, so the project's ini and `testpaths` decide what is collected.
  Each decided case the write settles must end with exactly its owner, every
  other test must keep exactly the ids it had, and the set of collected tests
  must not change; otherwise apply refuses, writes nothing and names each
  offending test with its before, after and expected ids. A collection error
  in either tree refuses too and prints pytest's output. This catches what the
  static guards cannot see: tests installed by a factory, a metaclass,
  `__init_subclass__`, `setattr` or `exec`.
  - `--python PATH`: the interpreter whose pytest and project dependencies
    collect the tests (default: the one running `rr`); only
    `rules_requirements` itself is added to its `PYTHONPATH`.
  - `--pytest-args "..."`: extra arguments for both collections, one
    shell-quoted string (`-c pytest.ini`, `-p my_project.plugin`,
    `--ignore=scripts`); `--pytest-args=--ignore=x` and `--pytest-args
    "--ignore=x"` both work.
  - `--no-collect-check`: write on the static guards alone, with a warning;
    for projects pytest cannot collect at all.
  - Limits: the check proves only what collects in its own environment (the
    `--python` interpreter, its packages, the current environment variables
    and the plugins named in `--pytest-args`): a test built only when `HW=1`
    is set, or a plugin your runner loads with `-p` that `--pytest-args`
    leaves out, is not checked. Collectors skipped at collection time
    (`pytest.importorskip`) are listed in a warning.
  - Symlinks fail closed: one resolving inside the tree is followed in both
    collections; one to a config or data file outside it is copied with its
    content; one reaching a test outside it (a directory, a `.py` file) that
    pytest would recurse into refuses and names the path. Apply never writes
    through a symlink.
- A draft guide, {doc}`guides/migrating-to-per-case`, walks through the
  steps: collect evidence, plan, decide, rewrite the tags, edit the model.
- `rr migrate apply` resolves a case only to a module pytest collects
  (`python_files`), and only to the source its evidence names (`rr.file`, as
  the worksheet's case rows carry it) when it names one: a C++ or Rust case
  whose path merely reads like a Python module, or a case of a record, is "not
  a Python test" and left alone.
- Ingest records the enclosing `<testsuite>` name as `TestCase.suite` (a new
  field after `properties`, so positional construction keeps working) and marks
  Bazel's generated result `rr.synthetic=true`. A `<testcase>`'s `file`
  attribute (`rr_case.h`, googletest) is its source when no `rr.file`
  property names one.
- Our own writers mark their whole-target results the same way, so one target
  has one case key whichever wrote it: `rr_evidence`'s result for a test that
  wrote no JUnit and `rr wrap`'s for a run that left no case are
  `rr.synthetic=true` (`[target]`); the `exit-status` case of `rr_evidence` and
  `rr wrap` (both formats), and `rr wrap`'s error for a missing or non-JUnit
  report, are `rr.scope=target`, so `rr migrate plan` lists them under
  `target_scope` instead of asking an owner to decide them. Their ids, and so
  0.2 verdicts, are unchanged.

### New: other

- `rr --version` prints the installed version.

### Changed

- `JUnitWriter`: a bare id string is one id (`add("a", "REQ-12")`), where
  0.1.0 split a string given as the list into characters (`R`, `E`, `Q`...);
  an empty or blank string is still no requirement, as in 0.1.0.
- `rr wrap --format libtest` prints an RR-E101 warning on stderr for each case
  that records several ids (`rr::verifies!` with two ids, or two calls naming
  different ids), and googletest's `RR_VERIFIES` does the same when a test
  gains its second id. Every id is still recorded.
- unittest (`rr.unittest_main`): a test whose subtest failed also gets a
  failed case under its own key (unittest reports no outcome for it), so the
  key reads failed, not missing.
- `rr_evidence` runs each test in the action's process group, so a cancelled
  build or the action's own timeout reaches the test and everything it
  started. On its own timeout the test gets `SIGTERM`, then `SIGKILL` after a
  2 s grace; the test's output is read on a thread, so a process the test left
  behind holding the output pipe never blocks the action. No verdict changes.

### Fixed

- `rr_wrapped_test` with a `py_binary` (or any target building more than its
  executable) as `test` failed analysis under Bazel 7 ("expands to more than
  one file"); the wrapper now runs the target's executable, and, for a
  checked-in script, a genrule output or a filegroup of one file (which have
  none), that one file, as 0.1.0 did.
- Labels from `bazel-testlogs` paths: a target whose name starts with `run_`,
  `shard_` or `attempt_` (`//pkg:run_tests`) lost its name to its package
  (`//:pkg`), and a package with a directory named `testlogs`
  (`//x/testlogs:y_test`) lost everything above it. Only Bazel's own run
  directories (`shard_1_of_4`, `run_2_of_3`, `test_attempts`) are skipped now,
  and the leftmost testlogs root is used. Reports show the right label for
  such targets, which changes their text.
- The pytest runner (`rr_py_test`) passes `--rootdir` set to the runfiles
  tree, so an ini file above it (the execroot's `pyproject.toml` in a local,
  unsandboxed run) no longer puts `bazel-out/<cfg>/bin/...runfiles` into
  every classname.
- `rr migrate apply --dry-run` ends with "N file(s) would be rewritten", not
  "N file(s) rewritten".
- `rr wrap` rejected its own options when they came first, as documented
  (`rr wrap --junit-xml rust.xml -- ./test`, `rr wrap --help`: "unrecognized
  arguments"); only `python -m rules_requirements.hooks.wrap` and the Bazel
  macros worked. Everything after `wrap` now goes to the wrapper.

### Deprecated

Declaring several requirement ids for one test case. Every id is still
recorded, exactly as before, but the hooks warn with
`rules_requirements.hooks.ids.MultipleRequirementsWarning`, a
`DeprecationWarning` ({ref}`multi-id-deprecation`):

- a pytest `rr` / `requirements` marker naming several ids (or several markers
  at one scope naming different ids, a marker and `@rr.verifies` on one
  function or class naming different ids, or a `pytest.param` mark naming
  several);
- `@rr.verifies` with several ids, or stacked decorators naming different ids;
- a `JUnitWriter` list, tuple or other iterable naming several ids,
  positionally or as `requirements=`. Lists of zero or one id are accepted
  silently;
- googletest `RR_VERIFIES` and Rust `rr::verifies!` naming several ids for one
  test (in one call or several): an RR-E101 line on stderr, not a Python
  warning.

The pytest marker warning is raised as the first test a marker applies to sets
up, so `-W error::DeprecationWarning` errors that test rather than the whole
session. `@rr.verifies` warns when it decorates, at import, and a `JUnitWriter`
call made at import warns there: escalated, either is a collection error that
interrupts the session.

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
