# Web editor

`rr serve` starts a local web application for working on the model: authoring
and editing user needs, requirements, risks, mitigations and test methods;
tracing them through a live graph; browsing from any entity to the code and
tests annotated with it; comparing the model across branches, tags and commits;
and running agentic reviews whose findings become notes or new entities.

```sh
rr serve --model requirements/ --evidence bazel-testlogs --author "Ada Lovelace <ada@example.com>"
# or, in a Bazel workspace:
bazel run @rules_requirements//python:rr -- serve --model requirements --evidence "$(bazel info bazel-testlogs)"
```

Open the printed URL (`http://localhost:8080/` by default). Everything the
editor changes is written to your checkout as ordinary YAML edits, so the usual
review loop — diff, commit, pull request — stays the source of truth.

```{image} ../_static/editor-entity.png
:alt: A risk in the web editor, with its trace chain, hazard analysis and notes
```

## What you can do

**Author and edit.** Every entity kind has a list view (status, open notes,
validation issues, file and line) and a form-based editor with pickers for the
references (`satisfies`, `refines`, `mitigates`, `implemented_by`, `method`).
New entities get the next free id in your project's format and are written next
to their siblings: appended to the section file that holds most of that kind, or
as a new one-object file when the model uses that layout. Ids can be renamed —
every reference in the model follows — and deleting an entity that is still
referenced asks first (and can remove the references).

Edits are **surgical**: only the changed fields of the changed entity are
rewritten. Comments, section banners, field order, line endings, file mode and
the formatting of every untouched field stay as they were, so a change made in
the editor produces the same small diff a person would write by hand.

Every write is also **verified** before it happens: the edited files are
re-read with the model loader and must differ from the originals exactly as
intended — the edited entities as requested, every other entity, the
configuration and the project metadata unchanged, no new errors. Otherwise
nothing is written and the editor explains why. Layouts that cannot be edited
in place without touching neighbours — flow-style sections
(`requirements: [{...}, {...}]`) and JSON model files — are refused the same
way; edit those by hand or convert them to block style. An entity carries a
version, so saving a form over a change someone made in the meantime is a
conflict, not a silent overwrite.

```{image} ../_static/editor-graph.png
:alt: The trace graph: needs, requirements, mitigations and risks coloured by status
```

**Trace.** Each entity page shows its traces in both directions with their
verification status — for a risk, the whole chain from user needs through
requirements and mitigations — its test evidence (name, target, level, result,
failure message), and the source locations annotated with it. The trace graph
(needs → requirements → mitigations → risks, laid out to keep crossings low)
pans and zooms, focuses on an entity's neighbourhood at a chosen depth, filters
by kind, and exports Mermaid.

**Browse the implementation.** The implementation view lists every `@rr(...)`
annotation found in the tree (see {doc}`annotations`) — implementing and
verifying sites, their symbols and descriptions, unknown ids flagged — and
opens any of them in a code viewer at the annotated line.

**Version.** The versions view shows the uncommitted model changes, commits
them (with the author you set in the editor, as the git author), names
baselines as annotated tags, lists recent commits touching the model, and shows
a **semantic diff** between any two refs — branches, tags, commits, or the
working tree: which entities were added, removed or modified, field by field
(`rr diff OLD [NEW]` prints the same on the command line).

**Notes.** Any entity can carry notes — comments, questions, TODOs and gaps —
with author, date and an open/resolved status. They are stored in the model
file (`notes:`), and open gaps, TODOs and questions join the work queue
({ref}`gaps`) as `note:*` gaps, so a finding recorded today drives the
next implementation cycle for a person or an agent. Questions route to a human.

**Work queue.** Everything between the model and a complete V&V argument, as the
report's gap list: kind, entity, route (`autonomous` or `human-gate`) and
detail, with filters and a JSON export to hand to an agent.

## Agentic workflows

The agents view runs workflows over the model, its evidence and the source
tree. Each produces **findings** — severity, category, the entity concerned,
detail, and optionally a *proposal* (a new need, requirement, risk or
mitigation, or an update to an existing one). A finding can be:

- **added as a note** on its entity (a gap, TODO or question — for a finding
  about the model as a whole you pick the entity), so it enters the work queue;
- **turned into the proposed object** — the editor opens pre-filled so you can
  adjust it before saving — or applied as an update;
- **dismissed**.

