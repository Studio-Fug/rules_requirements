# Concepts

This page defines the model's entities and how the traceability matrix turns a
model plus test evidence into verdicts. The rules here are exactly what
{py:mod}`rules_requirements.trace` implements.

## Entities

| Kind | Default prefix | What it is | Question the report answers |
| ---- | -------------- | ---------- | --------------------------- |
| User need | `UN` | What a user must be able to do. | Is it **validated**? |
| Requirement | `REQ` | A verifiable statement the product must meet. | Is it **verified**, at the rigor it demands? |
| Risk | `RISK` | A hazard → hazardous situation → harm chain, with estimated severity and likelihood. | Is it **mitigated**? |
| Mitigation | `MIT` | A risk control measure. | Is its implementation **verified**? |
| Test method | `TM` | A named verification procedure at a rigor level. | Which requirements rely on it, and do they pass? |

Ids are `<prefix>-<number>` by default (`REQ-7`, `REQ-0007`); prefixes and the
id pattern are configurable per project, so an existing `PR-12` scheme can be
kept ({ref}`config-reference`). Ids are unique across all kinds.

Every entity also carries optional `description`, `status`, `owner`, `tags`
and `notes`. Notes are how gaps found in review — by a person or an agent —
are attached to the object they concern; open notes appear in the reports.

## References

References always point from the more specific object to the more general one,
so each trace has exactly one source of truth:

| Field | From → to | Meaning |
| ----- | --------- | ------- |
| `satisfies` | requirement → user need | The requirement is (part of) how the need is met. |
| `refines` | requirement → requirement | Decomposition, e.g. system → software requirement. |
| `method` | requirement → test method *or* level | The verification rigor the requirement demands. |
| `verified_by` | requirement, mitigation → test cases | The cases that verify it ({ref}`claims <claims>`, see {ref}`below <evidence>`); a user need's are `validated_by`. |
| `mitigates` | mitigation → risk | The control acts on this risk. |
| `implemented_by` | mitigation → requirement | The requirements that realise the control. |
| `mitigated_by` | risk → mitigation | *Optional* back-reference; if given it must agree with `mitigates`. |

