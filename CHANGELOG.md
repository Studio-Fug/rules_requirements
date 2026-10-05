# Changelog

A short summary of each version. The detailed record — every option, code and
behaviour — is the [release notes](docs/release-notes.md) (published at
<https://studio-fug.github.io/rules_requirements/release-notes.html>); where
the two differ, the release notes are right and this file is the bug.

## 0.3.0 (2026-10-05)

One owner per test case: a test case verifies at most one requirement, a set
of test cases may together verify one, and the tool makes it impossible for
one case to verify two. Semantic release: verdicts change on purpose.
[Details](docs/release-notes.md#030-2026-10-05) ·
[upgrade guide](docs/guides/migrating-to-per-case.md).

### Added

- Claims in the model: `verified_by` / `validated_by` items
  `{target, cases: [selector]}` or `{target, whole: true, reason}`, on
  requirements, mitigations and user needs alike; `config.attribution`
  (`hybrid` | `model`), `main_repo`, `sets_lock`, `flaky`,
  `set_consistency`, `variants`.
- `rr validate`: `shared-case` with an example case, `same-code-multiple-owners`,
  `bad-selector`, `bad-target`, lock checks, `--known-targets`; hard errors
  no configuration turns off.
- `multi-parent-refines` (error by default, configurable): refines must form a
  tree, so each test case's evidence rolls up one chain of requirements;
  raised by `rr validate`, `<model>_test`, `rr report` (exit 2) and the
  editor's save guard (409).
- `multi-parent-implements` (error by default, configurable): a requirement
  that implements a mitigation has no other parent (a second mitigation, or a
  requirement it refines); raised where `multi-parent-refines` is.
- Attribution (`attribution.attribute()`, the one place a case gets an
  owner), verification sets, and the INVALID and INCOMPLETE statuses.
- The verification-set lock (`verification.rrlock`; `rr sets lock|check|show`).
  A `suite:`/`record:` entry whose case moved to another target is
  `lock-stale`; `--allow-removals` drops it.
- Report v2 (`cases`, `attribution`, sets and members); `rr report` exit 3,
  `--on-attribution-error`, lanes; `rr attribution`; `rr check-report`;
  `rr migrate apply --stage model`; `rr graph --cases`.
- Bazel: `rr_model(lock)`, `rr_report(check, lane, lane_targets,
  on_attribution_error)`, `rr_sets_lock_test`.
- Web editor: the 409 save guard, live precheck, case ledger, lock update;
  the *Assign test cases* agent workflow (proposals only).
- Docs: "One test case, one requirement", the 0.2 → 0.3 migration path, the
  standards rationale. The thermostat example runs in model mode with a lock.

### Changed

- A case naming two ids is quarantined: it counts for nobody, every id it
  names reads INVALID, `rr report` exits 3 and `rr_report` fails the build.
  Give each test one id.
- Two entities naming one target is a `shared-case` model error. Claim the
  cases each owns.
- A requirement refining two or more parents is a `multi-parent-refines`
  error. Keep one parent and split the child per parent.
- A requirement implementing a mitigation and having another parent is a
  `multi-parent-implements` error. Merge the mitigations, or let the parent
  requirement implement it.
- Retries are merged per case (a pass after a failure is flaky:
  UNDER-VERIFIED by default); suite-level ids are no longer inherited;
  Bazel's synthetic results are `[target]`, selected only by `whole: true`.
- JSON report schema `rules_requirements/report/v2`; new statuses and gaps;
  pytest records only the nearest scope's id; `JUnitWriter.cases` is
  read-only; `rr_report` adds `<name>_check_test` with a JSON report.
- `attribution: hybrid` is the default, so single-id tags keep working.
- Verdicts that move at the bump: whitespace separates ids, `[rr:ID]` name
  tags declare ids, a whole-target claim of a target without results reads
  INCOMPLETE, one stale member under-verifies the set, a skipped-only set
  reads INCOMPLETE, and `--fail-on gaps` fails on `unpinned-sets` without a
  lock and on every attribution issue, warnings included: `duplicate-case`,
  `unscoped-evidence`, `suite-level-requirement`, `coarse-claim`,
  `tag-mismatch`, `unclaimed-tag`, `misdirected-evidence`, `unknown-id`,
  `same-path-multiple-owners`, `ambiguous-source`, `level-mismatch`,
  `unlocked-member`, `lock-stale`, `lock-owner-changed`, `lock-invalid`,
  `multi-verifies-annotation` (lock, resolve, or configure the rule: see the
  release notes). `--strict` (and `strict = True` in Bazel) also fails on
  the new 0.3 warnings, such as `bare-target-reference`.
- One case path owned twice where one target is a `suite:`/`record:`
  pseudo-target and a source file is unknown is quarantined.

### Removed

- No command, rule or report key; some Python constructor positions changed
  (`JUnitWriter`, `VerifiedBy`, `CaseRow`: pass fields by keyword).

### Deprecated

- Multi-id authoring in every hook (an error in 0.4), the 0.2 claim forms
  (`bare-target-reference`), `attribution: hybrid`, `TestCase.requirements`,
  `Evidence.for_id`, `untraced-failure`, the report's `evidence[]`.

## 0.2.1 (2026-10-04)

Unblocks applying a worksheet in Bazel projects.
[Details](docs/release-notes.md#021-2026-10-04).

### Added

- `rr migrate verify`: checks the test evidence of a run after
  `rr migrate apply` against the worksheet — every decided case declares
  exactly its owner; with `--baseline`, every other case keeps its ids and
  none disappears (`--allow-missing` for targets CI does not run: a target
  with any result file, an empty `test.xml` included, ran). The
  verification path where the collection check cannot run (Bazel `py_test`s):
  apply with `--no-collect-check` (and `--trust-main-guard` only if needed),
  push, verify against the CI evidence before merging.
- `rr migrate apply --trust-main-guard` (opt-in): the static guards skip the
  `if __name__ == "__main__":` block and the functions only it reaches,
  which never run when pytest imports the module, so an import call in a
  script-style test's `main()` no longer refuses every file. Best-effort
  (static analysis cannot prove what runs at import): use it only with the
  collection check or `rr migrate verify` against fresh evidence. Without
  the flag, apply judges that code like any other, as 0.2.0 does. Even with
  it, nothing is skipped when the file may rebind `__name__`, looks names up
  dynamically at import, or launches its tests in-process from the block.

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
