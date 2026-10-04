# The model

The model is a set of YAML (or JSON) documents describing user needs,
requirements, risks, mitigations and test methods. This page is the reference
for every field, the `config:` section, the supported file layouts and the
validation rules. {doc}`../concepts` explains what the verdicts mean.

## File layouts

Model files are merged, so you can organise them however suits review:

**Section documents** hold any mix of the top-level sections:

```yaml
project: {name: Thermostat}
config: {acceptable_risk_score: 6}
user_needs: [...]
requirements: [...]
risks: [...]
mitigations: [...]
test_methods: [...]
```

**Single-object documents** hold one entity, named by `kind` (`user_need`,
`requirement`, `risk`, `mitigation`, `test_method`; the plural section names
are accepted too):

```yaml
# requirements/REQ-0007.yaml
kind: requirement
id: REQ-0007
title: Lock the door while heating
satisfies: [UN-0002]
```

One object per file makes merges and reviews of individual items trivial; it
is the layout the web editor writes.

Loading rules:

- Paths may be files or directories. Directories are searched recursively for
  `*.yaml`, `*.yml` and `*.json`, skipping directories whose name starts with a
  dot, in sorted order.
- A file may contain several YAML documents separated by `---`.
- `config:` may appear in only one document. `project:` (or its alias `meta:`)
  mappings from all documents are merged.
- An id may be defined only once across all files and all kinds.
- Every problem is reported with its `file:line`.

(common-fields)=
## Fields common to every entity

