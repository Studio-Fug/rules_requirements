// SPDX-License-Identifier: AGPL-3.0-or-later
// Agents: run completeness and review workflows, turn findings into work.

import { enc, get, post } from "../api.js";
import { button, empty, glyph, idPicker, idTag, reportError, toast } from "../components.js";
import { add, h, plural, swap, timeAgo } from "../dom.js";
import { go, hashOf, reloadModel } from "../nav.js";
import { KIND, config, store } from "../store.js";

const SEVERITY_ORDER = ["error", "warning", "info"];
let pollTimer = 0;

function stopPolling() {
  clearTimeout(pollTimer);
  pollTimer = 0;
}

// --------------------------------------------------------------------------
// Workflow cards
// --------------------------------------------------------------------------

function paramInput(name, help, wf) {
  if (name === "use_llm") {
    const el = h("input", { type: "checkbox", checked: true });
    return [h("label", { class: "check" }, el, "Also run the LLM coverage review"), () => el.checked];
  }
  if (name === "limit") {
    const el = h("input", { type: "number", min: 1, max: 200, value: 25, "aria-label": "Limit" });
    return [h("label", { class: "field inline" }, h("span", null, "At most"), el), () => Number(el.value) || 25];
  }
  if (name === "entities" || name === "focus") {
    const kinds =
      wf.id === "mitigation_adequacy"
        ? ["risk"]
        : name === "focus"
          ? ["user_need", "requirement", "mitigation", "risk", "test_method"]
          : ["requirement"];
    const picker = idPicker({
      kinds,
      placeholder:
        name === "focus"
          ? "Entities to include in full (optional)"
          : `Leave empty for all ${kinds[0] === "risk" ? "risks" : "requirements"}`,
      label: help,
    });
    return [
      h("div", { class: "field" }, h("span", { class: "field-label" }, name === "focus" ? "Focus" : "Scope"), picker),
      () => picker.getValue(),
    ];
  }
  if (name === "instruction") {
    const el = h("textarea", { rows: 3, placeholder: help, "aria-label": "Instruction" });
    return [h("div", { class: "field" }, el), () => el.value.trim()];
  }
  const el = h("input", { type: "text", placeholder: help, "aria-label": name });
  return [h("label", { class: "field" }, h("span", null, name), el), () => el.value.trim()];
}

function workflowCard(wf, llm) {
  const readers = {};
  const inputs = [];
  for (const [name, help] of Object.entries(wf.params || {})) {
    if (name === "use_llm" && !llm.available) continue;
    const [el, read] = paramInput(name, help, wf);
    readers[name] = read;
    inputs.push(el);
  }
  const run = async () => {
    const params = {};
    for (const [k, read] of Object.entries(readers)) {
      const v = read();
      if (Array.isArray(v) && !v.length) continue;
      if (v === "" || v === undefined) continue;
      params[k] = v;
    }
    try {
      const job = await post("/api/agents/run", { workflow: wf.id, params });
      toast(`${wf.title} started`);
      go(hashOf("agents", { job: job.id }));
    } catch (err) {
      reportError(err);
    }
  };
  return h(
    "article",
    { class: ["wf", wf.available ? "" : "off"] },
    h(
      "h3",
      null,
      wf.title,
      wf.needs_llm
        ? h("span", { class: "wf-llm", title: "Uses the LLM" }, "LLM")
        : h("span", { class: "wf-llm det", title: "Deterministic" }, "rules"),
    ),
    h("p", null, wf.description),
    inputs,
    button("Run", run, {
      primary: true,
      small: true,
      disabled: !wf.available,
      title: wf.available ? "" : llm.reason || "No LLM configured",
    }),
  );
}

