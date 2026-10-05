# One test case, one requirement

**A test case verifies at most one requirement.** A set of test cases may
together verify one requirement. And the system is built so that it is
*impossible* for one test case to verify two requirements: not discouraged,
not warned about, impossible. This page explains the rule, the structures
that carry it, and why no path through the tool can break it. The rule
applies to user needs and mitigations as well: requirements, user needs and
mitigations share one namespace, so a case verifies at most one *entity*.
Verdicts derived through `refines`, `satisfies` and `implements` are not
ownership; {ref}`derived-verdicts` explains why they cannot make one case
verify two requirements or two mitigations.

## Why the rule exists

When one test counts toward two requirements, both verdicts depend on one
result, and a reviewer cannot tell which requirement the test was written
for. A failure has to be traced back through every requirement it touches,
and a change to that test changes the evidence for all of them at once. With
one owner per case, every verification record is unambiguous: a case's result
is evidence for exactly one requirement, and change-impact analysis is exact.
Removing, renaming or breaking a test affects one verification set, and the
report says which one.

The rule is stricter than the standards that inspired the model require
(see {ref}`standards-one-owner`). It is a deliberate policy of this project.

## Verification sets

Each requirement, user need and mitigation has one **verification set**: the
test cases it owns, plus the cases it *expects*. These are each literal
selector's case and each entry of the verification-set lock that names it.
"Must pass together" is evaluated over the whole set: a requirement is
VERIFIED only when every member is present, passed (not just on a retry),
fresh against the current build, and at the demanded rigor
({ref}`verification sets <evidence>` in {doc}`concepts`).

Sets are how several test cases verify one requirement. There is one set per
entity, never alternatives: an "either of these sets" rule would let a
failing set be ignored. If a requirement really has independent proofs, the
honest model refines it into child requirements, each with its own set.

## The owner function

Which entity a test case verifies (its **owner**) is decided in exactly one
place: {py:func}`rules_requirements.attribution.attribute`. Everything else in
the tool only produces its inputs, or reads its output.

- **Claims come from the model.** `verified_by` on requirements and
  mitigations and `validated_by` on user needs name the cases of a target,
  per case by selector (`cases: ["clocksync::*"]`) or the whole target
  (`whole: true` with a `reason`) ({ref}`claims`).
- **Declared ids come from the evidence.** These are the tags a hook writes
  (`@pytest.mark.rr("REQ-1")`, `RR_VERIFIES`, `rr::verifies!`, a record's
  `requirement`, an `[rr:ID]` name tag). Ingest stores them as
  `TestCase.declared`. A declared id is never an owner.

`attribute()` returns an `Attribution` whose `owner` maps each case key
(`<target>#<path>`, {ref}`case-keys`) to one entity id. A mapping is a
function: a key cannot have two values, so no case can have two owners. Per
case, the first rule that applies decides:

1. The evidence names more than one distinct id: the case is quarantined
   (`multi-tag`).
2. Claims of more than one entity select it: the case is quarantined
   (`attribution-conflict`).
3. Exactly one entity's claims select it: that entity owns it. A tag naming
   another id is a `tag-mismatch` warning; the model wins.
4. No claim selects it and it declares one id:
   - with `config.attribution: hybrid` (the 0.3 default), that tag owns it, if
     the id names a requirement, user need or mitigation;
   - with `config.attribution: model` it stays unowned (`unclaimed-tag`).
5. A tag naming a risk or a test method is `misdirected-evidence`, and one
   naming an undefined id is `unknown-id`. Neither owns anything.

After that, owned cases with the same test code are compared: the same source
file and case path in two targets, or equal paths in targets that
`config.variants` declares as one test code. If they have different owners,
every one of them is quarantined (`same-code-multiple-owners`). So are equal
case paths with different owners when one of them is filed under a `suite:`
or `record:` pseudo-target and a source file is not recorded for each: a
pseudo-target (JUnit outside `bazel-testlogs`, say) cannot be pinned to a
build target, so a copy of one target's results could otherwise count for a
second requirement. Equal paths of two real build labels with unknown sources
stay a `same-path-multiple-owners` warning.