| Field | Type | Notes |
| ----- | ---- | ----- |
| `id` | string, **required** | Matches the kind's id pattern (default `<PREFIX>-<digits>`). |
| `title` | string, **required** | One line. |
| `description` | string | Free text; shown in reports. |
| `status` | enum | `draft`, `proposed`, `approved`, `implemented`, `obsolete`. Informational. |
| `owner` | string | Who is responsible. |
| `tags` | list of strings | Free-form labels. |
| `notes` | list | See [notes](#notes). |

Every list-valued field (`tags`, `satisfies`, `refines`, `modules`,
`mitigates`, `implemented_by`, `mitigated_by`) also accepts a single string,
split on commas: `satisfies: UN-1, UN-2`.

(notes)=
### Notes

Notes attach review findings and follow-up work to the object they concern. A
note is a string or a mapping:

```yaml
notes:
  - "plain comment"
  - text: The cutoff threshold is not justified anywhere.
    kind: gap          # comment (default) | gap | question | todo
    status: open       # open (default) | resolved
    author: reviewer-bot
    created: 2026-09-30
    id: n2             # defaults to n<position>
```

Open notes are listed with their entity in the reports.

## User needs

`user_needs:` entries — what a user must be able to do.

| Field | Type | Notes |
| ----- | ---- | ----- |
| `rationale` | string | Why the need exists. |
| `validated_by` | list of claims | Validation evidence (a usability study, an acceptance run): [claims](#claims) in the same namespace as `verified_by`. |

## Requirements

`requirements:` entries — verifiable statements the product must meet.

| Field | Type | Notes |
| ----- | ---- | ----- |
| `rationale` | string | Why the requirement exists. |
| `category` | string | Free-form (`functional`, `safety`, `performance`, ...). |
| `satisfies` | list of UN ids | The needs this requirement helps meet. |
| `refines` | list of REQ ids | Parent requirements this one decomposes. Cycles are errors. |
| `method` | TM id or level | The verification rigor demanded. Default: `config.default_level`. |
| `verified_by` | list of claims | The test cases that verify it: see [claims](#claims). |
| `modules` | list of strings | Implementing modules (documentation aid; drives the per-module rollup). |

A requirement must trace upward — `satisfies` a need, `refines` another
requirement, or be `implemented_by` a mitigation — or it is an orphan.

## Risks

`risks:` entries — a hazard → hazardous situation → harm chain (ISO 14971).

| Field | Type | Notes |
| ----- | ---- | ----- |
| `hazard` | string | Potential source of harm. |
| `hazardous_situation` | string | Circumstance of exposure. |
| `harm` | string | The injury or damage. |
| `severity` | enum | One of `config.severities` (default `negligible`, `low`, `medium`, `high`, `critical`). |
| `likelihood` | enum | One of `config.likelihoods` (default `rare`, `unlikely`, `possible`, `likely`, `certain`). |
| `residual_severity` | enum | Severity after risk control; if omitted, the initial `severity` is used. |
| `residual_likelihood` | enum | Likelihood after risk control; if omitted, the initial `likelihood` is used. |
| `residual` | string | Residual-risk note or acceptance rationale. |
| `mitigated_by` | list of MIT ids | Optional back-reference; must list exactly the mitigations whose `mitigates` names this risk. |

The **risk score** is `(severity index + 1) × (likelihood index + 1)` on the
configured scales (so 1–25 on the default five-point scales). The residual score
uses the residual fields, falling back to the initial ones.

## Mitigations

`mitigations:` entries — risk control measures.

| Field | Type | Notes |
| ----- | ---- | ----- |
| `type` | enum | `inherent` (safety by design), `protective` (protective measure), `information` (information for safety) — the ISO 14971 §7.1 options. |
| `mitigates` | list of RISK ids | **Required** (at least one). |
| `implemented_by` | list of REQ ids | The requirements that realise the control. |
| `verified_by` | list of claims | Effectiveness evidence of the control (ISO 14971 §7.2): see [claims](#claims). |

## Test methods

`test_methods:` entries — named verification procedures.

| Field | Type | Notes |
| ----- | ---- | ----- |
| `level` | level, **required** | The rigor this method provides and demands. |
| `procedure` | string | How the method is carried out. |

(claims)=
## Claims: which test cases verify an entity

**A test case verifies at most one requirement.** A set of test cases may
together verify one requirement, but no case ever counts toward two. Claims
are how the model says which cases belong to whom: `verified_by` on
requirements and mitigations, `validated_by` on user needs — one namespace,
so a need, a requirement and a mitigation can no more share a case than two
requirements can. Risks and test methods hold no claims.

Each item names one target and either the cases it claims or the whole target:

```yaml
requirements:
  - id: PR-29
    verified_by:
      - target: //web:improv_provision_test
        cases: ["improv_provision::provisionViaBle: survives Android's first-attempt GATT flake via retry"]
      - target: //pi/hitl/tests:hitl_test
        cases: ["pi.hitl.tests.test_improv::test_retry_*"]   # '*' is the only wildcard
      - target: //pi/hitl/harness:e2e_netstack
        level: hitl                                          # for cases that declare no level
        cases: [hitl_e2e.improv_provision::readiness_retry]
  - id: PR-25
    verified_by:
      - {target: //requirements:model_test, whole: true, reason: "rr validate runs as one test"}
user_needs:
  - {id: UN-5, validated_by: [{target: "record:usability_study", cases: ["*"]}]}
```

| Key | Meaning |
| --- | ------- |
| `target` | A Bazel label, or a pseudo-target: `suite:<testsuite name>` (JUnit outside a `bazel-testlogs` tree) or `record:<stem>` (records without a target). |
| `cases` | A non-empty list of case selectors (below). `["*"]` claims every per-case result of the target, and is preferred over `whole` whenever the target reports per-case results. |
| `whole: true` | Every result of the target — its single synthetic result when it reports no per-case results (Bazel's generated `test.xml`). Give a `reason`. |
| `level` | The level provided by claimed cases that declare none (default `config.default_provided_level`). |
| `reason` | Why a `whole` claim cannot be per-case. |

An item has exactly one of `cases` and `whole: true`. The 0.2 forms still
parse — a bare label, `{target}` or `{target, level}` — as a whole-target claim,
with a `bare-target-reference` warning (an error from 0.4).

A malformed item — both or neither of `cases` and `whole: true`, an empty or
non-list `cases`, `whole: false`, a `reason` without `whole: true` — is a
`bad-selector` error, and until it is fixed it claims the **whole** target,
whatever cases it lists: it can only add `shared-case` conflicts, never hide
one. The web editor shows such an item exactly as written and refuses to
rewrite the entity until it is fixed by hand.

**Case selectors** match the whole case path (`<classname>::<name>`, see
`rr cases`), case-sensitively. `*` matches any string, including the empty
string, `::`, `/` and blanks; `**` is the same as `*`. `\*` is a literal star
and `\\` a literal backslash; any other backslash is a `bad-selector`. `?`, `[`
and `]` are literals, so `test_x[*]` matches the pytest id `test_x[a]`. A
selector is never empty, never padded with blanks, and never `[target]` (claim
a synthetic result with `whole: true`). Like case paths it is in Unicode NFC
(an editor that writes `é` decomposed would otherwise claim a case that never
exists), and it never ends with an `[rr:ID]` name tag, which ingest strips
from case names.

**Labels** are compared in one spelling: `@@//p:n`, `@//p:n` and
`@<config.main_repo>//p:n` are `//p:n`; `//p` is `//p:p`; a module
repository's canonical `@@name+//` (Bazel 8) or `@@name~//` (Bazel 7) is
`@name//`. Anything else (`:n`, `p:n`, `//p/...`) is a `bad-target`.

**No two entities can claim one case.** `rr validate` compares the claims of
different entities on each target: a whole claim overlaps anything, and two
selectors overlap when some case path matches both — decided exactly (the
grammar has `*` as its only wildcard), with a shortest such path as the
example:

```text
requirements.yaml:411: error: [shared-case] PR-29 and PR-13 both claim cases of //web:clocksync_test ('clocksync::*' vs 'clocksync::offset*'), e.g. 'clocksync::offset' (PR-13 claims it at requirements.yaml:233). A test case verifies at most one requirement: narrow one selector.
```

`config.variants` declares targets that run the same test code under another
configuration; claims of different entities across one group are compared the
same way (`same-code-multiple-owners`). Both are errors no configuration can
turn off. Overlapping selectors of *one* entity are only `redundant-selector`.

(config-reference)=
## The `config:` section

Everything project-specific is configured in one `config:` mapping (in any one
model document). Omitted keys keep their defaults.

| Key | Default | Meaning |
| --- | ------- | ------- |
| `prefixes` | `{user_need: UN, requirement: REQ, risk: RISK, mitigation: MIT, test_method: TM}` | Id prefix per kind (keys may also be the section names). Prefixes must be distinct. |
| `id_pattern` | `{prefix}-\d+` | Regular expression for ids; `{prefix}` is substituted literally and the whole id must match, so `'{prefix}-\d{4}'` means exactly four digits. Groups are allowed. |
| `levels` | `analysis`, `simulation`, `sil`, `hil`, `hitl`, `inspection` (unordered) | The verification ladder, lowest first. Items are names or `{name, ordered, description}`; `ordered: false` makes a level incomparable. |
| `default_level` | `simulation` | Demanded by requirements without a `method`. |
| `default_provided_level` | `simulation` | Provided by evidence without a `level`. |
| `autonomous_max_level` | `sil` | Gaps demanding at most this level route `autonomous`; above it, `human-gate`. |
| `pyramid_min_level` | `hil` | Requirements demanding at least this level must have cheap backing evidence. Empty disables the policy. |
| `pyramid_cheap_levels` | `[analysis, simulation]` | What counts as cheap backing evidence. |
| `severities` | `[negligible, low, medium, high, critical]` | Ordinal severity scale, lowest first. |
| `likelihoods` | `[rare, unlikely, possible, likely, certain]` | Ordinal likelihood scale, lowest first. |
| `acceptable_risk_score` | `null` (off) | Residual scores above this raise `risk-unacceptable`. |
| `high_severities` | `[high, critical]` | Severities hoisted into the "not mitigated" banner. |
| `rules` | see [rules](#rules) | Severity (`error`, `warning`, `off`) of each coverage rule. |
| `annotation_patterns` | `[]` | Extra regular expressions for source annotations; capture group 1 holds the id list ({doc}`annotations`). |
| `attribution` | `hybrid` | Who owns a test case no [claim](#claims) covers: `hybrid` lets a single-id tag own it; `model` leaves it unowned (tags only cross-check claims). Neither mode can give a case two owners. |
| `main_repo` | `""` | This repository's apparent name in other modules: `@<main_repo>//x:y` is read as `//x:y`. |
| `sets_lock` | `""` (none) | The verification-set lock (`verification.rrlock`), relative to the file holding `config:`. `rr validate` checks it against the claims. |
| `flaky` | `under-verify` | A pass that needed a retry: `accept`, `flag`, `under-verify` or `fail`. |
| `set_consistency` | `warn` | Members of one set stamped with different builds: `off`, `warn` or `enforce`. |
| `variants` | `[]` | Groups (lists of two or more labels) of targets that run the same test code. |

Example — a project that keeps its `PR-` ids, uses its own ladder and starts
with coverage rules as warnings:

```yaml
config:
  prefixes: {requirement: PR}
  levels:
    - analysis
    - unit
    - bench
    - field
    - {name: review, ordered: false, description: Signed design review}
  default_level: unit
  default_provided_level: unit
  autonomous_max_level: unit
  pyramid_min_level: bench
  pyramid_cheap_levels: [analysis, unit]
  acceptable_risk_score: 6
  rules:
    need-unsatisfied: warning
    requirement-orphan: warning
  annotation_patterns:
    - 'Requirements:\s*([A-Z0-9,\s-]+)'
```

The top level of a section document may also carry `schema_version`, which is
accepted and currently ignored.

(rules)=
## Validation

`rr validate` (and the `<model>_test` target that `rr_model` creates) reports
every problem at once, each with a stable code:

```console
$ rr validate requirements/
requirements/reqs.yaml:14: error: [dangling-reference] REQ-4: satisfies unknown user_need UN-9
requirements/risks.yaml:3: error: [risk-unmitigated] RISK-2: no mitigation controls this risk
```

`--format json` prints the issues as a list of `{severity, code, message,
entity, path, line}`; `--strict` promotes warnings to errors.

### Always errors

| Code | Raised when |
| ---- | ----------- |
| `shape` | A document or entity is malformed: not a mapping, a missing `id`/`title`, a section that is not a list, a duplicate id, a malformed note, a claim item without a `target`, an invalid `config:` value, an unreadable file. |
| `bad-id` | An id does not match its kind's pattern. |
| `bad-status` | `status` is not one of the allowed values. |
| `dangling-reference` | A reference names an id that does not exist. |
| `bad-reference` | A reference names an entity of the wrong kind, or a requirement refines itself. |
| `bad-method` | A requirement's `method` is neither a test method id nor a level. |
| `bad-level` | A claim's `level` or a test method's `level` is not a defined level. |
| `missing-level` | A test method has no `level`. |
| `bad-enum` | A risk's severity/likelihood (initial or residual) is off-scale, or a mitigation's `type` is unknown. |
| `inconsistent-trace` | A risk's `mitigated_by` disagrees with the mitigations' `mitigates`. |
| `refines-cycle` | Requirements refine each other in a cycle. |
| `mitigation-no-risk` | A mitigation mitigates nothing. |
| `shared-case` | Claims of two entities can select one test case (the message names a witness). |
| `same-code-multiple-owners` | Claims of two entities on targets of one `config.variants` group can select one case. |
| `bad-selector` | A claim item with both or neither of `cases` / `whole: true`, an empty `cases`, or a selector that is empty, padded, not in Unicode NFC, `[target]`, badly escaped, or ends its case name with an `[rr:ID]` name tag (ingest strips those, so it could never match). |
| `bad-target` | A claim's or `variants` entry's target is not a label or pseudo-target. |
| `unknown-target` | With `rr validate --known-targets FILE` (`bazel query 'tests(//...)'` output): a claim, a `config.variants` entry or a lock target names a label not in the file (pseudo-targets are exempt). |
| `lock-invalid` | The configured `sets_lock` is missing or malformed, maps a case to a list, has a case path that is not canonical (NFC, no surrounding blanks), or names an owner that is not a user need, requirement or mitigation. |
| `lock-owner-changed` | `attribution: model`: a lock entry is selected by another entity's claim. |

These cannot be configured: naming one under `config.rules` is itself an
error, and so is naming a report-time quarantine (`multi-tag`,
`attribution-conflict`).

### Configurable coverage rules

| Rule | Default | Raised when |
| ---- | ------- | ----------- |
| `need-unsatisfied` | error | No requirement satisfies a user need — it can never be validated. |
| `requirement-orphan` | error | A requirement satisfies no need, refines nothing and implements no mitigation. |
| `risk-unmitigated` | error | No mitigation controls a risk. |
| `mitigation-unimplemented` | error | No requirement implements a mitigation. |
| `risk-unacceptable` | warning | The residual risk score exceeds `acceptable_risk_score`. |
| `unknown-field` | error | A document, entity, note or claim item has a key the model does not know — usually a typo (`satisfes:`) that would silently drop a trace. |
| `bare-target-reference` | warning | A claim in a 0.2 form (bare label, `{target}`, `{target, level}`). |
| `whole-target-reference` | warning | A `whole: true` claim without a `reason`. |
| `glob-selector` | off | A selector with a `*` (turn on to require literal case lists). |
| `redundant-selector` | warning | Two selectors of one entity on one target overlap. |
| `parent-with-claims` | warning | A requirement refined by others also claims cases of its own. |
| `lock-stale` | error | `attribution: model`: no claim of a lock entry's owner selects it. |

The 0.3 rules `coarse-claim`, `tag-mismatch`, `unclaimed-tag`,
`suite-level-requirement`, `duplicate-case`, `level-mismatch`,
`same-path-multiple-owners` and `multi-verifies-annotation` are configured
here too; they are raised when evidence is attributed or annotations are scanned.

Projects that deliberately carry extra keys (a `jira:` link, a note's
`priority:`) set the rule to `warning` or `off`; the tools keep such keys
intact, including the web editor. Like every rule, `unknown-field` findings are issues returned by
{py:func}`rules_requirements.validate.validate` (and shown by `rr validate`,
including `--format json`); `--strict` promotes them to errors when the rule is
a warning. A key repeated within one mapping is always an error: YAML would
otherwise silently keep only the last value.

## JSON Schema and editor support

The repository ships a JSON Schema for model files at
`schema/rules_requirements.schema.json` (exported to Bazel as
`@rules_requirements//:schema/rules_requirements.schema.json`), and this site
serves it at <https://studio-fug.github.io/rules_requirements/schema/rules_requirements.schema.json>.
It checks shape — field names, types, enums — and gives completion in editors;
referential integrity and coverage are `rr validate`'s job.

With the YAML language server (VS Code's YAML extension, and most editors via
LSP), add a modeline to each model file:

```yaml
# yaml-language-server: $schema=https://studio-fug.github.io/rules_requirements/schema/rules_requirements.schema.json
```

or map the schema to your model directory in the editor settings, for example
in VS Code:

```json
{
  "yaml.schemas": {
    "https://studio-fug.github.io/rules_requirements/schema/rules_requirements.schema.json": "requirements/**/*.yaml"
  }
}
```

The schema describes the default vocabulary: if you change `severities`,
`likelihoods` or `levels`, it still accepts any string there, and
`rr validate` checks the values against your configuration.