| Workflow | Needs an LLM | Question it answers |
| -------- | ------------ | ------------------- |
| Completeness check | no (an optional LLM pass adds more) | Are there validation issues or traceability gaps — needs without requirements, unverified / under-verified / stale / failing requirements, uncontrolled risks, requirements without implementation links? With an LLM: do the requirements cover each need, are they verifiable as written, do they imply hazards the analysis lacks? |
| Test adequacy | yes | Do the tests linked to requirement *Y* — by evidence or annotation — actually assert what *Y* states? Each test is judged *proves / partially / does not prove / cannot tell*, and missing checks are listed. |
| Implementation review | yes | Does the code annotated as implementing *Y* implement it completely? |
| Mitigation adequacy | yes | Do the requirements behind risk *W*'s mitigations actually control it (ISO 14971 §7), and is the residual estimate plausible? |
| Hazard discovery | yes | Which hazards and hazardous situations does the risk analysis not cover yet? |
| Assistant | yes | Describe a change in plain language; get proposed creates, updates and notes to review and apply. |

Entity pages offer the relevant workflow for that entity directly (test adequacy
and implementation review for a requirement, mitigation adequacy for a risk).
Agents only ever *propose*: nothing is written until you apply a finding.

### Enabling the LLM workflows

The LLM-backed workflows use Claude through the official `anthropic` SDK, an
optional dependency:

```sh
pip install "rules-requirements[agents]"
export ANTHROPIC_API_KEY=...        # or `ant auth login`
rr serve --model requirements/
```

Requests use `claude-opus-5-5` with adaptive thinking and structured (JSON
schema) output, streamed; a safety-classifier decline falls back server-side to
the model Anthropic recommends for that case (`fallbacks: "default"`). Choose
another model or effort with `--agent-model` / `--agent-effort` (or
`RR_AGENT_MODEL` / `RR_AGENT_EFFORT`); `--no-llm` disables the LLM workflows.
Without the SDK the deterministic completeness check still runs and the other
workflows show how to enable them.

The prompts contain the model (ids, titles, descriptions, traces and statuses)
and, for the adequacy and implementation reviews, excerpts of the linked test
and source files — keep that in mind for confidential code.

## Security

The server edits files in your checkout and runs `git`, so it is built for
local use:

- it binds to `127.0.0.1` by default (`--host` to change — any address other
  than loopback requires a token, and `rr serve` generates one if you give
  none, because the Host check below is no protection against clients that can
  reach the port directly);
- requests whose `Host` header is not a local name are refused, which defeats
  DNS-rebinding attacks from web pages (`--allow-host NAME` adds a name, e.g.
  behind a reverse proxy);
- every mutating request must carry an `X-RR-Request: 1` header, which a
  cross-site form or image cannot send (and no CORS access is ever granted);
- `--token SECRET` additionally requires `Authorization: Bearer SECRET` on the
  API; open the page as `http://localhost:8080/#token=SECRET` once and the UI
  keeps it for the session;
- the code viewer only serves text files inside the repository (not `.git`),
  and the UI is served with a strict Content-Security-Policy.

## API

The UI is a client of a small JSON API under `/api/`, usable from scripts and
agents too (send `X-RR-Request: 1` on `POST`/`PUT`/`PATCH`/`DELETE`, and
`X-RR-Author: Name <email>` to attribute edits):

| Method and path | Purpose |
| --------------- | ------- |
| `GET /api/state` | project, configuration, counts, validation issues, git status, LLM availability |
| `GET /api/entities?kind=` · `GET /api/entities/{id}` | lists; one entity with verdict, evidence, traces, source references, issues and gaps |
| `POST /api/entities` · `PUT`/`DELETE /api/entities/{id}` · `POST /api/entities/{id}/rename` | create, update, delete (`?force=1` also removes references), rename |
| `POST /api/entities/{id}/notes` · `PATCH`/`DELETE …/notes/{note}` | notes |
| `GET /api/next-id?kind=` | the next id and the file a new entity would go to |
| `GET /api/graph?focus=&depth=&kinds=&methods=` | trace graph (nodes, edges, SVG, Mermaid) |
| `GET /api/report` · `GET /api/queue` · `POST /api/reload` | report JSON, gap queue, re-read evidence and sources |
| `GET /api/annotations` · `GET /api/source?path=` | source annotations; a file's lines |
| `GET /api/git/status` · `GET /api/git/refs` · `GET /api/git/log` | git state |
| `GET /api/diff?from=&to=` | semantic model diff (`to` defaults to the working tree) |
| `POST /api/git/commit` · `POST /api/git/tag` | commit model changes; name a baseline |
| `GET /api/agents` · `POST /api/agents/run` · `GET /api/agents/jobs[/{job}]` | workflows and jobs |
| `POST /api/findings/{id}/apply` · `POST /api/findings/{id}/dismiss` | act on a finding |