function assistantComposer(llm) {
  const text = h("textarea", {
    rows: 3,
    placeholder: "e.g. Draft requirements for logging every setpoint change, and a risk for losing the log",
    "aria-label": "Instruction for the assistant",
    disabled: !llm.available,
  });
  const focus = idPicker({
    kinds: ["user_need", "requirement", "mitigation", "risk", "test_method"],
    placeholder: "Focus entities (optional)",
    label: "Focus entities",
  });
  const send = async () => {
    if (!text.value.trim()) {
      text.focus();
      return;
    }
    try {
      const job = await post("/api/agents/run", {
        workflow: "assistant",
        params: { instruction: text.value.trim(), focus: focus.getValue() },
      });
      go(hashOf("agents", { job: job.id }));
    } catch (err) {
      reportError(err);
    }
  };
  text.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && (ev.metaKey || ev.ctrlKey)) {
      ev.preventDefault();
      send();
    }
  });
  return h(
    "section",
    { class: "block assistant" },
    h("h2", null, "Ask the assistant"),
    h(
      "p",
      { class: "muted" },
      "Describe a change in plain language. The assistant proposes new objects, updates and notes; nothing changes until you apply a proposal.",
    ),
    text,
    focus,
    h(
      "div",
      { class: "row" },
      button("Propose changes", send, { primary: true, disabled: !llm.available }),
      h("span", { class: "muted small" }, "Ctrl+Enter"),
    ),
  );
}

// --------------------------------------------------------------------------
// Findings
// --------------------------------------------------------------------------

function proposalPreview(p) {
  if (!p) return null;
  if (p.kind === "case_owner") {
    // A proposed case owner: recorded in the attribution worksheet, never applied to the model.
    return h(
      "div",
      { class: "proposal" },
      h("p", { class: "proposal-head" }, "Proposed owner (for the attribution worksheet)"),
      h(
        "dl",
        { class: "facts small" },
        h("dt", null, "case"),
        h("dd", null, h("code", { class: "case-key" }, p.case)),
        h("dt", null, "owner"),
        h("dd", null, store.byId.has(p.owner) ? idTag(p.owner, { compact: true }) : p.owner),
        p.current ? [h("dt", null, "owned by today"), h("dd", null, idTag(p.current, { compact: true }))] : null,
      ),
    );
  }
  const data = p.data || {};
  const rows = Object.entries(data).filter(([k]) => k !== "id");
  return h(
    "div",
    { class: "proposal" },
    h(
      "p",
      { class: "proposal-head" },
      glyph(p.kind),
      `${p.op === "update" ? "Proposed update" : `Proposed ${KIND[p.kind] ? KIND[p.kind].one.toLowerCase() : p.kind}`}`,
    ),
    h(
      "dl",
      { class: "facts small" },
      rows.map(([k, v]) => [
        h("dt", null, k.replace(/_/g, " ")),
        h(
          "dd",
          null,
          Array.isArray(v)
            ? v.map((x) =>
                typeof x === "string" && store.byId.has(x)
                  ? [idTag(x, { compact: true }), " "]
                  : `${typeof x === "string" ? x : JSON.stringify(x)} `,
              )
            : String(v),
        ),
      ]),
    ),
  );
}

function noteForm(f, onDone) {
  const kinds = (config().note_kinds || ["comment", "gap", "question", "todo"])
    .filter((k) => k !== "comment")
    .concat("comment");
  const kind = h(
    "select",
    { "aria-label": "Note kind" },
    kinds.map((k) => h("option", { value: k, selected: k === (f.severity === "info" ? "todo" : "gap") }, k)),
  );
  const target = f.entity
    ? null
    : idPicker({
        kinds: ["user_need", "requirement", "mitigation", "risk", "test_method"],
        single: true,
        placeholder: "Attach to which entity?",
        label: "Entity",
      });
  const text = h("textarea", { rows: 3, "aria-label": "Note text" }, f.title + (f.detail ? `\n\n${f.detail}` : ""));
  const form = h(
    "div",
    { class: "inline-form" },
    target,
    text,
    h(
      "div",
      { class: "row" },
      kind,
      button(
        "Add note",
        async () => {
          const entity = f.entity || (target && target.getValue()[0]);
          if (!entity) {
            toast("Pick the entity to attach the note to", "error");
            return;
          }
          try {
            await post(`/api/findings/${enc(f.id)}/apply`, {
              action: "note",
              entity,
              kind: kind.value,
              text: text.value.trim(),
            });
            await reloadModel({ page: false });
            toast(`Note added to ${entity}`);
            onDone();
          } catch (err) {
            reportError(err);
          }
        },
        { primary: true, small: true },
      ),
      button("Cancel", () => form.remove(), { quiet: true, small: true }),
    ),
  );
  return form;
}

