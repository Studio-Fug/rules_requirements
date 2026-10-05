# Release notes

This page is the detailed record of each release. `CHANGELOG.md` at the
repository root summarises each version in a few lines and links here.

## 0.3.0 (2026-10-05)

**One owner per test case.** A test case verifies at most one requirement; a
set of test cases may together verify one; and the tool is built so that one
test case *cannot* verify two ({doc}`one-test-case-one-requirement`).
Ownership is decided in one function, `attribution.attribute()`, from the
model's claims and the evidence's declared tags; ambiguity quarantines the
case (it owns nothing, every entity it names reads INVALID, `rr report` exits
3); and `rr check-report` re-proves the partition from a published JSON
report alone.

This is the semantic release that 0.2 prepared: verdicts change, on purpose.
Projects that followed {doc}`guides/migrating-to-per-case` on 0.2 (single-id
tags, decided owners) bump the pin, fix the targets two requirements still
share, and keep the default `attribution: hybrid`; the guide's second half
then moves them to `attribution: model` with a lock. The
[thermostat example](https://github.com/Studio-Fug/rules_requirements/tree/main/examples/thermostat)
shows the end state.

### Added

- **Claims in the model** ({ref}`claims`). `verified_by` on requirements and
  mitigations, and the new `validated_by` on user needs, take items
  `{target, cases: [selector, ...]}` or `{target, whole: true, reason}`, with
  an optional `level`. Selectors match whole case paths, with `*` as the only
  wildcard (`\*` and `\\` escape; `?`, `[` and `]` are literal, so pytest ids
  work). Labels are compared in one spelling (`@@//p:n`, `@//p:n`,
  `@<config.main_repo>//p:n`, `//p`, canonical `+`/`~` module names).
  `verified_by` on mitigations and `validated_by` on user needs share the
  requirements' claim namespace.
- **`config` keys** `attribution` (`hybrid` | `model`), `main_repo`,
  `sets_lock`, `flaky` (`accept` | `flag` | `under-verify` | `fail`),
  `set_consistency` (`off` | `warn` | `enforce`) and `variants` ({ref}`config-reference`).
- **Validation of claims** ({ref}`rules`): `shared-case` (two entities can
  select one case; decided exactly, with an example case path), the same
  check across a `variants` group (`same-code-multiple-owners`),
  `bad-selector`, `bad-target`, `lock-invalid`, `lock-owner-changed`, and
  with `rr validate --known-targets FILE`, `unknown-target`. These are hard
  errors that no `config.rules` entry can turn off. New configurable rules:
  `bare-target-reference`, `whole-target-reference`, `coarse-claim`,
  `glob-selector`, `redundant-selector`, `tag-mismatch`, `unclaimed-tag`,
  `suite-level-requirement`, `duplicate-case`, `level-mismatch`,
  `same-path-multiple-owners`, `parent-with-claims`,
  `multi-verifies-annotation`, `lock-stale`. Under `$XML_OUTPUT_FILE` (or
  `--junit`), `rr validate` writes one JUnit case per check family, so
  `rr_model`'s `<name>_test` is per-case evidence.
- **Attribution** ({py:mod}`rules_requirements.attribution`): the owner map,
  quarantine codes `multi-tag`, `attribution-conflict` and
  `same-code-multiple-owners` (the same source file and case path, or a
  `variants` group, owned twice), and `check_invariant()`. Retries
  (`test_attempts/`), repeated runs, shards and several evidence roots of one
  case merge into one result before attribution.
- **Verification sets** ({ref}`evidence`): an entity's verdict is computed
  over every case it owns or expects. Member states `missing`, `not-run`,
  `moved` and `quarantined` join the case results, and verdicts gain `basis`
  (`own` / `derived` / `own+derived`) and `derived_from`.
- **New statuses** <span class="rr-status">INVALID</span> (a quarantined case
  names the entity; rolls up like FAILED) and
  <span class="rr-status">INCOMPLETE</span> (a member missing, not run,
  skipped or moved; rolls up like PARTIAL), with the counts
  `requirements_invalid` and `requirements_incomplete`.
- **The verification-set lock** ({ref}`verification-lock`):
  `verification.rrlock` maps each case to one id and pins the sets; it adds
  expected members and never creates ownership.
  `rr sets lock [--write] [--allow-removals]`, `rr sets check`,
  `rr sets show ID`; {py:mod}`rules_requirements.lock`.
- **Report v2** ({doc}`guides/outputs`): schema
  `rules_requirements/report/v2` (`schema/report.v2.schema.json`); the inverse
  matrix `cases` (every case with one owner or `null`), `attribution` (mode,
  lock, lane, per-target counts, quarantines, issues, granularity), each
  entity's `set` and `members`; Markdown and HTML gain a quarantine banner,
  set columns and member tables, a "Case attribution" section and the
  INVALID/INCOMPLETE badges. New gaps: `invalid`, `multi-tag`,
  `attribution-conflict`, `same-code-multiple-owners`, `incomplete`,
  `missing-case`, `flaky`, `mixed-builds`, `unlocked-member`,
  `lock-owner-changed`, `lock-stale`, `unpinned-sets`, `unclaimed-tag`,
  `tag-mismatch`, `duplicate-case`, `coarse-claim`, `ambiguous-source`,
  `unattributed-failure`.
- **CLI.** `rr report --on-attribution-error {fail,warn}`, `--lane NAME`,
  `--lane-targets FILE`, `--sets-lock PATH`, `--no-lock`, and exit `3`;
  `rr attribution [--check] [--suggest] [--unowned]`; `rr sets`;
  `rr check-report REPORT.json`; `rr migrate apply --stage model
  [--compress]`; `rr validate --known-targets FILE --sets-lock PATH --junit
  PATH`; `rr graph --cases`; `rr serve --lane-targets NAME=FILE`
  ({doc}`reference/cli`).
- **Bazel** ({doc}`guides/bazel`): `rr_model(lock = ...)`,
  `rr_report(check, lane, lane_targets, on_attribution_error)` with
  `<name>_check_test` (`rr check-report`), and `rr_sets_lock_test` with
  `.update`.
- **Web editor and agents** ({doc}`guides/web-editor`): the save guard
  refuses with 409 any edit that would give a case two owners (or introduce
  a bad selector or target, or a lock owner change), a live precheck, the
  verification-set widget, the case ledger (`#/cases`) with checked
  **Move…**, **Update lock…**, and the *Assign test cases* agent workflow,
  which only proposes owners into the attribution worksheet.
- **Ingest**: `TestCase.declared`, `scope` and `synthetic`; `[rr:ID]` name
  tags; nested `<testcase>`s; `rr.file`, `file` and `line`; a records file's
  document-level `target:` and scalar `requirement`.
- **Docs**: {doc}`one-test-case-one-requirement`; the second half of
  {doc}`guides/migrating-to-per-case`; the standards page's
  {ref}`standards-one-owner`. The thermostat example runs in model mode with
  a lock, a records target and one id per test case.

### Changed

These are the intended semantic breaks. Each says what a consumer must do.

**Model**

- **Two entities naming one target is now `shared-case`**, a hard error
  (`rr validate` and `<model>_test` fail, `rr report` exits 2, the editor
  refuses the save). *Do:* replace both whole-target references with the
  cases each entity owns. `rr migrate plan` on 0.2 lists every such pair, and
  the pin-bump change must carry the fix.
- **0.2 claim forms still parse**: a bare label, `{target}` or
  `{target, level}` is a whole-target claim with a `bare-target-reference`
  warning. *Do:* nothing now; convert them to `cases` (or `whole: true` with
  a `reason`) before 0.4, where the warning becomes an error.
- **A 0.3 model fails loudly on 0.2.** The new keys (`cases`, `whole`,
  `reason`, `validated_by`, `verified_by` on mitigations, the six `config`
  keys, the new rule names) are unknown fields or keys to 0.2. *Do:* bump
  every consumer that reads the model (CI, the editor, sibling repositories)
  together.

**Evidence**

- **A case naming two ids from any producer is quarantined**: it counts for
  no requirement, every id it names reads INVALID, and `rr report` exits 3.
  Re-ingesting archived 0.1/0.2 evidence shows its multi-counts as
  quarantines; there is deliberately no "legacy many-to-many" switch. *Do:*
  give each test one id (split tests that verify two things;
  `rr migrate apply --stage tags` rewrites Python tags). Only for a report
  whose quarantines are intentional, `--on-attribution-error=warn` keeps the
  exit status; it never changes a verdict.
- **Old JUnit still parses**: repeated or comma-separated `requirement`
  properties and the googletest `requirements` property are read as declared
  ids. *Do:* nothing.
- **Retries are merged, not double-counted.** `test_attempts/attempt_N.xml`
  and the final `test.xml` are one result per case: the final attempt
  decides, and a pass after a failed attempt is flaky — UNDER-VERIFIED under
  the default `config.flaky: under-verify`. *Do:* pass the whole testlogs
  directory (not a `**/test.xml` glob) so retries are seen, and choose
  `config.flaky`.
- **Suite-level requirement properties are no longer inherited** by the
  suite's cases (`suite-level-requirement` warning). *Do:* declare the id on
  each case, or claim the cases in the model.
- **Bazel's synthetic results are recognised** (the generated `test.xml` of a
  target that writes no JUnit): its case path is `[target]`, and only a
  `whole` claim selects it. *Do:* claim such targets with `whole: true` and a
  `reason`.

