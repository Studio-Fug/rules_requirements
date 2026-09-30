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

## Requirements

`requirements:` entries — verifiable statements the product must meet.

| Field | Type | Notes |
| ----- | ---- | ----- |
| `rationale` | string | Why the requirement exists. |
| `category` | string | Free-form (`functional`, `safety`, `performance`, ...). |
| `satisfies` | list of UN ids | The needs this requirement helps meet. |
| `refines` | list of REQ ids | Parent requirements this one decomposes. Cycles are errors. |
| `method` | TM id or level | The verification rigor demanded. Default: `config.default_level`. |
| `verified_by` | list | Build targets whose whole pass/fail is evidence: a label string, or `{target: <label>, level: <level>}`. A bare string provides `config.default_provided_level`. |
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

## Test methods

`test_methods:` entries — named verification procedures.

| Field | Type | Notes |
| ----- | ---- | ----- |
| `level` | level, **required** | The rigor this method provides and demands. |
| `procedure` | string | How the method is carried out. |

(config-reference)=
## The `config:` section

Everything project-specific is configured in one `config:` mapping (in any one
model document). Omitted keys keep their defaults.

| Key | Default | Meaning |
| --- | ------- | ------- |
| `prefixes` | `{user_need: UN, requirement: REQ, risk: RISK, mitigation: MIT, test_method: TM}` | Id prefix per kind (keys may also be the section names). Prefixes must be distinct. |
| `id_pattern` | `{prefix}-\d+` | Regular expression for ids; `{prefix}` is substituted and the whole id must match. The substitution uses Python's `str.format`, so literal braces are doubled: `'{prefix}-\d{{4}}'` for exactly four digits. |
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
| `shape` | A document or entity is malformed: not a mapping, a missing `id`/`title`, a section that is not a list, a duplicate id, a malformed note or `verified_by` item, an invalid `config:` value, an unreadable file. |
| `bad-id` | An id does not match its kind's pattern. |
| `bad-status` | `status` is not one of the allowed values. |
| `dangling-reference` | A reference names an id that does not exist. |
| `bad-reference` | A reference names an entity of the wrong kind, or a requirement refines itself. |
| `bad-method` | A requirement's `method` is neither a test method id nor a level. |
| `bad-level` | A `verified_by` level or a test method's `level` is not a defined level. |
| `missing-level` | A test method has no `level`. |
| `bad-enum` | A risk's severity/likelihood (initial or residual) is off-scale, or a mitigation's `type` is unknown. |
| `inconsistent-trace` | A risk's `mitigated_by` disagrees with the mitigations' `mitigates`. |
| `refines-cycle` | Requirements refine each other in a cycle. |
| `mitigation-no-risk` | A mitigation mitigates nothing. |

### Configurable coverage rules

| Rule | Default | Raised when |
| ---- | ------- | ----------- |
| `need-unsatisfied` | error | No requirement satisfies a user need — it can never be validated. |
| `requirement-orphan` | error | A requirement satisfies no need, refines nothing and implements no mitigation. |
| `risk-unmitigated` | error | No mitigation controls a risk. |
| `mitigation-unimplemented` | error | No requirement implements a mitigation. |
| `risk-unacceptable` | warning | The residual risk score exceeds `acceptable_risk_score`. |
| `unknown-field` | error | A document or entity has a key the model does not know — usually a typo (`satisfes:`) that would silently drop a trace. |

When `unknown-field` is set to `warning`, the findings are printed by the CLI
as `warning: [unknown-field] ...` lines rather than returned by
{py:func}`rules_requirements.validate.validate`.

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
