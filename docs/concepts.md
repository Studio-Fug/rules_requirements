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
| `verified_by` | requirement → build target | Whole-target evidence (see [below](#evidence)). |
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
## Evidence

Evidence is a set of test cases, each with a status (`passed`, `failed`,
`error`, `skipped`), the ids it verifies, the level it provides and, optionally,
the identity of the artifact it exercised. It reaches an entity in two ways:

Per test case
: A `requirement` property on a JUnit `<testcase>` (written by the
  {doc}`hooks <guides/hooks>`) attaches that case to the id. This is the precise,
  preferred form.

Per target (`verified_by`)
: A requirement may list build targets (for example Bazel test labels) in
  `verified_by`. Each target's status is the most severe status among its
  cases (`error` > `failed` > `skipped` > `passed`), recovered from the
  `bazel-testlogs/<pkg>/<name>/test.xml` path. This suits suites that cannot
  tag individual cases. Note that a single skipped case makes the whole target
  count as skipped, so it then provides no passing evidence.

Evidence may be tagged with the id of a requirement, a user need (direct
validation evidence, below) or a mitigation. Evidence naming an id the model
does not define is reported as an `unknown-id` gap.

## Verdicts

### Requirements

For each requirement the matrix gathers its evidence and classifies it:

1. If any of it `failed` or `error`ed → <span class="rr-status">FAILED</span>.
2. Otherwise consider the *fresh* passing evidence (see
   [staleness](#staleness)); skipped cases never count as passing.
   - None at all → <span class="rr-status">UNVERIFIED</span>; or, if there is
     passing evidence but all of it is stale,
     <span class="rr-status">UNDER-VERIFIED</span> flagged **stale**.
   - The best ranked level provided meets or exceeds the demanded level →
     <span class="rr-status">VERIFIED</span>.
   - Otherwise → <span class="rr-status">UNDER-VERIFIED</span>: the requirement
     is exercised, but not with the rigor it asks for.

For an unordered demand (`inspection`) the requirement is VERIFIED only if some
passing evidence is at exactly that level.

**Refinement.** When other requirements `refine` a requirement, their statuses
roll up into it:

- the children's rollup is FAILED if any child failed, VERIFIED if all are
  verified, PARTIAL if at least one is verified, under-verified or partial, and
  UNVERIFIED otherwise;
- the parent is FAILED if it or any child failed;
- when every child is VERIFIED, the parent is VERIFIED only if its *own* demand
  is met — by its own evidence, or because every child's best evidence is at
  least as rigorous as the parent demands. A `hitl` system requirement is not
  proven by simulation-verified software requirements: it stays
  UNDER-VERIFIED until system-level evidence at `hitl` exists;
- otherwise a parent with any passing evidence of its own, or with partly
  verified children, is PARTIAL; a parent with neither stays UNVERIFIED.

### Rollups

| Entity | Rolls up | Verdicts |
| ------ | -------- | -------- |
| User need | requirements that `satisfies` it | VALIDATED · PARTIAL · FAILED · UNVALIDATED |
| Mitigation | requirements it is `implemented_by` | VERIFIED · PARTIAL · FAILED · UNVERIFIED |
| Risk | mitigations that `mitigates` it | MITIGATED · PARTIAL · FAILED · OPEN |
| Test method | requirements whose `method` names it | VERIFIED · PARTIAL · FAILED · UNVERIFIED |
| Module | requirements listing it in `modules` | VERIFIED · PARTIAL · FAILED · UNVERIFIED |

Every rollup uses the same rule: no children → the "none" verdict
(UNVALIDATED, UNVERIFIED, OPEN); any child FAILED → FAILED; every child fully
verified → the "all good" verdict; at least one child verified, under-verified
or partial → PARTIAL; otherwise the "none" verdict. An under-verified
requirement therefore makes its user need and its mitigation PARTIAL, never
VALIDATED or VERIFIED.

### Direct validation evidence

Evidence can be tagged with a user need or mitigation id directly — a usability
study that validates `UN-2`, say. Such evidence is not graded by level (any
pass counts) and joins the rollup as one more child: a failing usability study
makes the need FAILED even if every requirement is verified.

(staleness)=
## Staleness

"It passed once" and "it passes for the build we are shipping" are different
claims. Evidence can record the identity of what it exercised as
`artifact.<key>` properties — a firmware build id, a board revision, a DUT git
SHA. When a report is built with the current identity
(`--current-build KEY=VALUE`, or `current_build` in Bazel), a passing case
whose recorded identity differs on **any key both sides have** is **stale**.
Evidence without an identity, and `verified_by` target evidence, is never
stale.

Fresh evidence decides the verdict whenever there is any. A requirement whose
*only* passing evidence is stale is UNDER-VERIFIED and flagged stale (the
reports show a `STALE` badge), and the gap queue carries a `stale` item for it.

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
| `failed` | a requirement with failing evidence (also a user need or mitigation with failing direct evidence) | by demanded level — reproducing a bench failure needs the bench |
| `stale` | a requirement whose only passing evidence is stale | by demanded level |
| `unverified` | a requirement with no evidence | by demanded level |
| `under-verified` | a requirement whose evidence is below its demand | by demanded level |
| `partial` | a requirement whose refinements are only partly verified | by demanded level |
| `pyramid` | a cost-pyramid violation | autonomous |
| `no-implementation` | a requirement (without refinements) that no source annotation implements — only when sources were scanned | autonomous |
| `high-risk-open` | a high-severity risk that is not MITIGATED | human-gate |
| `unknown-id` | evidence tagged with an id the model does not define | autonomous |
| `misdirected-evidence` | evidence tagged with a risk or test-method id (they are not verified by tests — tag the requirement) | autonomous |
| `untraced-failure` | a failing test case that traces to no requirement (and whose target no `verified_by` names) | autonomous |
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