function findingItem(f, job, refresh) {
  const actions = h("div", { class: "finding-actions" });
  const body = h("div", { class: "finding-body" });
  const done = f.status !== "open";
  if (!done) {
    add(
      actions,
      button(
        "Add as note",
        () => {
          if (!body.querySelector(".inline-form")) add(body, noteForm(f, refresh));
        },
        { small: true },
      ),
    );
    if (f.proposal && f.proposal.kind === "case_owner") {
      add(
        actions,
        button(
          "Record in worksheet",
          async () => {
            try {
              const res = await post(`/api/findings/${enc(f.id)}/apply`, { action: "worksheet" });
              toast(`Proposal recorded in ${res.worksheet}; a person decides the owner there`);
              refresh();
            } catch (err) {
              reportError(err);
            }
          },
          { small: true, primary: true },
        ),
      );
    } else if (f.proposal && f.proposal.op === "update" && f.entity) {
      add(
        actions,
        button(
          "Review update",
          () => {
            store.draft = { entity: f.entity, kind: f.proposal.kind, data: f.proposal.data, findingId: f.id };
            go(`#/edit/${enc(f.entity)}`);
          },
          { small: true, primary: true },
        ),
      );
    } else if (f.proposal) {
      add(
        actions,
        button(
          `Create ${KIND[f.proposal.kind] ? KIND[f.proposal.kind].one.toLowerCase() : "object"}`,
          () => {
            store.draft = { kind: f.proposal.kind, data: f.proposal.data, findingId: f.id };
            go(`#/new/${f.proposal.kind}`);
          },
          { small: true, primary: true },
        ),
      );
    }
    add(
      actions,
      button(
        "Dismiss",
        async () => {
          try {
            await post(`/api/findings/${enc(f.id)}/dismiss`);
            refresh();
          } catch (err) {
            reportError(err);
          }
        },
        { small: true, quiet: true },
      ),
    );
  }
  add(
    body,
    h(
      "div",
      { class: "finding-head" },
      h("span", { class: `sev ${f.severity}` }, f.severity),
      h("span", { class: "finding-title" }, f.title),
      f.entity && store.byId.has(f.entity) ? idTag(f.entity, { compact: true }) : null,
      done ? h("span", { class: `finding-status ${f.status}` }, f.status) : null,
    ),
    h(
      "p",
      { class: "finding-meta" },
      h("code", { class: "code-tag" }, f.category),
      h("span", { class: `finding-source ${f.source}` }, f.source === "llm" ? "from the agent" : "from the rules"),
      f.refs && f.refs.length
        ? h(
            "span",
            null,
            "related: ",
            f.refs.map((r) => (store.byId.has(r) ? [idTag(r, { compact: true }), " "] : `${r} `)),
          )
        : null,
    ),
    f.detail ? h("p", { class: "finding-detail" }, f.detail) : null,
    proposalPreview(f.proposal),
    actions,
  );
  return h("li", { class: ["finding", `sev-${f.severity}`, done ? "done" : ""] }, body);
}

function findingsList(job, refresh) {
  const findings = job.findings || [];
  if (!findings.length) return job.status === "done" ? empty("No findings. Nothing to act on.") : null;
  const open = findings.filter((f) => f.status === "open");
  const groups = SEVERITY_ORDER.map((sev) => [sev, findings.filter((f) => f.severity === sev)]).filter(
    ([, list]) => list.length,
  );
  return h(
    "div",
    { class: "findings" },
    h("p", { class: "muted" }, `${plural(findings.length, "finding")}, ${open.length} open`),
    groups.map(([sev, list]) =>
      h(
        "section",
        { class: `finding-group ${sev}` },
        h(
          "h3",
          null,
          `${sev === "error" ? "Errors" : sev === "warning" ? "Warnings" : "Information"} (${list.length})`,
        ),
        h(
          "ul",
          { class: "finding-list" },
          list.map((f) => findingItem(f, job, refresh)),
        ),
      ),
    ),
  );
}