The reverse views ("which requirements satisfy UN-1?", "which risks does
REQ-4's mitigation control?") are computed. A requirement that implements a
mitigation needs no `satisfies` — it traces through the risk instead.

## Verification levels

A **level** is a rung of verification rigor. The default ladder is:

| Level | Rank | Meaning |
| ----- | ---- | ------- |
| `analysis` | 1 | Static argument: derivation, review of a proof, static analysis. |
| `simulation` | 2 | Host-side unit or simulation test. |
| `sil` | 3 | Software-in-the-loop: the integrated software against simulated I/O. |
| `hil` | 4 | Hardware-in-the-loop: a component on real hardware. |
| `hitl` | 5 | Full system on real hardware, end to end. |
| `inspection` | — | Manual or visual sign-off recorded as evidence. |

Ranked levels are ordered: evidence at `hitl` satisfies a `hil` demand, but
`simulation` evidence does not. **Unordered** levels such as `inspection` are
incomparable: an unordered demand is met only by evidence at exactly that
level, and unordered evidence never meets a ranked demand.

A requirement's `method` names either a level directly (`method: hil`) or a
[test method](#test-methods) (`method: TM-2`), whose `level` then applies.
Without a `method` the requirement demands `default_level` (`simulation`).
Evidence names the level it **provides** (a `level` property on the test case);
evidence without one provides `default_provided_level` (`simulation`). Both
defaults, and the ladder itself, are configurable.

(test-methods)=
### Test methods

A test method gives a verification procedure a name, a level and a description
of how it is carried out (`procedure`). Pointing several requirements at the
same test method keeps their demanded rigor consistent and lets the report list
which requirements depend on which procedure. A test method's own status is the
rollup of the requirements that use it.

(evidence)=
## Evidence and attribution

Evidence is a set of test cases, each with a status (`passed`, `failed`,
`error`, `skipped`), the level it provides, optionally the identity of the
artifact it exercised, and the ids it **declares** — its tags. Every case is
filed under a key, `<target>#<path>` ({ref}`case-keys`): retries, repeated
runs, shards and several evidence roots of one case merge into one result.

**A test case verifies at most one requirement**; a set of test cases may
together verify one. Which entity a case verifies — its *owner* — is decided
in one place, {py:func}`rules_requirements.attribution.attribute`, from two
inputs:

Claims (the model)
: `verified_by` on requirements and mitigations and `validated_by` on user
  needs name the cases of a target an entity claims — per case, by selector,
  or the whole target ({ref}`claims`). A case selected by exactly one entity's
  claims is owned by that entity.

Tags (the evidence)
: A `requirement` property on a JUnit `<testcase>` (written by the
  {doc}`hooks <guides/hooks>`), a record's `requirement`, an `[rr:ID]` name
  tag. With `config.attribution: hybrid` (the default) a single tag owns a case
  that no claim covers; with `model` a tag never owns anything and only
  cross-checks the claims (`tag-mismatch`, `unclaimed-tag`).

Ambiguity fails closed: a case is **quarantined** when its evidence names more
than one id (`multi-tag`), when claims of more than one entity select it
(`attribution-conflict`, also a static `shared-case` error), or when the same
test code — the same source file and case path, or targets declared in
`config.variants` — is owned by different entities in different targets
(`same-code-multiple-owners`; a source file is compared in one spelling, so
`./x.py`, `a/../x.py` and the absolute path a harness started outside the
workspace records are one file). Equal case paths with different owners whose
source is unknown or recorded differently get the `same-path-multiple-owners`
warning. A quarantined case owns nothing, and every
entity it names reads INVALID until it has one owner. A tag naming a risk or a
test method is `misdirected-evidence` and one naming an undefined id
`unknown-id`; neither owns anything.

A result about a whole target run — an exit status after passing cases, a load
error, a report that cannot be read (`rr.scope=target`) — is never a case of
anyone. It *taints* the target: every member claimed on it reads `error`, so
each requirement fails through its own members.

## Verdicts

### Verification sets

Each requirement, user need and mitigation has one **verification set**:

- the cases it **owns** (through its claims, or its tag in hybrid mode);
- the cases it **expects**: each literal selector's case, and each entry of the
  verification-set lock ({ref}`verification-lock`) naming it;
- a member for each selector that matched nothing, and for each quarantined
  case that names it.

A member's state is the result of its case (`passed`, `failed`, `error`,
`skipped`), or: `missing` (its target ran without it — a renamed or deleted
test, a filter), `not-run` (no evidence for its target at all — another lane,
a target that never built), `moved` (a lock entry whose case now has another
owner or none) or `quarantined`. A member's level is its case's own `level`,
else its claim's, else `default_provided_level`; when the case and the claim
disagree the lower one counts (`level-mismatch`).

### Requirements

A requirement's verdict is the first that applies to its set:

1. Any member quarantined → <span class="rr-status">INVALID</span>.
2. Any member failed or errored (a taint included), or a member passed only on
   a retry under `config.flaky: fail` → <span class="rr-status">FAILED</span>.
3. No members at all, or none of them ran →
   <span class="rr-status">UNVERIFIED</span>.
4. Any member missing, not run, skipped or moved (or the set mixes builds
   under `config.set_consistency: enforce`) →
   <span class="rr-status">INCOMPLETE</span>.
5. The whole set passed, but a member is [stale](#staleness), or passed only on
   a retry (`config.flaky: under-verify`, the default), or the best level of
   the set is below the demanded one →
   <span class="rr-status">UNDER-VERIFIED</span>.
6. Otherwise → <span class="rr-status">VERIFIED</span>: the whole set passed
   together, at the demanded rigor; it provides the best level among its
   members, the cheaper members being the pyramid's base.

For an unordered demand (`inspection`) the set must include a passing member at
exactly that level. `config.flaky` decides what a retry-masked pass is worth:
`accept` (a pass), `flag` (a pass and a `flaky` gap), `under-verify` or
`fail`. Only an earlier failed or errored attempt makes a member flaky.

**Refinement.** When other requirements `refine` a requirement, their verdicts
roll up into it — verdicts, never cases: a parent's set holds only its own
claims. Each verdict says what it rests on: `basis` is `own` (its own set),
`derived` (other entities' verdicts, listed in `derived_from`) or
`own+derived`.

- the children's rollup is FAILED if any child failed or is INVALID, VERIFIED
  if all are verified, PARTIAL if at least one is verified, under-verified,
  partial or incomplete, and UNVERIFIED otherwise;
- the parent is INVALID if its own set is, and FAILED if its own set or any
  child failed;
- a parent with claims of its own needs its own set complete too: while it is
  incomplete (or did not run), the parent is INCOMPLETE;
- when every child is VERIFIED, the parent is VERIFIED only if its *own* demand
  is met — by its own set, or because every child's best evidence is at least
  as rigorous as the parent demands (a stale or flaky own set is not made good
  by the children). A `hitl` system requirement is not proven by
  simulation-verified software requirements: it stays UNDER-VERIFIED until
  system-level evidence at `hitl` exists;
- otherwise a parent whose own set passed, or with partly verified children,
  is PARTIAL; a parent with neither stays UNVERIFIED.

### Rollups

| Entity | Rolls up | Verdicts |
| ------ | -------- | -------- |
| User need | requirements that `satisfies` it, and its own set | VALIDATED · PARTIAL · FAILED · INVALID · UNVALIDATED |
| Mitigation | requirements it is `implemented_by`, and its own set | VERIFIED · PARTIAL · FAILED · INVALID · UNVERIFIED |
| Risk | mitigations that `mitigates` it | MITIGATED · PARTIAL · FAILED · OPEN |
| Test method | requirements whose `method` names it | VERIFIED · PARTIAL · FAILED · UNVERIFIED |
| Module | requirements listing it in `modules` | VERIFIED · PARTIAL · FAILED · UNVERIFIED |

Every rollup uses the same rule: no children → the "none" verdict
(UNVALIDATED, UNVERIFIED, OPEN); any child FAILED or INVALID → FAILED; every
child fully verified → the "all good" verdict; at least one child verified,
under-verified, partial or incomplete → PARTIAL; otherwise the "none" verdict.
An under-verified or incomplete requirement therefore makes its user need and
its mitigation PARTIAL, never VALIDATED or VERIFIED, and an INVALID one makes
them FAILED. A user need or mitigation that is itself named by a quarantined
case is INVALID.

### Direct validation evidence

A user need's `validated_by` and a mitigation's `verified_by` claim cases of
their own — a usability study that validates `UN-2`, say (in hybrid mode a
case tagged with the need's id works too). Such a set is not graded by level
(any pass counts) and joins the rollup as one more child: a failing usability
study makes the need FAILED even if every requirement is verified.

(staleness)=
## Staleness

"It passed once" and "it passes for the build we are shipping" are different
claims. Evidence can record the identity of what it exercised as
`artifact.<key>` properties — a firmware build id, a board revision, a DUT git
SHA. When a report is built with the current identity
(`--current-build KEY=VALUE`, or `current_build` in Bazel), a passing case
whose recorded identity differs on **any key both sides have** is **stale**.
Evidence without an identity is never stale.

A verification set must hold on the current build as a whole: a set that passed
but has any stale member is UNDER-VERIFIED and flagged stale (the reports show
a `STALE` badge), and the gap queue carries a `stale` item for it. Passed
members stamped with different values of one key (two `dut_git_sha` values in
one set) are *mixed builds*: a `mixed-builds` gap with
`config.set_consistency: warn` (the default), INCOMPLETE with `enforce`.

## The cost pyramid

Physical verification is expensive; cheap verification should back it rather
than be skipped. A requirement that demands at least `pyramid_min_level`
(default `hil`) and has passing *physical* evidence (at `pyramid_min_level` or
above), none of which is backed by evidence at one of the
`pyramid_cheap_levels` (default `analysis`, `simulation`), is a **cost-pyramid
violation**: a physical result nobody sanity-checked cheaply. (Evidence below
the demand without any physical result is simply under-verified.) Violations are
listed in the reports and as `pyramid` gaps; `rr report --pyramid-policy`
decides whether they only warn (default), fail the command, or are ignored.

## High-severity risks

A risk whose `severity` is one of `high_severities` (default `high`,
`critical`) and whose verdict is anything but MITIGATED is hoisted into a banner
at the top of the reports and becomes a `high-risk-open` gap. Mitigation is not
elimination: a high risk controlled only by an under-verified requirement is a
headline, not a checkmark. The check uses the risk's initial `severity`, not its
residual severity.

(gaps)=
## Gaps and routing

The matrix ends with a list of **gaps** — everything between the model and a
complete verification and validation argument — which `rr report --queue-out`
writes as a machine-readable work queue:

| Gap kind | Raised for | Route |
| -------- | ---------- | ----- |
| `multi-tag`, `attribution-conflict`, `same-code-multiple-owners` | a quarantined case (one gap per case, naming every claim's origin and the ids its evidence declares); first in the queue | autonomous |
| `invalid` | an entity a quarantined case names | autonomous |
| `failed` | a requirement with a failed or errored member (also a user need or mitigation whose own set has one), or failing refinements | by demanded level — reproducing a bench failure needs the bench |
| `incomplete` | a requirement whose set is incomplete: `17/23 passed; 6 not run (//pi/hitl/harness:e2e_netstack)` | human-gate if a member that did not run or was skipped is above `autonomous_max_level`, else autonomous |
| `missing-case` | a selector or lock entry whose case its target did not report, with the nearest case it did report | autonomous |
| `stale` | a requirement whose set passed with a stale member | by demanded level |
| `flaky` | a requirement whose set passed with a retry-masked member (`config.flaky: under-verify` or `flag`) | by demanded level |
| `mixed-builds` | a set whose passed members were stamped with different builds (`config.set_consistency: warn`) | by demanded level |
| `unverified` | a requirement with no members (`no test evidence`), or none that ran (`not run: <targets>`) | by demanded level; by the members' levels when they did not run |
| `under-verified` | a requirement whose set is below its demand | by demanded level |
| `partial` | a requirement whose refinements are only partly verified | by demanded level |
| `pyramid` | a cost-pyramid violation | autonomous |
| `no-implementation` | a requirement (without refinements) that no source annotation implements — only when sources were scanned | autonomous |
| `misdirected-evidence` | evidence tagged with a risk or test-method id (they are not verified by tests — tag the requirement) | autonomous |
| `unattributed-failure` | a failing case no entity owns, or a target-scope failure that affects no member (`untraced-failure` is still emitted alongside in 0.3) | autonomous |
| `tag-mismatch`, `unclaimed-tag` | a tag that disagrees with the claim owning its case, or (`attribution: model`) a tag on a case no claim selects | autonomous |
| `duplicate-case`, `coarse-claim` | one case key reported twice in a run; a whole-target claim on a target with per-case results | autonomous |
| `unlocked-member`, `lock-owner-changed`, `lock-stale`, `lock-invalid` | the lock disagrees with the attribution ({ref}`verification-lock`) | autonomous |
| `same-path-multiple-owners`, `level-mismatch`, `unscoped-evidence`, `suite-level-requirement` | equal case paths in two targets with different owners and no common recorded source; a case level that differs from its claim's; JUnit outside a testlogs tree; a suite-level requirement property (not inherited) | autonomous |
| `unpinned-sets` | no lock: the entities whose sets have glob, whole-target or tag-owned members | autonomous |
| `high-risk-open` | a high-severity risk that is not MITIGATED | human-gate |
| `unknown-id` | evidence tagged with an id the model does not define | autonomous |
| `note:gap`, `note:todo`, `note:question` | an open note of that kind on any entity (questions route to a human) | by demanded level |

**Routing** splits the work between agents and people. A gap whose
requirement demands a level at or below `autonomous_max_level` (default `sil`)
is `autonomous`: an agent can write the missing unit, simulation or
software-in-the-loop test itself. Anything demanding more — `hil`, `hitl`, or an
unordered level such as `inspection` — is a `human-gate`: it needs a bench, a
device or a signature, and an agent may prepare the harness but must not
synthesise the evidence.

## What is not modelled

- **Benefit–risk analysis** (ISO 14971 §7.4) and the **overall residual risk**
  judgement (§8) are decisions, not computations; record them in the risk's
  `residual` note and in your risk management file.
- **Software safety classification** (IEC 62304 §4.3) is not a field; record it
  in `project:` metadata and express the rigor it implies through levels and
  test methods.
- The tool records and checks traceability. Whether a test actually proves a
  requirement is a review question — which the gap queue and notes help you ask,
  but do not answer.
