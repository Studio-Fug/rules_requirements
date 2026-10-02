# Migrating to one requirement per test case

```{admonition} Draft
:class: note

This guide describes the migration tooling that ships in v0.2.0: `rr cases`,
`rr migrate plan` and `rr migrate apply --stage tags`. None of them changes a
verdict. The guide will be completed as later releases add case selectors in
the model (v0.3.0) and make multi-id tags an error (v0.4.0).
```

A test case should verify **at most one** requirement. A set of test cases may
verify one requirement together, but a single test that counts toward two
requirements makes both verdicts depend on one result, and a reviewer cannot
tell which requirement the test was written for.

Today a case counts toward *every* id it is tagged with, plus every requirement
whose `verified_by` names its target. That is the union rule. Three common
patterns make one test count twice:

- a module-level `pytestmark = pytest.mark.requirements("PR-13", "PR-29")`, or
  any marker or `@rr.verifies(...)` that names two ids;
- ids that accumulate across scopes, for example a module marker naming PR-11
  and a function marker naming PR-13;
- one target listed in the `verified_by` of two requirements, or one target
  whose cases are tagged for one requirement and listed whole by another.

From v0.3.0 such evidence is quarantined (it counts for nobody) and a target
claimed by two requirements is a model error. Migrating on v0.2.0 first keeps
CI green throughout: every step below is valid under today's rules.

## The steps

1. **Collect evidence.** Run the tests that feed your traceability report
   (`bazel test //...`, plus HITL runs if you have them) and keep
   `bazel-testlogs`, including `test_attempts/`.
2. **Plan.** `rr migrate plan` writes a worksheet listing every evidence unit
   that counts toward two or more entities.
3. **Decide.** The owners of the requirements involved record one owner per
   case on the worksheet.
4. **Rewrite the tags.** `rr migrate apply --stage tags` rewrites Python test
   markers to the decided owners.
5. **Edit the model.** Remove the `verified_by` references the decisions leave
   unused; the tool lists them.
6. **Check.** Re-run the tests and the plan. What is left is the work for
   v0.3.0's case selectors.

## Case keys

Every decision is made about a *case key*: `<target>#<path>`, for example
`//pi/server:server_test#pi.server.tests.test_handler::test_configure`. A
target that produced only Bazel's generated report has one case, `[target]`.
See {ref}`case-keys` for the exact rules. To list the keys in your evidence:

```console
$ rr cases --evidence bazel-testlogs
$ rr cases --evidence bazel-testlogs --target //web:clocksync_test --json
```

## Planning: `rr migrate plan`

```console
$ rr migrate plan --model requirements/ --evidence bazel-testlogs hitl-testlogs \
    --out requirements/attribution.rrplan --md attribution.md
178 of 271 attributed evidence unit(s) count toward two or more entities; 7 target(s) shared between requirements; 0 decided, 178 open
```

`--out` writes the worksheet as YAML (a `.rrplan` file). `--json` writes the
same document as JSON, and `--md` writes a Markdown rendering for review. With
no output flag the YAML goes to stdout. Use an extension other than
`.yaml`/`.yml` (such as `.rrplan`): when `--model` names a directory, every
YAML file in it is read as part of the model, and the worksheet must stay out.

The worksheet groups cases by target and test module or class:

```yaml
schema: rules_requirements/attribution-worksheet/v1
summary: {units: 489, attributed: 271, contested_units: 178, shared_targets: 7, decided: 0, open: 178}
targets:
- target: //web:clocksync_test
  claimed_by: [PR-13, PR-29]      # verified_by of both
  shared: true
  cases: 1
  per_case_output: false          # only Bazel's generated report: one [target] case
groups:
- target: //pi/server:server_test
  group: pi.server.tests.test_handler
  counts_toward: [PR-11, PR-13]
  tags: [PR-11, PR-13]            # where the ids come from: tags ...
  owner: '?'                      # <- the decision for every case below
  cases:
  - path: pi.server.tests.test_handler::test_configure_renegotiates_mid_capture
    status: passed
  - path: pi.server.tests.test_handler::test_resumes_session
    status: passed
    owner: PR-22                  # <- a per-case override
- target: //web:clocksync_test
  group: ''
  counts_toward: [PR-13, PR-29]
  target_claims: [PR-13, PR-29]   # ... or verified_by
  owner: '?'
  cases:
  - path: '[target]'
    status: passed
```

| Field | Meaning |
| ----- | ------- |
| `targets` | Every target named in `verified_by` that is shared between requirements, or whose cases are contested. `uncontested` counts the cases that need a requirement's claim because they count toward it alone. |
| `groups[].counts_toward` | The entities the cases count toward today. Only requirements, user needs and mitigations count; tags naming risks, test methods or unknown ids are listed per case as `ignored_tags`. |
| `groups[].tags`, `target_claims` | Whether those ids come from the tests' tags, from `verified_by`, or both. |
| `proposed`, `reason` | A hint where the model or the evidence determines an owner. A case tagged with a requirement and its refinement proposes the refinement, because the parent's verdict rolls up from it. A case with its own single tag inside a target claimed whole by another requirement proposes the tag. Everything else is left open. A proposal is never a decision. |
| `owner` | The decision: one entity id, `none` (the case verifies none of them) or `?` (still open). A group's owner applies to each case without an `owner` of its own. |
| `target_scope` | Whole-target results, such as an `exit-status` case, that carry several ids today. There is nothing to decide here: from v0.3.0 they carry no ids. |

