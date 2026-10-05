# Migrating to one requirement per test case

```{admonition} The path from 0.2 to 0.3
:class: note

This guide takes a project from 0.2's union rule to 0.3's model mode in
steps that each keep CI green (a CI that runs `--strict`, `rr_model(strict =
True)` or `rr_report(strict = True)`, or gates on `--fail-on gaps`, needs the
extra steps the pin bump below lists): the first half runs on 0.2 (`rr cases`,
`rr migrate plan`, `rr migrate apply --stage tags`, and since v0.2.1
`rr migrate verify`, none of which changes a verdict); the second pins 0.3
in hybrid mode, writes the claims with `rr migrate apply --stage model` and
locks the sets. {doc}`../one-test-case-one-requirement` explains the rule
itself.
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
  and a function marker naming PR-13 (from v0.3.0 the hooks record only the
  nearest scope's id, here PR-13);
- one target listed in the `verified_by` of two requirements, or one target
  whose cases are tagged for one requirement and listed whole by another.

From v0.3.0 such evidence is quarantined (it counts for nobody) and a target
claimed by two requirements is a model error. Migrating on v0.2 first keeps
CI green throughout: every step of the first half is valid under 0.2's
rules, so the 0.3 pin bump only has to fix what 0.2 cannot express.

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
6. **Check.** Re-run the tests, check their evidence against the worksheet
   with `rr migrate verify`, and re-run the plan. What is left is the work
   for v0.3.0's case selectors.

On 0.3:

7. **Pin 0.3 in hybrid mode.** Bump the pin. Turn every target two entities
   still share into per-case selectors, so the model is valid; keep
   `attribution: hybrid` (the default), so the single-id tags keep owning
   their cases. Confirm with `rr attribution --check` that nothing is
   quarantined before merging.
8. **Move to model mode.** `rr migrate apply --stage model` writes an explicit
   selector for every case each entity owns today and proves the owner table
   unchanged; then set `config.attribution: model`.
9. **Lock.** `rr sets lock --write` pins every set's members; gate CI on
   `rr sets check` and `rr check-report`.

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

After the rewrite passes the static guards (below) in memory, and whenever it
would write anything (with `--partial` and `--dry-run` too), apply:

1. copies the tree under `--root` to a temporary directory and writes the
   rewritten files **there**, never in place. It skips version control and
   cache directories, virtualenvs (a directory holding `pyvenv.cfg`) and Bazel's
   `bazel-*` convenience symlinks, and passes an `--ignore` for each skipped
   path to *both* collections, so the two trees are collected over the same
   files. **Other symlinks are never skipped silently**, because a real pytest
   run from `--root` would follow them: a symlink whose target resolves *inside*
   the tree is recreated in the copy (so both collections follow it and the
   check covers whatever it reaches, refusing if its attribution changes); a
   symlink to any other file *outside* the tree (a shared `pytest.ini`,
   `pyproject.toml`, `setup.cfg` or `tox.ini`, a data file) is copied with its
   content, so both collections run under the same config; and a symlink that
   reaches a test *outside* the tree — a directory, or a `.py` file — makes
   apply **refuse and name the path** (the copy cannot cover it) whenever the
   original collection shows pytest would reach it. A link pytest never
   recurses into is left alone: one outside the paths it collects (its
   arguments, else `testpaths`) or under a directory `norecursedirs` excludes
   (`.*` by default, so `.direnv`'s flake-input links). Remove or redirect a
   link that refuses, or pass `--no-collect-check`;
2. runs `python -m pytest --collect-only` in the original tree and in the copy,
   from `--root` with no path argument (so the project's ini, `testpaths`
   included, decides what is collected, exactly as a plain `pytest` run there
   would), with a tiny plugin that records, for every collected item, its
   nodeid and the ids the JUnit hook would give it. Neither run leaves
   `__pycache__` in your tree;
3. compares: each decided case **the written files settle** must end with
   exactly its owner (no id for `none`), every other collected test must keep
   exactly the ids it had, and the set of collected nodeids must not change.
   Decided cases the write does not settle — cases left to `verified_by`
   (their test declares no id), cases in files held back or refused, cases
   outside `--only` — are held to their before-ids like undecided ones.

Any difference — including a settled decided case that no collected test
matches — makes apply **refuse, write nothing, and name each offending item**
with its before, after and expected ids (`--dry-run` exits 1 and reports the
files as held back). This is what catches the dynamic shapes the static guards
cannot: a `setattr`-installed method that would silently lose a shared marker,
a factory-built subclass, an aliased test. If either collection records the
same nodeid twice (a conftest that builds items by hand), apply refuses and
names it: the check cannot tell the two cases apart.

Apply **never writes through a symlink**: just before writing, it re-checks
each file it would rewrite, and if one is (or lies under) a symlink — even one
swapped in after the check ran — it refuses, names the path and where it
resolves, and writes nothing. This holds with `--no-collect-check` too.

Run apply where `python -m pytest --collect-only` works for the project — the
same interpreter and dependencies the tests need. For a plain project that is
its virtualenv; for a Bazel project, make a virtualenv with the test
dependencies (the same ones the `py_test` targets use) and run apply from the
source tree. Where that is impractical — `py_test` targets that import
through their runfiles, generated code, toolchain-provided modules — apply
with `--no-collect-check` (and `--trust-main-guard` only if needed), push,
then check the rewrite against the CI test evidence with `rr migrate verify`
before merging (see {ref}`migrate-verify`). If collection fails in either tree (an import error, a non-zero
exit, no tests found while the worksheet has decided cases), apply refuses and
prints the collection output, so a broken environment never passes silently.

**The guarantee covers what collects in the check's environment** — the
`--python` interpreter, its installed packages and the current environment
variables. A test that only exists elsewhere (a class built only when `HW=1` is
set, a module that imports a hardware library only the bench has) is not
collected by the check, so it cannot be verified. Collectors pytest skips at
collection time (`pytest.importorskip`, a module-level `pytest.skip`) are listed
in a warning for that reason. Run apply in the environment your tests really
run in (set the same variables, install the same packages), or check those
tests by hand.

- `--python PATH` picks the interpreter whose pytest and project dependencies
  collect the tests (default: the interpreter running `rr`). Point it at the
  project's venv when `rr` runs from another; only `rules_requirements` itself
  is added to its `PYTHONPATH`, never the rest of `rr`'s own environment.
- `--pytest-args "..."` passes extra arguments to the check's pytest, one
  shell-quoted string, for a project that needs them to collect: `-c
  pytest.ini`, `--rootdir .`, `-p my_project.plugin` for a plugin the tests
  rely on, `--ignore=scripts` for a directory that does not import here, or a
  path to collect beyond `testpaths`. A value that starts with a dash works
  either way: `--pytest-args "--ignore=x"` or `--pytest-args=--ignore=x`. **Any
  plugin your runner loads with `-p` must be repeated here** (for example
  `--pytest-args "-p my_project.plugin"`): a plugin that mutates markers at
  collection time changes what a real run attributes, and the check is blind to
  it unless it loads the same plugin. (Only `rr migrate apply` takes
  `--pytest-args`; it is passed through to the check's pytest unchanged.) The
  check's plugin registers rr's `rr` markers itself, so `--strict-markers`
  collects even when rr's pytest plugin is loaded with `-p` only in the real
  run (as the Bazel runner does).
- `--no-collect-check` skips the check and writes on the static guards alone.
  It prints a loud warning: the guards are **best-effort** and cannot see what
  pytest collects dynamically, so a rewrite they accept can still move a
  shared marker off a test you did not mean to touch. It also gives up
  everything the check adds: the before/after collection comparison, the
  refusal on symlinks that reach tests outside the tree, and the refusal on
  duplicate nodeids. Use it only where pytest cannot collect the project at
  all, and check the result against real test evidence afterwards with
  `rr migrate verify` ({ref}`migrate-verify`).
- `--trust-main-guard` (off by default) leaves the code only an
  `if __name__ == "__main__":` block runs out of the static guards (below).
  That exclusion is best-effort, so only use it together with a definitive
  check: the collection check, or `rr migrate verify` against fresh test
  evidence before merging. apply prints a warning saying so.

## The static guards: a first, conservative line

The guards below run first, entirely in memory. They are deliberately
conservative — they refuse anything they cannot read or follow at **import or
class-creation time** (module and class bodies, decorators, metaclasses,
`__init_subclass__`, `__new__`, and anything called at module or class scope),
because that is what decides collection. They no longer refuse code in the
bodies of tests, fixtures (`@pytest.fixture`), `setUp` / `tearDown` /
`setUpClass` / `setup_method`-style hooks: that code runs *after* collection
and cannot change it, and the collection check vouches for the result either
way. A function only counts as such a hook while nothing that may run at
import time refers to it: a `setUp` or `setup_module` the module calls itself
(directly, through a helper, `getattr`, or as a decorator) is judged like any
other import-time code.

By default they also judge the code only an `if __name__ == "__main__":`
block runs, like any other code: static analysis cannot prove what Python
runs at import, and the guards fail closed. A script-style test whose
`main()` loads a helper with `importlib.util.spec_from_file_location` is
therefore refused, and with it every file that import call may import.
**`--trust-main-guard`** (opt-in) leaves out the body of an
`if __name__ == "__main__":` block (either operand order, either quote; not
its `else`) and the bodies of module-level functions reachable *only* from
it: when pytest itself imports a test module, it imports it under its module
name, so that code does not run at collection. The exclusion is
**best-effort**. It follows the names it can see, and a name computed at run
time can get past it (a `builtins` alias, `__getattribute__`,
`runpy.run_module(..., run_name="__main__")` or a `"__main__"` module spec in
another file), so only use it together with a definitive check: the
collection check, or `rr migrate verify` against fresh test evidence before
merging. With the flag, such a function is still judged as usual when anything that may run
at import time names it (a module-level call, an alias, a `getattr` /
`globals()` string, a test, a default argument), when it is decorated,
rebound or named like a test or a hook, or when another file under `--root`
imports it, passes its module around, reads it from `sys.modules`, or names
it on a pytest item's `.module` / `.obj`. Nothing in the file is excluded —
the block included — when the file may rebind `__name__`, when code that may
run at import looks names up dynamically (`globals()`, `vars()`, `eval`,
`getattr` with a computed name, `inspect.getmembers`, `__dict__`,
`sys.modules`, a frame's globals), or when the block launches the tests
in-process (`pytest.main()`, `unittest.main()`): a Bazel `py_test` whose
main is the test file runs the block, and whatever it runs before the
launch runs before collection, in the same interpreter. Nor is anything
excluded anywhere when another file may reach any module's functions
without importing it (a computed `sys.modules` read, a frame, `eval` /
`exec`, a `pytest_pycollect_makeitem` hook, or a computed lookup on a pytest
item's module). A file is **refused** and left unchanged, never
half-migrated, when:

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
  `pytestmark` element around it being one the codemod reads. They count
  only through `verified_by`: see the edits below.
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

(migrate-verify)=
## Verifying with test evidence: `rr migrate verify`

The exact check of a rewrite is the evidence of a real test run. For a Bazel
project, where collecting the tests locally is impractical (`py_test`
targets that import through their runfiles, as most do), this is the
verification path: **apply with `--no-collect-check` (adding
`--trust-main-guard` only if needed), push, then `rr migrate verify` against
the CI evidence before merging.** `--trust-main-guard` is needed only when
apply refuses a file for code that only its `__main__` block runs (a
script-style test whose `main()` imports a helper by path, say); its
exclusion is best-effort, and the `verify` run is what checks the result.

```console
$ rr migrate apply requirements/attribution.rrplan --stage tags --no-collect-check
$ # only if apply refused a file for code only its __main__ block runs:
$ rr migrate apply requirements/attribution.rrplan --stage tags --no-collect-check --trust-main-guard
$ git push    # CI runs the tests; download its bazel-testlogs into ci/
$ rr migrate verify --worksheet requirements/attribution.rrplan \
    --evidence ci/bazel-testlogs/ --baseline bazel-testlogs/
