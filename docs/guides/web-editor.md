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
rewritten. Comments, section banners, field order, line endings (per line, in
files that mix them), file mode, keys the model does not define and the
formatting of every untouched field stay as they were, so a change made in the
editor produces the same small diff a person would write by hand.

Every write is also **verified** before it happens: the edited files are
re-read with the model loader and must differ from the originals exactly as
intended — the edited entities as requested (and of the requested kind), every
other entity, the configuration and the project metadata unchanged, no new
errors, no key the model does not define lost, and no comment gone except from
a field the edit changes or an entity it deletes. Otherwise nothing is written
and the editor explains why. A change spanning several files (a rename, a
delete that removes references) is written all-or-nothing, and deleting the
only entity of a one-object file removes the file unless other documents
(say, `project:`) live in it.

**One owner per test case.** A test case verifies at most one requirement
(user need or mitigation); a set of cases may together verify one. Before any
save — a form, a rename, a note, a finding applied — the editor builds the
model the save would write and runs the same checks as `rr validate` and
`rr report` over it: the `shared-case` and `same-code-multiple-owners`
witnesses of the claims (the very pairs `rr validate` reports, also before the
targets have run), the edited entity's selectors and targets (`bad-selector`,
`bad-target`) and whole-target claims (`whole-target-reference`: one needs a
reason), the verification-set lock (`lock-owner-changed`, and `lock-invalid`
for an entry whose owner the save would remove), and attribution over the
loaded evidence (a new `attribution-conflict`, or one source file owned twice).
A save that would introduce any of them is refused with **409**, naming the
case and its owner today:

```text
this would make //web:clocksync_test#clocksync::bestSample keeps the min-RTT sample
verify both PR-13 and PR-29 (it is owned by PR-13 now); a test case verifies at
most one requirement
```

Only problems the edit introduces count, so a conflict already in the model
never blocks an unrelated edit (it is still a validation error). None of these
checks can be configured off. Renaming an entity renames its lock entries in
the same write; deleting one drops them (the response lists the cases).

Saves are checked and written under an advisory lock on the checkout, after
making sure no file the save read changed on disk meanwhile, so two editors on
one checkout (two `rr serve` processes) cannot both pass the checks with edits
that together give a case two owners: the second is refused with a 409 and
reloads. In hybrid mode a claim may take a case its own tag gives to another
entity (the model wins); the pre-check and the save say so (`takes-from-tag`)
instead of changing the owner silently.

The form's **verification set** widget (validation set, for a user need) edits
`verified_by` / `validated_by` one target per row: *Cases* with one selector
per line and a checklist of the target's observed cases — a case another
entity owns is disabled and labelled with its owner — or *Whole target*, with
the reason the target cannot be claimed per case (required: Save stays disabled
without one). While you type, the draft is
pre-checked (`POST /api/entities/{id}/precheck`): problems show above the Save
button and disable it, each selector shows how many cases it matches, and the
set the entity would get is summarised. An item you do not touch is written
back exactly as it was.

**Case ledger.** `#/cases` lists every test case of the loaded evidence with its
one owner (or none), how it got it (`model`, or `tag` in hybrid mode), its
result, the entities whose claims select it, its lock entry, and its
quarantine — filterable by owned, unowned, quarantined, (with a lock) not
locked, coarse (selected by a whole-target claim of a target that reports
per-case results) and, with `rr serve --lane-targets NAME=FILE` (repeatable;
one label per line, as `rr report --lane-targets` reads it), by lane. A
quarantined case shows no owner: a `multi-tag` case lists the ids its tags
declare, never as owners. Owners are read from the attribution, never derived
from tags or targets in the browser. **Move…** (or **Assign…**) gives a case to another
entity, or to none, only through a model edit: the claims that select it give
it up (a literal selector is dropped; a glob or whole-target claim is rewritten
into literal selectors of the other cases it selects, after you confirm), the
new owner gains a literal selector, a configured lock entry is re-locked in the
same write, and the result must pass the checks above and give the case to the
chosen owner. A `multi-tag` case cannot be moved: its own evidence names two
ids, so fix the test's tag.

