# Reports

One traceability matrix, four renderings: **HTML** to read, **Markdown** for pull
requests and wikis, **JSON** as the canonical, diffable record, and a **gap
queue** for whoever — or whatever — closes the gaps.

## Producing a report

With the CLI:

```console
$ rr report --model requirements/ --evidence bazel-testlogs \
    --html report.html --json report.json --md report.md \
    --queue-out gaps.json --title "Thermostat V&V"
```

`--out FILE` (repeatable) picks the format from the extension (`.html`, `.htm`,
`.json`, `.md`, `.markdown`); `-` writes to standard output. `--scan` adds the
source annotations of the workspace (see {doc}`annotations`), and
`--current-build KEY=VALUE` (repeatable) enables the
[staleness](../concepts.md#staleness) check.

In Bazel, `rr_report` builds `<name>.html`, `<name>.json` and `<name>.md` as
ordinary outputs ({doc}`bazel`).

The command prints a one-line summary to standard error and exits with:

| Exit | When |
| ---- | ---- |
| `0` | The report was written and no `--fail-on` / `--pyramid-policy error` condition holds, no attribution issue is an error, and no test case is quarantined (or `--on-attribution-error=warn`). |
| `1` | An attribution issue is an error (a hard error such as `lock-owner-changed`, a rule set to `error` under `config.rules`, or — with `--strict` — **any attribution warning**, e.g. `coarse-claim` or `same-path-multiple-owners`); `--fail-on failed` and an entity is FAILED or INVALID; `--fail-on unverified` and, in addition, a requirement is UNVERIFIED or INCOMPLETE; `--fail-on gaps` and there is any gap; or `--pyramid-policy error` and there is a cost-pyramid violation. |
| `2` | The model is invalid, or an `--out` extension is unknown. |
| `3` | A test case is quarantined (unless `--on-attribution-error=warn`). The reports and the queue are written first; `3` wins over `1`. |

`--fail-on` defaults to `none`: the report describes the state of the product,
and whether that state should fail a pipeline is a separate decision.

Each quarantined test case — one whose evidence names several ids, that two
entities claim, or whose test code two entities own — prints one
`ATTRIBUTION ERROR: <code>: <detail>` line to standard error, every entity it
names reads INVALID ({ref}`evidence`), and `rr report` exits 3.
`--on-attribution-error=warn` keeps the exit status; it never changes a
verdict (the case still counts for nobody).

Every other attribution issue (`same-path-multiple-owners`, `unscoped-evidence`,
`level-mismatch`, `suite-level-requirement`, the lock findings, ...) is a gap
in the report. One at error severity also prints an
`ATTRIBUTION ERROR: [<code>] <message>` line and makes `rr report` exit 1;
the warnings are counted on one `attribution: N warning(s) (...)` line.
`--strict` escalates every attribution warning (not only the model's) to an
error.

`--sets-lock PATH` reads that verification-set lock instead of
`config.sets_lock`; `--no-lock` reads none (the sets are then not pinned: an
`unpinned-sets` gap).

### The gates at a glance

Exit 3 is new in 0.3; `0`, `1` and `2` mean what they meant in 0.2. The
commands that enforce one owner per test case
({doc}`../one-test-case-one-requirement`), and when each one fails:

| Command | Fails with | When | In Bazel |
| ------- | ---------- | ---- | -------- |
| `rr validate` | `1` | a model error: `shared-case`, `same-code-multiple-owners`, a bad selector or target, a lock that is invalid or disagrees with the claims | `rr_model`'s `<name>_test` |
| `rr report` | `2` / `3` / `1` | an invalid model / a quarantined case / an error-level attribution issue or a `--fail-on` condition | `rr_report` (the build fails) |
| `rr check-report` | `1` (`2`: not a v2 report) | the published JSON breaks the partition or its counts | `rr_report`'s `<name>_check_test` |
| `rr sets check` | `1` | the lock and the evidence disagree: a missing case, an unlocked member, an owner change, a stale entry | `rr_sets_lock_test` |
| `rr attribution --check` | `1` | a quarantine, a missing case, lock drift or an error-level issue | — |

### Lanes

A requirement's set may span lanes — software tests in one pipeline, HITL
tests in another. `--lane NAME` stamps the report with its lane, and
`--lane-targets FILE` (labels, one per line, e.g. `bazel query
'tests(//...)'`) lists the targets that lane runs. A not-run member of any
other target is labelled `lane_hint: "out of lane"` (expected elsewhere), and
the `unverified` / `incomplete` gaps such members alone cause stay out of
`--queue-out` (they are another lane's work; the report still lists them).
Not-run members of in-lane targets stay ordinary `not-run` gaps. **Verdicts
are identical with or without these flags**: a set spanning both lanes reads
INCOMPLETE in each lane's report, and only the combined report, over both
lanes' evidence, can read VERIFIED.

### Checking a published report

`rr check-report report.json` re-proves the one-owner partition from the JSON
alone, independently of the code that wrote it. It exits `1` if a case key is
an owned member (`owned: true`) of two entities — whether or not `cases`
lists it —, if an owned member is no row of `cases` owned by its entity, if
an owner is not a scalar id, if a quarantined case is owned, if the entities
holding a quarantined case are not exactly the ones its quarantine names (a
list of ids that follows from its code), if one of them does not read
INVALID, if an entity's `evidence` is not exactly the view of its owned
members, if the counts (summary, sets, per-target counts, granularity)
disagree with the rows they count, or if the file names a key twice in one
object (a duplicate `owner` reads differently to different parsers); `2` if
the file is not a `rules_requirements/report/v2` report. An `error` member
that is not owned is a pseudo-member: it names no case of the report, on a
tainted or synthetic-only target. A `missing` or `not-run` member never names
a case of the report either (only a `moved` or `quarantined` member may), so
no case sits in two verification sets. In Bazel, `rr_report` adds it as
`<name>_check_test` whenever it builds the JSON report.

## JSON (`rules_requirements/report/v2`)

The JSON report is deterministic — entities sorted by id (`REQ-2` before
`REQ-10`), no timestamps, no machine-specific paths — so it can be checked in as
a golden file and reviewed as a diff. Its JSON Schema is
`schema/report.v2.schema.json`. Top-level keys:

| Key | Content |
| --- | ------- |
| `schema` | `"rules_requirements/report/v2"` (`v1` before 0.3) |
| `title` | `--title`, else the project's `name`, else `"Requirements traceability"` |
| `project` | The model's `project:` metadata |
| `summary` | Counts: `user_needs`, `user_needs_validated`, `requirements`, `requirements_verified`, `requirements_under_verified`, `requirements_partial`, `requirements_failed`, `requirements_unverified`, `requirements_incomplete`, `requirements_invalid`, `risks`, `risks_mitigated`, `mitigations`, `mitigations_verified`, `test_cases` (one per case key: retries, runs, shards and evidence roots merged, target-scope results not counted), `test_cases_owned`, `test_cases_unowned`, `test_cases_quarantined`, `gaps` |
| `attribution` | `mode` (`hybrid` / `model`), `lock` (its path, or `null`), `lane` (or `null`), `targets` (per target: `cases`, `owned`, `quarantined`, `owners`, `synthetic`, `ran`; `tainted` and `in_lane` when they apply), `quarantined` (each `{case, code, entities, declared, claims: [{entity, selector, location}], detail}`), `issues` (`{code, severity, message, case, entities, declared, target}`), `granularity` (`owned_by_literal`, `owned_by_pattern`, `owned_by_whole`, `owned_by_tag`, `coarse_claims`) |
| `cases` | The inverse matrix: every case key with **one owner or `null`** — `{case, target, path, owner, via, status, level, declared, quarantine, file, synthetic, flaky, duplicate}` (the last five when they apply). The input of `rr check-report`. |
| `levels` | The configured levels: `{name, rank}` (`rank` is `null` for unordered levels) |
| `user_needs`, `requirements`, `mitigations`, `risks`, `test_methods` | One object per entity (below) |
| `modules` | `{module: status}` rollup |
| `high_open_risks` | Ids of high-severity risks that are not MITIGATED |
| `pyramid_violations` | Ids of requirements violating the cost pyramid |
| `unknown_evidence` | `{id: [case key, ...]}` for evidence naming undefined ids; each case as its key `<target>#<path>` (0.2 wrote `<target> <classname>::<name>`) |
| `gaps` | The gap queue (below); a gap only out-of-lane members cause carries `lane_hint: "out of lane"` |

Every entity object has `id`, `title`, `status` and `basis` (`own`, `derived`
or `own+derived`: whether its verdict rests on its own set, on other
entities' verdicts — listed in `derived_from` — or both), plus `description`,
`open_notes` (`[{kind, text}]`) and `evidence` when present. User needs,
requirements and mitigations also carry their {ref}`verification set
<evidence>`: `set` counts its members (`complete`, `members`, `passed`,
`failed`, `error`, `skipped`, `missing`, `not_run`, `moved`, `quarantined`)
and `members` lists them — `{case, target, selector, via, state, owned}` plus
`level`, `stale`, `flaky`, `reason` and `lane_hint` when they apply (`case` is
`null` for a glob or whole claim that matched nothing). `owned` says whether
the entity owns the case (it counts as its evidence): an owned member is a row
of `cases` owned by that entity, and no case key is an owned member of two
entities. The others are pseudo-members (`missing`, `not-run`, `moved`,
`quarantined`, and an `error` for a case a tainted target did not report).

`evidence` is the 0.2 view of the owned members, kept for 0.3.x readers: each
entry is `{name, status, level}` — the case path, its member state, its level —
with, when applicable, `target` (the build label or pseudo-target),
`stale: true`, and `message` (the first line, at most 300 characters, of a
failure). Since 0.3 a whole-target claim lists the target's cases, so
`kind: "target"` entries no longer occur, and a quarantined case is listed for
no entity.

| Section | Additional keys |
| ------- | --------------- |
| `user_needs` | `requirements` — ids of the requirements that satisfy it |
| `requirements` | `demanded_level`, `provided_level` (best passing level or `null`), `satisfies`, `refines`, `implements` (mitigation ids); `method`, `modules`, `stale`, `pyramid_violation` when set; `implemented_in` and `verified_in` when sources were scanned |
| `mitigations` | `type` (or `null`), `mitigates`, `implemented_by`; `implemented_in` / `verified_in` when scanned |
| `risks` | `severity`, `likelihood`, `residual_severity`, `residual_likelihood` (each or `null`), `mitigations`; `hazard`, `hazardous_situation`, `harm`, `residual` when set; `score`, and `residual_score` when a residual estimate exists |
| `test_methods` | `level`, `used_by` — ids of the requirements that demand it |

`implemented_in` / `verified_in` entries are the annotations:
`{ids, relation, path, line}` plus `text` and `symbol` when known.

## The gap queue

`--queue-out FILE` writes the [gaps](../concepts.md#gaps-and-routing) as
`{"queue": [...]}`; each gap is

```json
{
  "kind": "under-verified",
  "entity": "REQ-5",
  "message": "demands hil, best passing evidence is simulation",
  "route": "human-gate",
  "demanded_level": "hil",
  "provided_level": "simulation"
}
```

`demanded_level` and `provided_level` are present when they apply. The queue is
meant to drive work: `autonomous` items are ones an agent can close by writing
the missing analysis, simulation or software-in-the-loop test; `human-gate`
items need a bench, a device or a person, and must not be closed by synthesised
evidence.

## Markdown

A compact rendering for pull-request comments and wikis: the summary line
(with the owned, unowned and quarantined case counts), the attribution mode,
lock and lane, a red banner listing every quarantined case with its code and
every claim's origin, a banner for high-severity risks that are not mitigated,
then tables for user needs, requirements (each row's evidence cell starts with
its set line, e.g. `set 17/23 passed · 6 not run (out of lane)`), risks,
mitigations, test methods, source implementation links (when sources were
scanned) and modules; then "Verification sets" (one member table per entity:
case, state, level, via, selector, lane hint), "Case attribution" (per target:
cases, owned, quarantined, unowned, owners — and the unowned cases, the
granularity backlog), "Lock drift" and "Coarse claims" when they have entries,
and the gaps. The {doc}`tutorial <../tutorial>` shows the complete Markdown
report of the example project.

## HTML

A single self-contained page (no external assets; light and dark themes) with
progress tiles, a quarantine banner (each case, its code and every claim's
origin), alerts for unmitigated high-severity risks, cost-pyramid violations
and evidence that names undefined ids, the trace graph, and a table per entity
kind. Requirement rows show their traces, their set line — expanding to the
member table — evidence (with levels, staleness and failure messages),
demanded and best-provided level, the basis of a derived verdict, source links
and open notes; the "Case attribution", "Lock drift" and "Coarse claims"
sections follow. Every id is an anchor, so `report.html#REQ-5` links straight
to a row.

## Graph exports

`rr graph` exports the trace graph on its own:

```console
$ rr graph --model requirements/ --format mermaid          # to stdout
$ rr graph --model requirements/ --format svg --evidence bazel-testlogs --out trace.svg
$ rr graph --model requirements/ --format dot --methods | dot -Tpng -o trace.png
$ rr graph --model requirements/ --format svg --evidence bazel-testlogs --cases --out cases.svg
```

| Format | Content |
| ------ | ------- |
| `dot` | Graphviz; needs are ellipses, requirements boxes, mitigations hexagons, risks diamonds, test methods notes. |
| `mermaid` | A Mermaid `flowchart LR`, for Markdown that renders Mermaid (GitHub does). |
| `svg` | A self-contained layered drawing: needs, requirements, mitigations and risks in columns, ordered to reduce crossings. |
| `json` | `{nodes: [{id, kind, title, status}], edges: [{source, target, relation}]}` |

Edges are `satisfies`, `refines` and `method` (dashed or dotted), `mitigates`
and `implemented_by`. `--methods` adds test methods and `method` edges (the SVG
layout draws only the four main columns). With `--evidence`, nodes are coloured
by status. `--cases` (which needs `--evidence`) adds a node for every owned test
case, coloured by its result, with exactly one `verifies` edge into it: from the
one entity attribution gave it to. A test case verifies at most one requirement,
so no case node has two; unowned and quarantined cases are left out. The same graph, coloured from the example's golden report:

```{raw} html
<div class="rr-graph-wrap">
```

```{raw} html
:file: ../_generated/thermostat-graph.svg
```

```{raw} html
</div>
```