The JSON Schema is `schema/worksheet.schema.json`
(<https://studio-fug.github.io/rules_requirements/schema/worksheet.schema.json>).

Evidence changes while decisions are made. Re-run the plan with
`--merge requirements/attribution.rrplan` to keep every decision whose case
is still contested.

## Deciding

Decide per group first, then override per case. Some rules of thumb:

- **The owner is the requirement whose acceptance criterion the test
  asserts**, not every requirement the code under test helps with. A protocol
  round-trip test verifies the protocol requirement, not the feature that uses
  the protocol.
- **`none` is a valid answer.** Helper and plumbing tests often verify no
  requirement. They still run and still gate CI; they just do not count as
  evidence.
- **A requirement with no test of its own is a gap, not a reason to share.**
  If deciding leaves a requirement without evidence, the honest result is
  UNVERIFIED, and the remedy is a test written for it.
- **Parametrized cases** can go to different owners. The codemod cannot split
  one test function between owners; split those by hand (see below).
- **Shared whole targets** (`per_case_output: false`) can only be given to one
  owner as they are. To split them, give the target per-case output first
  (`rr_node_test` for node:test, `rr_case.h` for plain-assert C/C++,
  `JUnitWriter`/`CheckPlan` for scripts), re-run the plan, and decide per case.
- **Hardware phases** that check several things in one case should become one
  case per check (`CheckPlan`), each with one owner.

The PR owner of each requirement decides. Agents and heuristics only propose.

## Rewriting tags: `rr migrate apply --stage tags`

```console
$ rr migrate apply requirements/attribution.rrplan --stage tags --model requirements/ --dry-run
$ rr migrate apply requirements/attribution.rrplan --stage tags --model requirements/ --only pi/server
```

The codemod finds each decided case's test in the Python sources under
`--root` (default: the workspace). It matches the case path against module
paths and class and function names, and ignores parametrization ids and
subtest suffixes. It then rewrites `@pytest.mark.rr`,
`@pytest.mark.requirements` (on functions and classes, or in a module or class
`pytestmark`) and `@rr.verifies` so that each test declares exactly its
decided owner:

- a module or class declaration whose tests all went to one owner is narrowed
  to that id;
- otherwise it is removed, together with the comment block directly above a
  removed `pytestmark`, and each test under it gets its own single-id
  declaration in the same spelling;
- `level=` and `artifact=` are carried to wherever the id now lives, so no
  test changes level;
- a test decided `none` loses its tags, and `import pytest` or
  `from rules_requirements import rr` goes too when the rewrite removed its
  last use.

```python
# before
pytestmark = pytest.mark.requirements("PR-11", "PR-22")


def test_start_persist_and_clear(tmp_path): ...


def test_reconfigure_without_session_raises(tmp_path): ...


# after (decided: PR-11, and PR-22 for the reconfigure cases)
@pytest.mark.requirements("PR-11")
def test_start_persist_and_clear(tmp_path): ...


@pytest.mark.requirements("PR-22")
def test_reconfigure_without_session_raises(tmp_path): ...
```

Only the declarations change. Comments, formatting and other markers are left
alone, and new lines are laid out the way black lays them out, so running
black (or `ruff format`) afterwards changes nothing. Pass your formatter's
line length with `--line-length` (default 88). Before writing a file, the
codemod parses it again and checks that every test's ids, level and artifact
are exactly the intended ones.

A file is **refused** and left unchanged, never half-migrated, when:

- a test in it names several ids and has no decision (`?`, or a test the
  evidence never ran). With `--unassigned drop` such tests lose their tags
  instead.
- the parametrizations of one test were decided differently. Split it by hand:

  ```python
  @pytest.mark.parametrize(
      "mode",
      [
          pytest.param("fast", marks=pytest.mark.requirements("PR-1")),
          pytest.param("slow", marks=pytest.mark.requirements("PR-3")),
      ],
  )
  ```

- a declaration is not something it can read statically: ids, levels or
  artifacts that are not literals, `**kwargs`, a marker inside
  `pytest.param(...)`, or a `pytestmark` that shares its line with other code.
- one scope declares two different levels.

The command also lists, without changing anything:

- decided cases with no Python test (googletest `RR_VERIFIES`, Rust
  `rr::verifies!`, `JUnitWriter` harnesses, records, and whole-target `[target]`
  results). Edit those by hand so that each case names one id.
- decided cases whose test carries no tag. Their owner is set in the model.
- the `verified_by` edits the decisions imply: `remove` (no case of the target
  is left for that requirement) or `split` (the requirement keeps some of the
  target's cases while others went elsewhere). A whole-target reference cannot
  express a split; that waits for case selectors in v0.3.0.

Markers added by a `conftest.py` (`item.add_marker`) are invisible to the
codemod; check them by hand.

It exits 0 when every file it had to change was rewritten, 1 when a file was
refused, and 2 when the worksheet is unreadable or names an owner that is not a
requirement, user need or mitigation of the `--model` given.

`--only PATH` (repeatable) limits the rewrite to files below a path, so several
pull requests can migrate disjoint directories in parallel from one worksheet.

## Checking the result

Re-run the tests and the plan:

```console
$ bazel test //...
$ rr migrate plan --model requirements/ --evidence bazel-testlogs --merge requirements/attribution.rrplan --md attribution.md
```

Everything still listed is either decided but not yet applied (a non-Python
test, a split you have to make by hand) or a `split` target waiting for case
selectors. `rr report` verdicts change honestly as tags move. A requirement
that was VERIFIED only through a shared test now shows what it really has.

## What comes next

In v0.3.0 a requirement's `verified_by` can name individual cases of a target
(`{target: //web:clocksync_test, cases: ["clocksync::*"]}`). The model then
rejects any case claimed by two entities, and evidence that still names two
ids for one case counts for nobody. `rr migrate apply --stage model` will write
those selectors from the decided ownership. In v0.4.0 a multi-id tag becomes a
collection, import or compile error.