**The verification-set lock.** With `config.sets_lock`, an entity page shows a
banner when the lock is out of date for its set (entries `rr sets lock` would
add, re-own or remove over the loaded evidence), and the ledger's
**Update lock…** runs the same logic (`POST /api/lock/update`): a dry run
first, a quarantine refuses it, and an entry to remove is kept unless you
confirm its removal. The lock only records owners attribution decided; it never
decides one. The overview shows the invariant —
"N test cases · 0 quarantined · each case → ≤1 requirement" — and a banner
while any case is quarantined.

Some layouts cannot be edited in place without touching their neighbours:
flow-style sections (`requirements: [{...}, {...}]`), JSON model files, and
entities written as a flow mapping or with merge keys (`<<: *base`) that carry
comments or custom keys. The editor refuses those the same way; edit them by
hand or convert them to block style. An entity carries a version, so saving a
form over a change someone made in the meantime is a conflict, not a silent
overwrite.

```{image} ../_static/editor-graph.png
:alt: The trace graph: needs, requirements, mitigations and risks coloured by status
```

**Trace.** Each entity page shows its traces in both directions with their
verification status — for a risk, the whole chain from user needs through
requirements and mitigations — its **verification set** (every case it owns or
expects, with its state: passed, failed, error, skipped, missing, not run,
moved or quarantined, flaky and stale flags, the selector and level), the
quarantined cases that make it INVALID, and the source locations annotated
with it. The trace graph
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
| Test adequacy | yes | Do the cases of requirement *Y*'s verification set (and test code annotated with *Y*, labelled as not in the set) actually assert what *Y* states? Each test is judged *proves / partially / does not prove / cannot tell*, and missing checks are listed. |
| Implementation review | yes | Does the code annotated as implementing *Y* implement it completely? |
| Mitigation adequacy | yes | Do the requirements behind risk *W*'s mitigations actually control it (ISO 14971 §7), and is the residual estimate plausible? |
| Hazard discovery | yes | Which hazards and hazardous situations does the risk analysis not cover yet? |
| Assistant | yes | Describe a change in plain language; get proposed creates, updates and notes to review and apply. |
| Assign test cases | yes | Which one entity does each unowned or quarantined case verify (or none)? With `worksheet`, the proposals are written into that attribution worksheet (`.rrplan`). |

Entity pages offer the relevant workflow for that entity directly (test adequacy
and implementation review for a requirement, mitigation adequacy for a risk).
Agents only ever *propose*: nothing is written until you apply a finding.

Agents read a requirement's tests from its verification set, never from the
tags in the evidence: a case owned by another entity, or quarantined, is no
test of it. They **never edit ownership**: a proposal never carries
`verified_by` / `validated_by`, applying a finding cannot change them (409),
and a proposed case owner is only recorded in the attribution worksheet — as
`proposed`, `reason` and `proposed_by` of its case, never as the decided
`owner:` — for a person to decide (see `rr migrate`).

### Enabling the LLM workflows

The LLM-backed workflows use Claude through the official `anthropic` SDK, an
optional dependency:

```sh
pip install anthropic       # or: pip install "rules-requirements[agents] @ git+https://github.com/Studio-Fug/rules_requirements"
export ANTHROPIC_API_KEY=...        # or `ant auth login`
rr serve --model requirements/
```

Under Bazel, give the {doc}`rr_editor <bazel>` target your pip hub's
`anthropic` package (`deps = ["@pypi//anthropic"]`).

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

## Where the editor fits in the standards

The editor is a working surface for the records the standards ask for
({doc}`../standards`); the model files under version control remain the record.