**Python API**

- `TestCase.requirements` is a read/write alias of `declared`, with a
  `DeprecationWarning`. *Do:* read and write `declared`.
- `build_matrix(model, evidence, current_build, references)` keeps its
  signature and gains keyword-only `lock=None`; `Matrix.attribution` is new.
  *Do:* nothing.
- `Verdict` gains `members`, `basis` and `derived_from`; `Verdict.evidence`
  is kept as a view of the owned members. *Do:* read `members` for new code.
- `Evidence.for_id` is deprecated (it returns cases *declaring* an id, and
  no verdict uses it); `Evidence.target_status` is kept but unused. *Do:* use
  `matrix.attribution.members_of(entity_id)`.
- `JUnitWriter.cases` is a read-only tuple, and a recorded case's
  `requirements` a read-only alias: code that re-attributed a recorded case
  now raises `AttributeError`. *Do:* record a new case (or use `CheckPlan`).

**Statuses, gaps and reports**

- INCOMPLETE and INVALID are new statuses. *Do:* handle them wherever you
  switch on a status: CI step summaries, dashboards, site generators.
- `unattributed-failure` replaces `untraced-failure`, which is still emitted
  alongside it in 0.3.x. *Do:* move readers to the new kind.
- The JSON report's `schema` is `rules_requirements/report/v2`. `evidence[]`
  is kept for 0.3.x and every 0.2 summary key is still written (new ones are
  added); `kind: "target"` evidence
  entries are gone (a whole-target claim lists the target's cases). A case is
  named by its key `<target>#<path>` in `unknown_evidence` and the
  `unknown-id` gap, and `summary.test_cases` counts case keys. *Do:* check the
  schema string, and read `cases` and each entity's `members`.
- **Exit codes**: `0`, `1` and `2` are unchanged; `3` is new (a quarantine).
  `--fail-on failed` counts INVALID, and `--fail-on unverified` counts
  INCOMPLETE. *Do:* make CI distinguish `3` if it treated every non-zero
  status alike.

**Hooks**

- googletest records the property `requirement` (0.2: `requirements`);
  ingest reads both. Rust trace lines carry a single `requirement`; the list
  form is still read, and a list naming more than one id is quarantined.
  *Do:* nothing, unless you parse these files yourself.
- pytest writes only the nearest scope's id (0.2 accumulated the ids of every
  scope). *Do:* check that a function marker under a module `pytestmark`
  names the id you mean.