In **model mode** the model alone decides. Tags are cross-checks that can
only raise a warning, and a test needs no tag at all. In **hybrid mode**, the
0.3 transition, a single tag may still own a case that no claim covers.
Neither mode can produce a second owner. The
[thermostat example](https://github.com/Studio-Fug/rules_requirements/tree/main/examples/thermostat)
runs in model mode with a lock; that is the end state 0.3 asks projects to
reach ({doc}`guides/migrating-to-per-case`).

(derived-verdicts)=
## Derived verdicts are not ownership

Ownership is one entity per case: `attribute()` gives each case at most one
owner, and only that owner's verification set holds it. Some verdicts are
not computed from a set at all, or not only from one. They are *derived*
from other entities' verdicts along the model's trace links:

- a requirement that others **refine** rolls up its children's verdicts;
- a user need's VALIDATED rolls up the requirements that **satisfy** it;
- a mitigation rolls up the requirements that **implement** it, and a risk
  rolls up its mitigations.

The report marks these with `basis: derived` (or `own+derived` when the
entity also has a set) and lists the entities in `derived_from`. A derived
verdict is a statement about other verdicts, not about cases: no case joins
the parent's set, no case gets a second owner, and `rr check-report` rejects
a `derived_from` that names anything but the entity's children. Derived
verdicts are not ownership.

**Refines must form a tree.** With one parent per requirement, the
requirements above a case form a single chain: its owner, the owner's parent,
that parent's parent, and so on to the root. Each case then supports exactly
one chain of requirements. A requirement that refined two parents would make
each of its cases the whole basis of both parents' VERIFIED verdicts: two
requirements verified by one test case through rollups. So `rr validate`
raises `multi-parent-refines`, an error by default, and so do `rr_model`'s
`<name>_test`, `rr report` (exit 2) and the web editor's save guard (409).
The fix is to keep one parent and split the child into one requirement per
parent, each with its own cases. A project restructuring an old model can set
`config.rules: {multi-parent-refines: warning}` for a while; the report then
shows the shared rollup honestly (`basis: derived` on both parents, each
`derived_from` the one child), but the rule is the tool's default for a
reason.

**A requirement that implements a mitigation has no other parent.** A
mitigation's VERIFIED is derived from the requirements that implement it,
exactly as a parent requirement's is from its children, so a mitigation is a
parent too. A requirement implementing two mitigations, or implementing one
and refining a requirement, would make each of its cases the basis of two
VERIFIED verdicts that are not on one chain. `rr validate` raises
`multi-parent-implements`, an error by default, wherever
`multi-parent-refines` is raised. The fixes keep the traceability: one
mitigation may mitigate several risks, so two mitigations one requirement
implements can be merged into one that mitigates both risks; or the parent
requirement implements the mitigation, and its children's cases roll up one
chain (child, parent, mitigation); or the requirement is split, one per
parent. `config.rules: {multi-parent-implements: warning}` relaxes it the way
`multi-parent-refines` is relaxed.

**One requirement may satisfy several user needs.** Each of those needs'
VALIDATION is then derived from it, and that is allowed. User needs are
validated, not verified: VALIDATED says that the requirements written for a
need hold, which is a judgement about requirements, not a second use of a
test case. The case still verifies exactly one requirement, and that
requirement's verdict is computed once, from one set. Several needs reading
it is ordinary traceability (one capability serving several needs), and
forbidding it would force a project to duplicate requirements and their
tests, which is the opposite of what the rule is for. A need that also claims
cases of its own (`validated_by`) owns those cases like any other entity.
The same holds one level up the risk file: a risk reads MITIGATED from its
mitigations, a judgement about risk control rather than a verification, so
one mitigation may mitigate several risks. Test methods and modules also roll up requirements,
but they are views that group requirements by how they are verified and
where they are implemented: they claim no cases, have no verification set,
and are not entities a test case could verify.

## Quarantine: ambiguity fails closed

When ownership is ambiguous, the tool does not guess. The case is
**quarantined**:

- it owns nothing, so it counts for no entity;
- every entity it names reads **INVALID**, through a `quarantined` member in
  its set;
- the report shows a banner, and the gap queue gets one gap per case that
  names every claim's origin and every declared id;
- `rr report` prints an `ATTRIBUTION ERROR` line and exits 3, after writing
  the reports. In Bazel, `rr_report` fails the build.

```text
INVALID: REQ-3, REQ-4
ATTRIBUTION ERROR: multi-tag: //:setpoint_test#tests::requires_a_unit declares REQ-3, REQ-4; a test case verifies at most one requirement, so it verifies none of them until its evidence names one (it is claimed by REQ-3 (requirements/requirements.yaml:39))
rr: 1 quarantined test case(s) count for no requirement; every entity they name is INVALID; exit 3 (--on-attribution-error=warn to report without failing)
```

A multi-id tag therefore never gains anything: rather than counting twice,
the case counts for nobody, and every requirement it names is marked. There
is deliberately no "legacy many-to-many" switch: it would make the guarantee
optional. `--on-attribution-error=warn` (`rr_report(on_attribution_error =
"warn")`) only changes the exit status, never a verdict. It exists for
reports whose quarantines are intentional test fixtures, such as this
repository's hook integration test.

## INVALID and INCOMPLETE

Two verdicts exist because of the rule:

<span class="rr-status">INVALID</span>
: A quarantined case names the entity. Its evidence is ambiguous, so nothing
  can be concluded until the case has one owner: fix the test's tag or narrow
  a claim. INVALID rolls up like FAILED (a user need, a mitigation or a risk
  above it reads FAILED), counts as failed for `--fail-on failed`, and is
  first in the verdict order: an INVALID set may also hold failures, but the
  ambiguity is reported first.

<span class="rr-status">INCOMPLETE</span>
: The set is not whole. A member is `missing` (its target ran without it: a
  renamed, deleted or filtered test), `not-run` (no evidence for its target in
  this report, for example another lane's), `skipped`, or `moved` (a lock
  entry whose case now has another owner). Nothing failed, but the set did not
  pass together. INCOMPLETE rolls up like PARTIAL and counts as unverified for
  `--fail-on unverified`.

A set that spans the software and the HITL lane reads INCOMPLETE in each
lane's report. Only the combined report over both lanes' evidence can read
VERIFIED: lanes never soften a verdict.

## Why it cannot be bypassed

The guarantee is built in layers. Each one would hold the rule on its own;
together they catch a mistake as early as possible and check the published
result independently.

**L0: authoring.** Every hook writes at most one `requirement` per test case.
The older multi-id forms still record every id in 0.3 but warn (RR-E101), and
0.4 rejects them at compile, import or collection time. pytest records only the
nearest scope's id, and its RR-E102 guard fails a test that writes a raw
`requirement` property past the single-id API ({doc}`guides/hooks`).

**L1: the static model.** `rr validate` compares the claims of different
entities on every target. A whole-target claim overlaps everything, and two
selectors overlap exactly when some case path matches both. The grammar's only
wildcard is `*`, so this is decided exactly, and the error names an example
path (`shared-case`). Targets declared in `config.variants` are compared the
same way (`same-code-multiple-owners`), and the lock is checked against the
claims. These are hard errors, not rules: naming one under `config.rules` is
itself an error. They run in `rr validate`, in `rr_model`'s `<name>_test`, in
the web editor's save guard (a save that would introduce one is refused with
409) and in `rr report`, which refuses an invalid model with exit 2. So a
shared case is caught before any test runs:

