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
| `0` | The report was written and no `--fail-on` / `--pyramid-policy error` condition holds, and no attribution issue is an error. |
| `1` | An attribution issue is an error (a hard error such as `lock-owner-changed`, a rule set to `error` under `config.rules`, or any attribution warning under `--strict`); `--fail-on failed` and an entity is FAILED or INVALID; `--fail-on unverified` and, in addition, a requirement is UNVERIFIED or INCOMPLETE; `--fail-on gaps` and there is any gap; or `--pyramid-policy error` and there is a cost-pyramid violation. |
| `2` | The model is invalid, or an `--out` extension is unknown. |

`--fail-on` defaults to `none`: the report describes the state of the product,
and whether that state should fail a pipeline is a separate decision.

Each quarantined test case — one whose evidence names several ids, that two
entities claim, or whose test code two entities own — prints one
`ATTRIBUTION ERROR: <code>: <detail>` line to standard error, and every entity
it names reads INVALID ({ref}`evidence`).

Every other attribution issue (`same-path-multiple-owners`, `unscoped-evidence`,
`level-mismatch`, `suite-level-requirement`, the lock findings, ...) is a gap
in the report. One at error severity also prints an
`ATTRIBUTION ERROR: [<code>] <message>` line and makes `rr report` exit 1;
the warnings are counted on one `attribution: N warning(s) (...)` line.

## JSON (`rules_requirements/report/v1`)

The JSON report is deterministic — entities sorted by id (`REQ-2` before
`REQ-10`), no timestamps, no machine-specific paths — so it can be checked in as
a golden file and reviewed as a diff. Top-level keys:

| Key | Content |
| --- | ------- |
| `schema` | `"rules_requirements/report/v1"` |
| `title` | `--title`, else the project's `name`, else `"Requirements traceability"` |
| `project` | The model's `project:` metadata |
| `summary` | Counts: `user_needs`, `user_needs_validated`, `requirements`, `requirements_verified`, `requirements_under_verified`, `requirements_partial`, `requirements_failed`, `requirements_unverified`, `requirements_incomplete`, `requirements_invalid`, `risks`, `risks_mitigated`, `mitigations`, `mitigations_verified`, `test_cases` (one per case key: retries, runs, shards and evidence roots merged, target-scope results not counted), `gaps` |
| `levels` | The configured levels: `{name, rank}` (`rank` is `null` for unordered levels) |
| `user_needs`, `requirements`, `mitigations`, `risks`, `test_methods` | One object per entity (below) |
| `modules` | `{module: status}` rollup |
| `high_open_risks` | Ids of high-severity risks that are not MITIGATED |
| `pyramid_violations` | Ids of requirements violating the cost pyramid |
| `unknown_evidence` | `{id: [test case, ...]}` for evidence naming undefined ids |
| `gaps` | The gap queue (below) |

Every entity object has `id`, `title` and `status`, plus `description`,
`open_notes` (`[{kind, text}]`) and `evidence` when present. `evidence` lists the
cases the entity owns (the owned members of its
{ref}`verification set <evidence>`): each entry is
`{name, status, level}` — the case path, its member state, its level — with,
when applicable, `target` (the build label or pseudo-target), `stale: true`,
and `message` (the first line, at most 300 characters, of a failure). Since 0.3
a whole-target claim lists the target's cases, so `kind: "target"` entries no
longer occur, and a quarantined case is listed for no entity.

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

A compact rendering for pull-request comments and wikis: the summary line, a
banner for high-severity risks that are not mitigated, then tables for user
needs, requirements (with their evidence and demanded level), risks,
mitigations, test methods, source implementation links (when sources were
scanned), modules and gaps. The {doc}`tutorial <../tutorial>` shows the complete
Markdown report of the example project.

## HTML

A single self-contained page (no external assets; light and dark themes) with
progress tiles, alerts for unmitigated high-severity risks, cost-pyramid
violations and evidence that names undefined ids, the trace graph, and a table
per entity kind. Requirement rows show their traces, evidence (with levels,
staleness and failure messages), demanded and best-provided level, source
links and open notes; every id is an anchor, so `report.html#REQ-5` links
straight to a row.

## Graph exports

`rr graph` exports the trace graph on its own:

```console
$ rr graph --model requirements/ --format mermaid          # to stdout
$ rr graph --model requirements/ --format svg --evidence bazel-testlogs --out trace.svg
$ rr graph --model requirements/ --format dot --methods | dot -Tpng -o trace.png
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
by status. The same graph, coloured from the example's golden report:

```{raw} html
<div class="rr-graph-wrap">
```

```{raw} html
:file: ../_generated/thermostat-graph.svg
```

```{raw} html
</div>
```
