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
| `owner` | The decision: one of the ids the case counts toward, `none` (the case verifies none of them; any case, so `None` works too) or `'?'` (still open; quote it, since a bare `?` is not valid YAML). A group's owner applies to each case without an `owner` of its own. A decision chooses among the existing claims: an id the case does not count toward is rejected, as are numbers, booleans and empty values (`owner:` with nothing after it, or `''`). |
| `target_scope` | Whole-target results, such as an `exit-status` case, that carry several ids today. There is nothing to decide here: from v0.3.0 they carry no ids. |

An `--evidence` path that holds no evidence file is reported as a warning
(`[no-evidence]`), and no evidence at all stops the plan (exit status 2). A
target named in `verified_by` that the evidence has no result of — a HITL-only
or manual target planned from software evidence, say — is listed under
*Targets without evidence*: nothing is decided about it, and its references
are not unused.

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
are exactly the intended ones. Once every file is rewritten in memory, it
derives each decided case's ids again from the rewritten sources and compares
them with the worksheet; if any differs, nothing is written.

## The collection check: the guarantee

A rewrite's promise is that *each test keeps collecting exactly the ids it
should* — every decided case ends with its owner, and nothing else moves. No
reading of the syntax tree can prove that: Python collects tests pytest never
sees in the source — installed by a factory, a metaclass, `__init_subclass__`,
`setattr`, `exec`, or an import call. So, by default, `rr migrate apply`
proves the rewrite against pytest itself.

After the rewrite passes the static guards (below) in memory, apply:

1. copies the tree under `--root` to a temporary directory (skipping `.git`,
   `bazel-*` symlinks and caches) and writes the rewritten files **there**,
   never in place;
2. runs `python -m pytest --collect-only` in the original tree and in the copy,
   with a tiny plugin that records, for every collected item, its nodeid and
   the ids the JUnit hook would give it;
3. compares: each decided case must end with exactly its owner (no id for
   `none`), every other collected test must keep exactly the ids it had, and
   the set of collected nodeids must not change.

Any difference — including a decided case that no collected test matches —
makes apply **refuse, write nothing, and name each offending item** with its
before, after and expected ids. This is what catches the dynamic shapes the
static guards cannot: a `setattr`-installed method that would silently lose a
shared marker, a factory-built subclass, an aliased test.

Run apply where `python -m pytest --collect-only` works for the project — the
same interpreter and dependencies the tests need. For a plain project that is
its virtualenv; for a Bazel project, make a virtualenv with the test
dependencies (the same ones the `py_test` targets use) and run apply from the
source tree. If collection fails in either tree (an import error, a non-zero
exit, no tests found while the worksheet has decided cases), apply refuses and
prints the collection output, so a broken environment never passes silently.

- `--python PATH` picks the interpreter whose pytest and project dependencies
  collect the tests (default: the interpreter running `rr`). Point it at the
  project's venv when `rr` runs from another.
- `--pytest-args "..."` passes extra arguments to the check's pytest, for a
  project that needs them to collect: `-c pytest.ini`, `--rootdir .`, or
  `-p my_project.plugin` for a plugin the tests rely on.
- `--no-collect-check` skips the check and writes on the static guards alone.
  It prints a loud warning: the guards are **best-effort** and cannot see what
  pytest collects dynamically, so a rewrite they accept can still move a
  shared marker off a test you did not mean to touch. Use it only where pytest
  cannot collect the project at all, and re-run the tests and `rr migrate plan`
  afterwards to check the result.

## The static guards: a first, conservative line

The guards below run first, entirely in memory. They are deliberately
conservative — they refuse anything they cannot read or follow at **import or
class-creation time** (module and class bodies, decorators, metaclasses,
`__init_subclass__`, `__new__`, and anything called at module or class scope),
because that is what decides collection. They no longer refuse code in the
bodies of tests, fixtures (`@pytest.fixture`), `setUp` / `tearDown` /
`setUpClass` / `setup_method`-style hooks: that code runs *after* collection
and cannot change it, and the collection check vouches for the result either
way. A file is **refused** and left unchanged, never half-migrated, when:

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
- a decided test, or a class or module around it, carries a decorator or a
  `pytestmark` element that may hold ids the codemod cannot see: a variable
  holding a marker (`AB = pytest.mark.rr("A", "B")`, then `@AB`), an alias
  such as `m = pytest.mark` with `@m.rr(...)`, a helper's decorator
  (`@tagged("A")`, or any decorator from a third-party library), a
  `pytest.param(..., marks=AB)`, or a `pytestmark` that is augmented,
  annotated, assigned twice or bound inside a block. What it reads: the
  `rr` / `requirements` / `verifies` declarations, any other
  `pytest.mark.NAME` marker, the rest of `pytest` (`pytest.fixture`),
  `staticmethod` / `classmethod` / `property`, and what `unittest` and `mock`
  provide (`mock.patch`).