```text
requirements/requirements.yaml:55: error: [shared-case] REQ-4 and REQ-3 both claim cases of //:setpoint_test ('tests::requires_*' vs 'tests::requires_a_unit'), e.g. 'tests::requires_a_unit' (REQ-3 claims it at requirements/requirements.yaml:39). A test case verifies at most one requirement: narrow one selector.
```

**L2: ingest.** Ingest records what the evidence declares and nothing more.
Every `requirement` value, from properties, attributes, records, Rust trace
lines, node diagnostics and name tags, is split on commas and whitespace into
`TestCase.declared`. A case declaring two distinct ids keeps both, so
attribution can quarantine it. `TestCase.requirements` is only a deprecated
alias of `declared`, so a third-party ingestor that fills it produces tags,
never owners.

**L3: attribution.** One function builds the owner map and fails closed on
any ambiguity (above). Retries, repeated runs, shards and several evidence
roots of one case are merged into one result before attribution, so one case
can never appear as two. Each `Attribution` is checked by
`check_invariant()`: the owner map is a function, the owned members of all
sets partition the owned cases, no case is both owned and quarantined, and
every entity a quarantine names holds a `quarantined` member.

**L4: verdicts.** Verdicts, rollups, report rows, the editor's panels and the
agents' context all read an entity's cases from the attribution
(`Attribution.members_of`) and from nothing else. No code reads tags to
decide a verdict; `Evidence.for_id`, the old tag lookup, is deprecated and
unused. `build_matrix` always runs `attribute()`, and nothing stores an owner
anywhere that could be read instead. The verification-set lock maps each case
to one id and only adds *expected* members: it can make a set INCOMPLETE,
never give a case an owner.