```

Keep each evidence tree in a directory named `bazel-testlogs` (or
`testlogs`): a `test.xml`'s build target comes from its path
(`bazel-testlogs/<package>/<name>/test.xml`), so the same files under
`ci-testlogs/` would be keyed by suite name and match no case of the
worksheet. `verify` exits 2 with that hint when the worksheet's cases belong
to build targets and the evidence names none.

It checks, case by case (by {ref}`case key <case-keys>`, so pytest, node,
`rr_case.h`, googletest and every other evidence rr ingests alike):

- every case the worksheet decides declares **exactly** its owner in the
  `--evidence` (no id at all for `none`). A decided case with no result there
  is an error. A decided case that declared no id before the rewrite and
  declares none after counts only through `verified_by`, which apply does
  not edit (it lists the `keep` / `remove` / `split` edits): it is listed in
  a note, not an error. With `--model` (or the model named in the
  worksheet's `inputs`, when that is still there) the note says whether
  the model's claims select it for exactly its owner (selected as
  attribution selects them: a case selector reaches only the cases it
  matches, a whole-target claim every case of its target), or the case is
  still pending a model edit (a target split between owners needs case
  selectors: `rr migrate apply --stage model`); a decided case that declares
  exactly its owner while claims of other entities select it is noted too:
  from v0.3.0 a claim decides a case's owner over its tag (`tag-mismatch`),
  and claims of two entities quarantine it. Without a model, `verify` checks
  declared ids only.
  "Before" is the `--baseline` when it has the case, else the worksheet (a
  group with no `tags` has no tagged case); a tagged case that ends with no
  id lost its tag and is an error;
- with `--baseline` (the evidence the worksheet was planned from, or any run
  from before the rewrite), every other case — undecided, still open, or not
  on the worksheet — declares the same ids as before (in any order), and no
  case of the baseline is missing from the new evidence. A target-scope
  result (a whole run's exit status) carries the union of its cases' ids,
  which the decisions change: it must still be there, its ids are not
  compared. Cases only the new evidence has are counted, not refused;
- `--allow-missing` (a HITL or manual target CI does not run) makes a case
  with no result — decided or from the baseline — a warning, not verified,
  when its **target** has no result file at all in the new evidence. A
  target with any result file ran: a case, a target-scope or Bazel
  synthetic result, or a `test.xml` that holds no testcase (pytest collected
  nothing). Outside a testlogs tree such an empty report ran the targets its
  cases would be keyed under: `suite:<name>` for each of its `<testsuite>`
  names, the file stem only for an unnamed suite or none. A case missing
  from a target that did run stays an error: it was renamed or lost by the
  rewrite.

It prints one line per offending case, with the ids expected and found, and
exits 1 on any:

```text
//pi/server:server_test#pi.server.tests.test_handler::test_configure: a decided case does not declare exactly its owner (expected [PR-11], found [PR-11, PR-13])
//pi/server:server_test#pi.server.tests.test_session::test_resume: a case of the baseline has no result in the evidence (expected [PR-13], found no result)
```

Otherwise it exits 0 with a summary on stderr. It exits 2 when the worksheet
cannot be read or an owner is not one of the ids its case counts toward (with
`--model`, or the model named in the worksheet's `inputs` when that is still
there, also when it is not an entity of the model), when `--evidence` or
`--baseline` holds no evidence at all, or when the worksheet's cases belong
to build targets and the evidence names none (above). `--format` and `--ingestor` work as for
`rr cases`.

## Checking the result

Re-run the tests, verify them (above) and re-run the plan:

```console
$ bazel test //...
$ rr migrate plan --model requirements/ --evidence bazel-testlogs --merge requirements/attribution.rrplan --md attribution.md
```

Everything still listed is either decided but not yet applied (a non-Python
test, a split you have to make by hand) or a `split` target waiting for case
selectors. `rr report` verdicts change honestly as tags move. A requirement
that was VERIFIED only through a shared test now shows what it really has.

## On 0.3: pin the release in hybrid mode

0.3 changes semantics on purpose: a case whose evidence names two ids is
quarantined (it counts for nobody, every id it names reads INVALID, and
`rr report` exits 3), and a target that two entities claim — two
requirements listing one target in `verified_by`, say — is a `shared-case`
model error. The first half of this guide removed the multi-id tags; the pin
bump fixes the shared targets, which 0.2 cannot express per case.

1. Bump the pin (`bazel_dep(name = "rules_requirements", version = "0.3.0")`
   and its override) and run `rr validate requirements/`. Every
   `shared-case` error names the two entities, both selectors and an example
   case. A requirement that `refines` two or more parents is a
   `multi-parent-refines` error (refines must form a tree, so each case's
   evidence rolls up one chain of requirements: {ref}`derived-verdicts`):
   keep one parent and split the child into one requirement per parent, or
   set `config.rules: {multi-parent-refines: warning}` until you do.
2. Replace each shared whole-target reference with the cases each entity
   owns, as the worksheet decided them. `rr cases --evidence bazel-testlogs
   --target //web:clocksync_test` lists the exact case paths:

   ```yaml
   # before (0.2): both requirements list the whole target
   #   - id: PR-13
   #     verified_by: [//web:improv_provision_test]
   #   - id: PR-29
   #     verified_by: [//web:improv_provision_test]
   - id: PR-13
     verified_by:
       - target: //web:improv_provision_test
         cases:
           - "improv_provision::provisionViaBle: sends the correct wifi-settings wire and returns the redirect"
           - "improv_provision::provisionViaBle: surfaces a device error notification as a rejection"
   - id: PR-29
     verified_by:
       - target: //web:improv_provision_test
         cases: ["improv_provision::provisionViaBle: survives Android's first-attempt GATT flake via retry"]
   ```

   Other 0.2 references (a bare label, `{target, level}`) still parse as
   whole-target claims with a `bare-target-reference` warning; leave them
   for step 8, unless CI runs strict: `--strict`, `rr_model(strict = True)`
   and `rr_report(strict = True)` escalate that warning (and the other new
   0.3 warnings: `unknown-id`, `coarse-claim`, `suite-level-requirement`,
   `unscoped-evidence`, `same-path-multiple-owners`, ...) to errors. Then
   convert them in this change, set `config.rules: {bare-target-reference:
   warning}` (strict still escalates it), or drop strict until step 8. Two
   more effects of the bump: a whole-target claim of a target with no
   results in this evidence (another lane's HIL target, say) is now an
   expected not-run member, so its entity reads INCOMPLETE and `--fail-on
   unverified` fails (pass that lane's evidence too, or report it with
   `--lane`/`--lane-targets`); and `--fail-on gaps` fails on more gaps.
   Without `config.sets_lock` the report carries an `unpinned-sets` gap (set
   `sets_lock` and run `rr sets lock --write`, which works in hybrid mode),
   and every attribution issue is a gap, warnings included, so locking alone
   may not make `--fail-on gaps` pass. The attribution issues that are gaps:
   `duplicate-case`, `unscoped-evidence`, `suite-level-requirement`,
   `coarse-claim`, `tag-mismatch`, `unclaimed-tag`, `misdirected-evidence`,
   `unknown-id`, `same-path-multiple-owners`, `ambiguous-source`,
   `level-mismatch`, `unlocked-member`, `lock-stale`, `lock-owner-changed`,
   `lock-invalid` and `multi-verifies-annotation` (under `--scan`). Lock for
   the four lock findings; resolve the rest ({ref}`gap-issues` says how for
   each; `unscoped-evidence`, which no rule configures, means JUnit outside a
   testlogs tree: write it to `testlogs/<pkg>/<name>/test.xml`); or set a
   configurable rule `off` under `config.rules`; or stop gating on gaps
   until step 8.
3. Keep `attribution: hybrid`, the 0.3 default: a single-id tag still owns
   a case that no claim covers, so every module whose tags you rewrote keeps
   its owners. Set `config.main_repo` if other modules refer to your
   targets as `@your_repo//...`.
4. Before merging, check the attribution over fresh evidence from every lane
   (software and HITL):

   ```console
   $ rr attribution --model requirements/ --evidence bazel-testlogs hitl-testlogs --check
   ```

   It exits 1 on any quarantine, missing case or error-level issue.
   `rr report` would exit 3 on the same quarantines.

Verdicts change honestly at the bump. A requirement that was VERIFIED only
through a test it shared now shows what it has on its own; a requirement whose
set spans the software and HITL lanes reads INCOMPLETE in each lane's
report and VERIFIED only in a report over both lanes' evidence. Announce it
as a correction, not a regression.

## Moving to model mode: `rr migrate apply --stage model`

In hybrid mode a tag can still own a case, and a test deleted or renamed
silently leaves its requirement's set. Model mode makes the model the single
record: every owner comes from a claim, and tags only cross-check them.

Once the tags carry one id each and fresh evidence shows the decided
owners, `rr migrate apply PLAN.rrplan --stage model [--compress]
[--dry-run]` writes the claims: one selector per case each entity owns
through a tag today (with `--compress`, a `*` glob where it selects exactly
that entity's cases, none of them skipped, and overlaps no other claim; a
synthetic-only target gets `whole: true` with a reason). Before writing, it
proves that the owner table over the evidence given is unchanged under
`attribution: model` and that `check_claims` passes; it refuses a quarantine
and any worksheet decision the evidence contradicts. A case the worksheet
decided but the evidence given does not hold (another lane's test, say)
keeps the worksheet's owner: it gets a literal selector of that entity, and
the stage is refused unless exactly that entity's claims select it (no
claim, for `none`); a `--compress` glob never reaches such a case of another
owner. Cases in neither the evidence nor the worksheet are not seen: pass
every lane's evidence, or re-run `rr attribution --check` over it afterwards.

```console
$ rr migrate apply requirements/attribution.rrplan --stage model --compress \
    --model requirements/ --evidence bazel-testlogs hitl-testlogs --dry-run
$ rr migrate apply requirements/attribution.rrplan --stage model --compress \
    --model requirements/ --evidence bazel-testlogs hitl-testlogs
```

Then switch the mode and name the lock:

```yaml
config:
  attribution: model
  sets_lock: verification.rrlock
```

Verdicts over every lane's evidence are identical to the hybrid ones by
construction. A single lane's report now shows a set that spans lanes as
INCOMPLETE (its other lane's cases are not-run members), where hybrid mode,
whose tags only own the cases present, ignored them: report lanes with
`--lane`/`--lane-targets`, or gate per lane on the combined report
({doc}`outputs`). Keep the
single-id tags as cross-checks (a tag that disagrees with the model is a
`tag-mismatch`); new tests need none. `rr attribution --suggest` prints the
selector for any case still owned only by a tag, or tagged but unclaimed
(`unclaimed-tag`) afterwards.

## Locking the sets

```console
$ bazel test //...
$ rr sets lock --model requirements/ --evidence bazel-testlogs hitl-testlogs --write
$ git diff requirements/verification.rrlock
```

The lock maps every case to the one entity whose set holds it and adds
those cases as expected members: from now on a deleted, renamed or filtered
test makes its requirement INCOMPLETE (`missing-case`) instead of quietly
shrinking its set. It never creates ownership. Review its diff like a golden
file, and re-lock with every intended change ({ref}`verification-lock`).
In a hermetic Bazel project, `rr_model(lock = ...)` checks it statically and
`rr_sets_lock_test` against `rr_evidence`; `bazel run :<name>.update`
re-locks (the thermostat example does this).

Finally gate on it. In CI, after the report:

```yaml
- name: Attribution checks (one test case, one requirement)
  run: |
    bazel query 'tests(//...)' > "$RUNNER_TEMP/targets.txt"
    bazel run @rules_requirements//python:rr -- validate requirements --known-targets "$RUNNER_TEMP/targets.txt"
    bazel run @rules_requirements//python:rr -- sets check --model requirements --evidence "$(readlink -f bazel-testlogs)"
    bazel run @rules_requirements//python:rr -- check-report traceability-report.json
```

Then tighten the model: `bare-target-reference: error` and
`whole-target-reference: error` under `config.rules` once the remaining
whole-target claims are per case (or carry a reason), and `config.variants`
for targets that run the same test code. Record the policy and why you chose
it in your development plan ({ref}`standards-one-owner`).

## What 0.4 changes

v0.4.0 makes `attribution: model` the default (hybrid warns
`hybrid-mode`) and multi-id authoring impossible: a collection, import or
compile error. A project that has finished this guide needs no changes.

## Smaller 0.3 changes

Smaller v0.3.0 changes a script reading the reports or the Python API may
notice (the [release notes](../release-notes.md) list every change):

- A case is named by its case key, `<target>#<path>`, where 0.2 wrote
  `<target> <classname>::<name>`: in the JSON report's `unknown_evidence` and
  in the `unknown-id` gap.
- `summary.test_cases` counts case keys (retries, runs, shards and evidence
  roots merged; target-scope results not counted), not raw results.
- `TestCase.requirements` is a deprecated alias of `TestCase.declared`; it
  still reads, writes and works in `dataclasses.replace(case,
  requirements=...)`, but `dataclasses.asdict()` names the field `declared`,
  and passing both `declared=` and `requirements=` to the constructor is a
  `TypeError` (two sets of ids for one case: neither may silently win), even
  `declared=()` or another case's `declared`.