- one scope declares two different levels.
- a change would reach a test the codemod cannot see: a test class that
  inherits tests or declarations from another class, in the file or in
  another module; a test defined inside an `if`, `try`, `with` or loop block;
  a decided case of the module that the file does not define (inherited from
  another module, or generated); or a test name bound other than by a `def`
  or `class` statement, whether or not it is on the worksheet. That covers
  every way of binding a name in a module or class body: assignments
  (tuple, starred, annotated and augmented ones too, `test_b = test_a`), `for`
  and `with ... as` targets, `:=` anywhere in the scope, `from m import ...`
  (also `as`, and star imports, which may bring tests), `del`, `global`,
  `except ... as`, `match` captures, `Class.test_x = ...`,
  `globals()["test_x"] = ...` and `setattr(..., "test_x", ...)`, and `exec`
  / `eval` / `globals()` / `setattr` with a computed name at the top of a
  module or class. Bindings from inside a function body, which may run at
  import time, count too, and then nothing in the file is changed: `global
  test_x`; an attribute store, `setattr` / `delattr` or `__setattr__` naming
  a test or with a computed name, on anything but a method's own `self`
  (`TestK.test_x = ...`, `sys.modules[__name__].test_x = ...`); any use of
  `globals()`, `vars(x)` or `x.__dict__` (except `self.__dict__`); `exec` and
  `eval`. A class deriving from another may be a `unittest.TestCase`, which
  pytest collects whatever its name, so `Alias = Checks` counts as a test
  name bound by an assignment too. A name is a test name when pytest's
  `python_functions` or `python_classes` match it (`test` and `Test` by
  default; the codemod also reads the patterns configured in `pytest.ini`,
  `pyproject.toml`, `tox.ini` or `setup.cfg` under `--root`). A literal
  (`test_cases = [...]`) and a plain `import m as test_m` (a module) are not
  tests. When what such a name holds cannot be traced to a `def` of the file
  (`x = test_a`, then `test_b = x`), nothing in the file is changed;
  otherwise nothing that reaches it is. Migrate the file by hand, or rename
  the name if it does not hold a test.
- another file imports or subclasses a class or test whose declarations would
  change. pytest collects an imported test class or function again in the
  importing module, and a subclass inherits its base's tests and markers, so
  the change would reach tests in that file unseen. Both files are refused.
  The codemod indexes every Python file under `--root` for this, also those
  outside `--only`: `from m import C` (with or without `as`), `import m` with
  `m.C` (also `pkg.sub.C` with only `pkg` imported), relative imports and
  star imports, `importlib.import_module("m")` and `__import__("m")` with a
  literal module name (also relative ones), and a base class it cannot trace
  to an import by its name. A package passed around as an object
  (`getattr(pkg, ...)`, `__import__("pkg")`) reaches every module below it. Files are read in
  their own encoding (a PEP 263 coding cookie or a BOM, as Python reads
  them) and written back in it.
- a file under `--root` cannot be read or parsed, while some declaration
  would change: it may import or subclass anything. That file is refused by
  name, together with every file that would change. A file holding a decided
  case that cannot be read or parsed (an unknown coding cookie, or syntax
  newer than the Python running `rr`) is refused by name too.
- a test module, or a file a test module imports, imports a module by a call
  whose module the codemod cannot name: `importlib.import_module(name)` with
  a computed name, `importlib.util.spec_from_file_location`,
  `runpy.run_path`, `exec` of a computed string or of a literal naming an
  import, and the like. pytest collects what it imports there, which may be
  any test or class, so when any declaration in the tree would change, that
  file and every file that would change are refused.
- a class has a base the codemod cannot resolve statically: a name bound by
  an assignment or a `def` (`B = importlib.import_module("m").C`,
  `B = __import__(...)`, `try: from m import B` / `except: B = object`) or an
  expression (`getattr(m, "C")`). It may be any class, so when the
  declarations of any class in the tree would change, its file and every
  file whose classes would change are refused, and its own decided tests are
  refused. This counts for classes pytest may collect (named like a test
  class, or in a test module: `python_files`, `test_*.py` and `*_test.py` by
  default), classes another class subclasses, and classes a test module
  imports.
- a multi-line declaration it would rewrite has comments inside it (they
  would be lost).

The command also lists, without changing anything:

- decided cases whose module it found but whose test is not defined there.
  These make it exit 1: it cannot vouch for them.
- decided cases with no Python test (googletest `RR_VERIFIES`, Rust
  `rr::verifies!`, `JUnitWriter` harnesses, records, and whole-target `[target]`
  results). Edit those by hand so that each case names one id. With `--only`,
  cases outside the given paths are only counted.
- decided cases whose test declares no id in its source, every decorator and
  `pytestmark` element around it being one the codemod reads. Their owner is
  set by `verified_by` in the model.
- the `verified_by` edits the decisions imply: `remove` (no case of the target
  is left for that requirement) or `split` (the requirement keeps some of the
  target's cases while others went elsewhere). A whole-target reference cannot
  express a split; that waits for case selectors in v0.3.0.

Markers added by a `conftest.py` (`item.add_marker`) are invisible to the
codemod; check them by hand.

It is all or nothing: when a file is refused or a decided case's test was
not found in its module, nothing is written, and the files it would have
rewritten are listed as held back, each with the cause. Fix those, or pass
`--partial` to write each rewritten file that holds no such case and is not
linked to a refused file or to one holding such a case. Two files are linked
when one star-imports the other or passes it around as a module object, or
imports, references (`m.name`) or subclasses a class of it, a test-named
name, or a name not defined at its top level. Importing a plain helper
function or constant does not link files: changing a file never changes what
such a name holds. A file that cannot be read, a class whose base cannot
be resolved, and an import call whose module cannot be named are linked to
the files refused with them.

It exits 0 when every file it had to change was rewritten, 1 when a file was
refused or a decided case's test was not found in its module (with or without
`--partial`), and 2 when the
worksheet is unreadable or an owner is not one of the ids its case counts
toward (or, with a model, not a requirement, user need or mitigation of it).
Without `--model` it checks the owners against the model named in the
worksheet's `inputs`, when that is still there. Files keep their line endings
and their encoding.

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