**L5: gates.** `rr report` exits 3 on any quarantine, and `rr_report` fails
the build. `rr sets check` (`rr_sets_lock_test` in Bazel) exits 1 when the
lock and the evidence disagree: a missing case, an unlocked member, an owner
change or a stale entry. `rr attribution --check` exits 1 on a quarantine, a
missing case, lock drift or an error-level issue. Configuration cannot turn
these off: `--strict` only escalates warnings to errors, and
`--on-attribution-error=warn` changes the exit status, never a verdict.

**L6: audit.** `rr check-report report.json` re-proves the partition from the
published JSON alone, without the code that wrote it. It fails if any case
key is owned by two entities, if a key is not in the one spelling `rr
report` writes (so one case cannot hide as two keys: `@//p:t`, a padded or
non-NFC path, an `[rr:ID]` name tag; `attribution.main_repo` makes
`@<main_repo>//` collapse too), if one test's code (same file and path, a
`variants` group, or a pseudo-target without recorded sources) is owned by
two entities, if a case naming two ids or a tag that names another id owns
anything, if an owner is not a single id, if a quarantined case is owned, if
an entity a quarantine names is not INVALID, if a verdict its own set cannot
back (an entity with members may not relabel its basis `derived`) or a rollup
from a non-child is claimed, or if the counts disagree with
the rows they count ({doc}`guides/outputs`).
`rr_report` adds it as `<name>_check_test` whenever it builds a JSON report.
Property tests guard the code itself: one regression test per path by which a
case could acquire a requirement, and a seeded fuzz of attribution that
checks the invariant, that every report-time conflict has a static witness,
and that `rr check-report` accepts every generated report and rejects a
mutated one.

The argument in one paragraph: ownership is represented as a scalar per case
key, and a set is the cases one entity owns. Every mechanism (hooks, records,
claims, the lock, the editor, the agents) only produces claims or declared
tags; ownership exists only inside `attribute()`, which every reader calls.
Ambiguity removes the evidence and visibly marks every entity it touches. Errors
are caught before evidence exists (L0, L1, the editor), and the published
artifact is audited independently afterwards (L6).

## What this asks of a project

- **Claim cases, not targets.** Name each requirement's cases with selectors.
  Reserve `whole: true` (with a reason) for targets that only produce Bazel's
  single synthetic result. `rr cases --evidence bazel-testlogs` lists the
  exact case paths to copy.
- **Tag one id per test, or none.** In model mode a tag is a cross-check; a
  test that verifies two things should become two tests. The thermostat's
  Rust test `requires_a_unit` named REQ-3 and REQ-4 in 0.2; in 0.3 it
  verifies REQ-3 (a unit is required), and a new test,
  `checks_the_range_after_converting`, verifies REQ-4.
- **Lock the sets.** `rr sets lock --write` (in Bazel, `bazel run
  //:<lock_test>.update`) writes `verification.rrlock`. Review its diff like a
  golden file: a deleted or renamed test then shows up as a lock change, not a
  silently smaller set.
- **Gate on it.** Keep `rr_report`'s default `on_attribution_error = "fail"`,
  run `rr check-report` on the JSON you publish, and run `rr sets check`
  against fresh evidence.

{doc}`guides/migrating-to-per-case` walks an existing project from 0.2's
union rule to this end state without a flag day.