- pytest's RR-E102 guard fails a test that records a raw `requirement`
  property past the single-id API. *Do:* use `@pytest.mark.rr`.

**Modes and Bazel**

- `config.attribution` defaults to `hybrid`: an existing tag-based project
  keeps its attribution, except for multi-id tags and shared targets (above).
  *Do:* nothing to start; move to `model` with the migration guide.
- `rr_report` fails the build on a quarantine (`on_attribution_error =
  "fail"`, the default), and builds `<name>_check_test` whenever it builds a
  JSON report (`check = False` opts out). The macros' other arguments are
  unchanged. *Do:* fix the quarantine; use `"warn"` only for intentional
  fixtures.

### Removed

Nothing yet. 0.4.0 removes the `TestCase.requirements` alias, the report's
`evidence[]` view and the `untraced-failure` gap.

### Deprecated

- Multi-id authoring in every hook: pytest markers, `@rr.verifies`,
  `JUnitWriter` lists, `RR_VERIFIES` and `rr::verifies!` with several ids
  (warn now, quarantined in the report; a collection, import or compile
  error in 0.4).
- The 0.2 claim forms (`bare-target-reference`, an error by default in 0.4)
  and multi-id *verifies* source annotations (`multi-verifies-annotation`,
  likewise).
- `attribution: hybrid` (0.4 defaults to `model`, and hybrid warns
  `hybrid-mode`).
- `TestCase.requirements`, `Evidence.for_id`, the `untraced-failure` gap and
  the report's `evidence[]` view.

Still deferred: `rr migrate plan --agent` (agent proposals are made in the
web editor's *Assign test cases* workflow, into the worksheet).

### Upgrade guide

{doc}`guides/migrating-to-per-case`: on 0.2, plan, decide, apply the tags
and verify them; then pin 0.3 in hybrid mode, `rr migrate apply --stage
model`, and lock.

## 0.2.1 (2026-10-04)

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
  warning when its target has no result file at all in the new evidence,
  never when the target ran (a case renamed or lost by the rewrite). A
  target with any result file ran: a `test.xml` that holds no testcase
  (pytest collected nothing) or only Bazel's synthetic whole-run result
  counts. A decided
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
  `--no-collect-check` (and `--trust-main-guard` only if needed), push, then
  verify against the CI evidence before merging.

### New: `rr migrate apply --trust-main-guard`

- **Opt-in, off by default.** By default `rr migrate apply` judges the code
  only an `if __name__ == "__main__":` block runs like any other code, as
  0.2.0 does. `--trust-main-guard` leaves out of the static guards the code
  that never runs when pytest imports a test module — the body of an
  `if __name__ == "__main__":` block (either operand order, either quote; not
  its `else`) and the bodies of module-level functions reachable only from
  it. A script-style test module whose `main()` loads a helper with
  `importlib.util.spec_from_file_location` refuses every rewritten file in
  the tree without it, because an import call the codemod cannot name may
  import any of them. The exclusion is **best-effort**: static analysis
  cannot prove what Python runs at import (a name computed at run time, a
  `builtins` alias, `__getattribute__`, `runpy.run_module(...,
  run_name="__main__")` in another file all get past it), so the project's
  fail-closed rule keeps it opt-in. Only use it together with a definitive
  check: the collection check, or `rr migrate verify` against fresh test
  evidence before merging. apply prints a warning saying so whenever the
  flag is given. With the flag, such a function is still judged when anything that may run at import
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