async function jobPanel(jobId) {
  const holder = h("section", { class: "block job", "aria-live": "polite" });
  const draw = async () => {
    let job;
    try {
      job = await get(`/api/agents/jobs/${enc(jobId)}`);
    } catch (err) {
      swap(holder, h("p", { class: "form-error" }, err.message));
      return;
    }
    if (!holder.isConnected && holder.dataset.drawn) return; // navigated away
    holder.dataset.drawn = "1";
    const running = job.status === "running" || job.status === "queued";
    swap(
      holder,
      h(
        "h2",
        null,
        `${job.workflow.replace(/_/g, " ")}`,
        h("span", { class: `job-status ${job.status}` }, running ? "running" : job.status),
        h("span", { class: "muted" }, ` ${job.id}, started ${timeAgo(job.started)}`),
      ),
      job.result && job.result.reply ? h("div", { class: "reply" }, h("p", null, job.result.reply)) : null,
      job.error ? h("p", { class: "form-error" }, job.error) : null,
      h(
        "details",
        { class: "job-log", open: running },
        h("summary", null, `Log (${job.log.length} lines)`),
        h("pre", null, job.log.join("\n")),
      ),
      findingsList(job, draw),
    );
    stopPolling();
    if (running) pollTimer = setTimeout(draw, 1000);
  };
  await draw();
  return holder;
}

// --------------------------------------------------------------------------
// Page
// --------------------------------------------------------------------------

export async function renderAgents(query) {
  stopPolling();
  const [info, jobs] = await Promise.all([get("/api/agents"), get("/api/agents/jobs")]);
  const llm = info.llm || {};
  const page = h("div", { class: "page agents" });
  add(
    page,
    h(
      "header",
      { class: "page-head" },
      h("h1", null, "Agents"),
      h(
        "p",
        { class: "lede" },
        "Check the model for gaps and ask whether tests, code and mitigations really do what the requirements say. Findings become notes or new objects, and open notes join the work queue.",
      ),
    ),
  );
  add(
    page,
    llm.available
      ? h(
          "p",
          { class: "callout ok small" },
          `LLM workflows use ${llm.name}. Each run sends the relevant model text and source excerpts to the model.`,
        )
      : h(
          "div",
          { class: "callout amber" },
          h("h2", null, "LLM workflows are off"),
          h("p", null, llm.reason ? `${llm.reason[0].toUpperCase()}${llm.reason.slice(1)}.` : "No LLM is configured."),
          h(
            "p",
            null,
            "Then provide Claude credentials, for example ",
            h("code", null, "export ANTHROPIC_API_KEY=…"),
            ", and restart rr serve. The completeness check runs without an LLM.",
          ),
        ),
  );

  const jobId = query.get("job");
  const grid = h("div", { class: "agents-grid" });
  const left = h("div", { class: "agents-main" });
  const right = h("aside", { class: "agents-side" });

  if (jobId) add(left, await jobPanel(jobId));
  add(left, assistantComposer(llm));
  add(
    left,
    h(
      "section",
      { class: "block" },
      h("h2", null, "Workflows"),
      h(
        "div",
        { class: "wf-grid" },
        info.workflows.filter((w) => w.id !== "assistant").map((w) => workflowCard(w, llm)),
      ),
    ),
  );

  const list = jobs.jobs || [];
  add(
    right,
    h(
      "section",
      { class: "block" },
      h("h2", null, "Runs"),
      list.length
        ? h(
            "ul",
            { class: "plain runs" },
            list.map((j) =>
              h(
                "li",
                { class: j.id === jobId ? "current" : "" },
                h("a", { href: hashOf("agents", { job: j.id }) }, j.workflow.replace(/_/g, " ")),
                h("span", { class: `job-status ${j.status}` }, j.status),
                h("span", { class: "muted" }, `${plural(j.findings || 0, "finding")}, ${timeAgo(j.started)}`),
              ),
            ),
          )
        : h("p", { class: "muted" }, "No runs yet in this session."),
    ),
  );
  add(grid, left, right);
  add(page, grid);
  return page;
}

export { stopPolling };