| Editor feature | Activity it supports |
| -------------- | -------------------- |
| Authoring needs, requirements, risks and mitigations | Design inputs and software requirements analysis (ISO 13485 §7.3.3, IEC 62304 §5.2, IEC 60601-1 §14.7); risk analysis and risk control records (ISO 14971 §5, §7) |
| Trace view, trace graph, implementation and test links | Traceability from hazardous situation to software item, cause, risk control measure and its verification (IEC 62304 §7.3.3); of design outputs to design inputs (ISO 13485 §7.3.2) |
| Diffs, baselines (tags), history, commits | Configuration identification and change control, including traceability of change (IEC 62304 §8.1, §8.2); design changes (ISO 13485 §7.3.9) |
| Completeness check, test adequacy review | Requirements that are traceable, testable and verified (IEC 62304 §5.2.6, §5.7); completeness of risk control (ISO 14971 §7.6) |
| Mitigation adequacy review | Verification of the implementation *and effectiveness* of risk control measures (ISO 14971 §7.2; IEC 62304 §7.3.1) |
| Hazard discovery | Identification of hazards and hazardous situations (ISO 14971 §5.4; IEC 62304 §7.1) |
| Notes and findings | Inputs to software problem resolution (IEC 62304 §9) and to the risk management review (ISO 14971 §9) |

Agent findings are proposals. A person decides what enters the model, as for
any hand edit, and the commit records who made the change (`--author`, or
the name the UI sends). The editor does not make a record compliant; it makes
complete, consistent records cheaper to keep.

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
| `GET /api/entities?kind=` · `GET /api/entities/{id}` | lists; one entity with verdict, verification set (`members`, `set`, `quarantined`, `basis`, `derived_from`, and `lock`: what an out-of-date lock would change for it), traces, source references, issues and gaps |
| `POST /api/entities` · `PUT`/`DELETE /api/entities/{id}` · `POST /api/entities/{id}/rename` | create, update (its `notices`), delete (`?force=1` also removes references; `unlocked` lists the lock entries dropped with it), rename; a save that would give a case two owners is a 409 whose body lists `conflicts` (`code`, `message`, `case`, `entities`, `owner`); a file changed on disk since the save read it is a 409 too (`changed-on-disk`) |
| `POST /api/entities/{id}/precheck` | dry run of saving a draft (`{data}`; `_new` with `kind` for a create): `ok`, `problems`, `notices` (`takes-from-tag`), the entity's `issues`, and the set it would get |
| `GET /api/cases?target=&q=&state=&lane=` | the case ledger from the attribution (`state`: `owned`, `unowned`, `quarantined`, `unlocked`, `coarse`; `unowned=1` also works; `lane` with `rr serve --lane-targets`), with the lock's `lock_status` |
| `GET /api/lock?entity=` · `POST /api/lock/update` | whether the verification-set lock is out of date (for one entity's set); `{allow_removals?, dry_run?}`: `rr sets lock --write` over the loaded evidence |
| `POST /api/cases/move` | `{case, to, expand?, dry_run?}`: give a case to another owner (or `none`) through a checked model edit |
| `GET /api/attribution` | mode, lock, per-target counts and owners, quarantines, attribution issues, each entity's set |
| `POST /api/entities/{id}/notes` · `PATCH`/`DELETE …/notes/{note}` | notes |
| `GET /api/next-id?kind=` | the next id and the file a new entity would go to |
| `GET /api/graph?focus=&depth=&kinds=&methods=` | trace graph (nodes, edges, SVG, Mermaid) |
| `GET /api/report` · `GET /api/queue` · `POST /api/reload` | report JSON, gap queue, re-read evidence and sources |
| `GET /api/annotations` · `GET /api/source?path=` | source annotations; a file's lines |
| `GET /api/git/status` · `GET /api/git/refs` · `GET /api/git/log` | git state |
| `GET /api/diff?from=&to=` | semantic model diff (`to` defaults to the working tree) |
| `POST /api/git/commit` · `POST /api/git/tag` | commit model changes; name a baseline |
| `GET /api/agents` · `POST /api/agents/run` · `GET /api/agents/jobs[/{job}]` | workflows and jobs |
| `POST /api/findings/{id}/apply` · `POST /api/findings/{id}/dismiss` | act on a finding (`action`: `note`, `create`, `update`, or `worksheet` for a proposed case owner) |
