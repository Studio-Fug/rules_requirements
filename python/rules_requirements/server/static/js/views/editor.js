// SPDX-License-Identifier: AGPL-3.0-or-later
// Create and edit form for every entity kind.

import { enc, get, post, put, settings } from "../api.js";
import { button, glyph, idPicker, reportError, toast } from "../components.js";
import { add, debounce, h, swap } from "../dom.js";
import { VERIFIABLE, escapeSelector, noticeList, problemList, setLine } from "../ledger.js";
import { go, reloadModel } from "../nav.js";
import { KIND, config, entitiesOf, levelNames, store } from "../store.js";

const CLAIM_HINT =
  "The test cases this entity claims, per target: case selectors ('*' matches any run of characters), or the " +
  "whole target when it reports no per-case results. A test case verifies at most one requirement: a case " +
  "another entity owns cannot be claimed here (move it in the case ledger).";

// Field specs, grouped. `type` decides the control.
function spec(kind) {
  const c = config();
  const statement = [
    { key: "title", label: "Title", type: "text", required: true, hint: "One line, stated so it can be checked." },
    { key: "description", label: "Description", type: "textarea", rows: 4 },
  ];
  const meta = [
    { key: "status", label: "Lifecycle status", type: "select", options: ["", ...(c.statuses || [])] },
    { key: "owner", label: "Owner", type: "text" },
    { key: "tags", label: "Tags", type: "list", hint: "Comma-separated." },
  ];
  switch (kind) {
    case "user_need":
      return [
        {
          legend: "Statement",
          fields: [...statement, { key: "rationale", label: "Rationale", type: "textarea", rows: 3 }],
        },
        {
          legend: "Validation",
          fields: [{ key: "validated_by", label: "Validation set", type: "verified_by", hint: CLAIM_HINT }],
        },
        { legend: "Bookkeeping", fields: meta },
      ];
    case "requirement":
      return [
        {
          legend: "Statement",
          fields: [...statement, { key: "rationale", label: "Rationale", type: "textarea", rows: 3 }],
        },
        {
          legend: "Traces",
          fields: [
            { key: "satisfies", label: "Satisfies user needs", type: "ids", kinds: ["user_need"] },
            {
              key: "refines",
              label: "Refines requirements",
              type: "ids",
              kinds: ["requirement"],
              hint: "Parent requirements this one decomposes.",
            },
          ],
        },
        {
          legend: "Verification",
          fields: [
            {
              key: "method",
              label: "Demanded method",
              type: "method",
              hint: "A test method, or a verification level. Evidence below this rigor counts as under-verified.",
            },
            {
              key: "verified_by",
              label: "Verification set",
              type: "verified_by",
              hint: CLAIM_HINT,
            },
          ],
        },
        {
          legend: "Bookkeeping",
          fields: [
            { key: "category", label: "Category", type: "text", hint: "functional, performance, safety, security…" },
            { key: "modules", label: "Modules", type: "list", hint: "Implementing modules, comma-separated." },
            ...meta,
          ],
        },
      ];
    case "risk":
      return [
        { legend: "Statement", fields: statement },
        {
          legend: "Hazard to harm",
          fields: [
            { key: "hazard", label: "Hazard", type: "textarea", rows: 2 },
            { key: "hazardous_situation", label: "Hazardous situation", type: "textarea", rows: 2 },
            { key: "harm", label: "Harm", type: "textarea", rows: 2 },
          ],
        },
        {
          legend: "Estimate",
          fields: [
            { key: "severity", label: "Severity", type: "select", options: ["", ...(c.severities || [])] },
            { key: "likelihood", label: "Likelihood", type: "select", options: ["", ...(c.likelihoods || [])] },
            {
              key: "residual_severity",
              label: "Residual severity",
              type: "select",
              options: ["", ...(c.severities || [])],
            },
            {
              key: "residual_likelihood",
              label: "Residual likelihood",
              type: "select",
              options: ["", ...(c.likelihoods || [])],
            },
            {
              key: "residual",
              label: "Residual risk note",
              type: "textarea",
              rows: 2,
              hint: "What remains after control, and why it is acceptable.",
            },
          ],
        },
        { legend: "Bookkeeping", fields: meta },
      ];
    case "mitigation":
      return [
        { legend: "Statement", fields: statement },
        {
          legend: "Control",
          fields: [
            {
              key: "type",
              label: "Type of control",
              type: "select",
              options: ["", ...(c.mitigation_types || [])],
              hint: "inherent safety by design, protective measure, or information for safety",
            },
            { key: "mitigates", label: "Mitigates risks", type: "ids", kinds: ["risk"] },
            { key: "implemented_by", label: "Implemented by requirements", type: "ids", kinds: ["requirement"] },
          ],
        },
        {
          legend: "Verification",
          fields: [{ key: "verified_by", label: "Verification set", type: "verified_by", hint: CLAIM_HINT }],
        },
        { legend: "Bookkeeping", fields: meta },
      ];
    case "test_method":
      return [
        { legend: "Statement", fields: statement },
        {
          legend: "Method",
          fields: [
            { key: "level", label: "Level", type: "select", options: ["", ...levelNames()], required: true },
            { key: "procedure", label: "Procedure", type: "textarea", rows: 4 },
          ],
        },
        { legend: "Bookkeeping", fields: meta },
      ];
    default:
      return [];
  }
}

function control(field, value, ctx = {}) {
  const id = `f-${field.key}`;
  const labelEl = (el) =>
    h(
      "div",
      {
        class: [
          "field",
          field.type === "textarea" ? "wide" : "",
          field.type === "ids" || field.type === "verified_by" ? "wide" : "",
        ],
      },
      h(
        "label",
        { for: id },
        field.label,
        field.required ? h("span", { class: "req", "aria-hidden": "true" }, " *") : null,
      ),
      el,
      field.hint ? h("small", null, field.hint) : null,
    );
  switch (field.type) {
    case "text": {
      const el = h("input", { id, type: "text", value: value || "", required: field.required || false });
      el.read = () => el.value.trim();
      return [labelEl(el), el];
    }
    case "textarea": {
      const el = h("textarea", { id, rows: field.rows || 3 }, value || "");
      el.read = () => el.value.trim();
      return [labelEl(el), el];
    }
    case "select": {
      const opts = field.options.slice();
      if (value && !opts.includes(value)) opts.push(value);
      const el = h(
        "select",
        { id },
        opts.map((o) => h("option", { value: o, selected: o === (value || "") }, o || "—")),
      );
      el.read = () => el.value;
      return [labelEl(el), el];
    }
    case "list": {
      const text = Array.isArray(value) ? value.join(", ") : value || "";
      const el = h("input", { id, type: "text", value: text });
      el.read = () =>
        el.value
          .split(",")
          .map((x) => x.trim())
          .filter(Boolean);
      return [labelEl(el), el];
    }
    case "ids": {
      const el = idPicker({ kinds: field.kinds, value: value || [], label: field.label });
      el.querySelector("input").id = id;
      el.read = () => el.getValue();
      return [labelEl(el), el];
    }
    case "method": {
      const methods = entitiesOf("test_method");
      const el = h(
        "select",
        { id },
        h("option", { value: "" }, "Project default level"),
        methods.length
          ? h(
              "optgroup",
              { label: "Test methods" },
              methods.map((m) =>
                h(
                  "option",
                  { value: m.id, selected: m.id === value },
                  `${m.id}: ${m.title}${m.data.level ? ` (${m.data.level})` : ""}`,
                ),
              ),
            )
          : null,
        h(
          "optgroup",
          { label: "Levels" },
          levelNames().map((l) => h("option", { value: l, selected: l === value }, l)),
        ),
      );
      if (value && !methods.some((m) => m.id === value) && !levelNames().includes(value))
        add(el, h("option", { value, selected: true }, `${value} (unknown)`));
      el.read = () => el.value;
      return [labelEl(el), el];
    }
    case "verified_by": {
      const rowsHost = h("div", { class: "vb-rows" });
      const rows = [];
      for (const v of value || []) rows.push(claimRow(v, ctx, rowsHost));
      const el = h(
        "div",
        { class: "vb", id },
        rowsHost,
        button(
          "Add target",
          () => {
            const row = claimRow(null, ctx, rowsHost);
            rows.push(row);
            row.focusTarget();
            if (ctx.onChange) ctx.onChange();
          },
          { small: true },
        ),
      );
      el.read = () => [...rowsHost.children].map((r) => (r.read ? r.read() : null)).filter(Boolean);
      el.problems = () => [...rowsHost.children].map((r) => (r.problem ? r.problem() : "")).filter(Boolean);
      el.setCounts = (counts) => [...rowsHost.children].forEach((r) => r.setCounts && r.setCounts(counts));
      return [labelEl(el), el];
    }
    default:
      return [labelEl(h("span", null, "?")), { read: () => value }];
  }
}

/**
 * One claim item: a target and either its case selectors or the whole target.
 * The checklist offers the target's observed cases; a case another entity
 * owns is disabled (move it in the case ledger instead). An item left
 * untouched is written back exactly as it was read.
 */
function claimRow(item, ctx, host) {
  const orig = item;
  const it = typeof item === "string" ? { target: item, legacy: true } : { ...(item || {}) };
  let dirty = item === null || item === undefined;
  const ledger = ctx.ledger || { cases: [] };
  const target = h("input", {
    type: "text",
    value: it.target || "",
    placeholder: "//pkg:test_target",
    "aria-label": "Test target",
    list: "vb-targets",
    class: "vb-target",
  });
  const whole = Boolean(it.whole || it.legacy);
  const mode = h(
    "select",
    { "aria-label": "Claim", class: "vb-mode" },
    h("option", { value: "cases", selected: !whole }, "Cases"),
    h("option", { value: "whole", selected: whole }, "Whole target"),
  );
  const level = h(
    "select",
    { "aria-label": "Level it provides" },
    h("option", { value: "" }, "default level"),
    levelNames().map((n) => h("option", { value: n, selected: n === it.level }, n)),
  );
  const selectors = h(
    "textarea",
    {
      rows: 3,
      class: "vb-selectors",
      "aria-label": "Case selectors, one per line",
      placeholder: "one selector per line, e.g. clocksync::*",
    },
    Array.isArray(it.cases) ? it.cases.join("\n") : "",
  );
  const reason = h("input", {
    type: "text",
    value: it.reason || "",
    class: "vb-reason",
    "aria-label": "Why the whole target",
    placeholder: "why can this target not be claimed per case?",
  });
  const count = h("span", { class: "vb-count muted" });
  const list = h("div", { class: "vb-cases" });
  const casesBox = h("div", { class: "vb-cases-box" }, selectors, count, list);
  const reasonHint = h("small", { class: "form-error vb-reason-missing", hidden: true }, "A whole-target claim needs a reason.");
  const wholeBox = h("div", { class: "vb-whole-box" }, reason, reasonHint);
  const lines = () =>
    selectors.value
      .split("\n")
      .map((x) => x.trim())
      .filter(Boolean);

  function changed() {
    dirty = true;
    if (ctx.onChange) ctx.onChange();
  }

  function drawList() {
    const t = target.value.trim();
    const observed = ledger.cases.filter((c) => c.target === t);
    const perCase = observed.filter((c) => !c.synthetic);
    if (!t || !observed.length) {
      swap(list, t ? h("p", { class: "muted small" }, "No result of this target in the loaded evidence.") : null);
      return;
    }
    if (!perCase.length) {
      swap(list, h("p", { class: "muted small" }, "This target reports no per-case results: claim it as a whole target."));
      return;
    }
    const chosen = new Set(lines());
    swap(
      list,
      perCase.slice(0, 300).map((c) => {
        const lit = escapeSelector(c.path);
        const other = c.owner && c.owner !== ctx.entityId ? c.owner : "";
        const box = h("input", { type: "checkbox", checked: chosen.has(lit), disabled: Boolean(other) });
        box.addEventListener("change", () => {
          const now = lines().filter((x) => x !== lit);
          if (box.checked) now.push(lit);
          selectors.value = now.join("\n");
          changed();
        });
        return h(
          "label",
          { class: ["vb-case", other ? "taken" : ""], title: c.case },
          box,
          h("span", { class: "test-name" }, c.path),
          h("span", { class: `result ${c.status}` }, ` ${c.status}`),
          other ? h("span", { class: "muted" }, ` owned by ${other}`) : null,
          c.quarantine ? h("span", { class: "mstate st-fail" }, c.quarantine.code) : null,
        );
      }),
      perCase.length > 300 ? h("p", { class: "muted small" }, `${perCase.length - 300} more; type selectors above.`) : null,
    );
  }

  function sync() {
    casesBox.hidden = mode.value === "whole";
    wholeBox.hidden = !casesBox.hidden;
    reasonHint.hidden = !(dirty && mode.value === "whole" && !reason.value.trim());
    drawList();
  }

  const row = h(
    "div",
    { class: "vb-row claim" },
    h(
      "div",
      { class: "vb-head" },
      target,
      mode,
      level,
      button(
        "Remove",
        () => {
          row.remove();
          if (ctx.onChange) ctx.onChange();
        },
        { small: true, quiet: true },
      ),
    ),
    casesBox,
    wholeBox,
  );
  target.addEventListener("input", () => {
    sync();
    changed();
  });
  mode.addEventListener("change", () => {
    changed();
    sync();
  });
  for (const el of [level, selectors, reason]) el.addEventListener("input", changed);
  reason.addEventListener("input", sync);
  selectors.addEventListener("input", drawList);
  row.read = () => {
    if (!dirty) return orig;
    const t = target.value.trim();
    if (!t) return null;
    const extra = typeof orig === "object" && orig ? { ...orig } : {};
    for (const k of ["target", "cases", "whole", "reason", "level"]) delete extra[k];
    const out = { target: t };
    if (mode.value === "whole") out.whole = true;
    else out.cases = lines();
    if (level.value) out.level = level.value;
    if (mode.value === "whole" && reason.value.trim()) out.reason = reason.value.trim();
    return { ...out, ...extra };
  };
  // Whole mode requires a reason (the save guard refuses a new one without).
  row.problem = () =>
    dirty && target.value.trim() && mode.value === "whole" && !reason.value.trim()
      ? `${target.value.trim()}: say why the whole target is claimed (reason), or claim its cases`
      : "";
  row.setCounts = (counts) => {
    const t = target.value.trim();
    if (mode.value === "whole") {
      swap(count);
      return;
    }
    const mine = (counts || []).filter((c) => c.target === t && lines().includes(c.selector));
    const n = mine.reduce((a, c) => a + c.cases, 0);
    swap(count, lines().length ? `matches ${n} case${n === 1 ? "" : "s"}` : "");
  };
  row.focusTarget = () => target.focus();
  sync();
  add(host, row);
  return row;
}

/**
 * renderEditor({kind, query}) for a new entity, or ({id}) to edit one.
 * A draft handed over by the agents view (store.draft) prefills the form and
 * is saved through the finding, so the finding is marked applied.
 */
export async function renderEditor({ kind, id, query }) {
  let existing = null;
  let base = {};
  let draft = null;
  if (store.draft && ((id && store.draft.entity === id) || (!id && store.draft.kind === kind))) {
    draft = store.draft;
  }
  if (id) {
    existing = await get(`/api/entities/${enc(id)}`);
    kind = existing.kind;
    base = { ...existing.data };
    if (draft) base = { ...base, ...draft.data, id };
    // Notes are edited on the entity page, never through this form: the server
    // keeps them when `notes` is absent, so a stale form cannot drop a note.
    delete base.notes;
  } else if (draft) {
    base = { ...draft.data };
  }
  const meta = KIND[kind];
  const page = h("div", { class: "page editor" });
  const errorBox = h("div", { class: "form-error", role: "alert", hidden: true });
  const precheckBox = h("div", { class: "precheck", "aria-live": "polite" });
  const fields = [];
  // The case ledger (owners from the server's attribution) for the claim checklists.
  let ledger = { cases: [], targets: [] };
  if (VERIFIABLE.includes(kind)) {
    try {
      ledger = await get("/api/cases");
    } catch (err) {
      reportError(err);
    }
  }
  const ctx = { ledger, entityId: existing ? existing.id : "", onChange: () => schedulePrecheck() };

  let idInput = null;
  let fileInput = null;
  if (!existing) {
    let next = { id: "", file: "" };
    try {
      next = await get(`/api/next-id?kind=${enc(kind)}`);
    } catch (err) {
      reportError(err);
    }
    idInput = h("input", { id: "f-id", type: "text", value: base.id || next.id, required: true, autocomplete: "off" });
    fileInput = h("input", { id: "f-file", type: "text", value: next.file || "", autocomplete: "off" });
  }

  const form = h("form", { class: "entity-form", novalidate: true });
  if (!existing) {
    add(
      form,
      h(
        "fieldset",
        null,
        h("legend", null, "Identity"),
        h(
          "div",
          { class: "field" },
          h("label", { for: "f-id" }, "Id", h("span", { class: "req", "aria-hidden": "true" }, " *")),
          idInput,
          h("small", null, `Next free ${meta.one.toLowerCase()} id. Must match the project's id pattern.`),
        ),
        h(
          "div",
          { class: "field" },
          h("label", { for: "f-file" }, "Save to"),
          fileInput,
          h("small", null, "Model file (or directory, for one-object-per-file layouts)."),
        ),
      ),
    );
  }
  for (const group of spec(kind)) {
    const fs = h("fieldset", null, h("legend", null, group.legend));
    for (const field of group.fields) {
      const [wrapper, el] = control(field, base[field.key], ctx);
      fields.push([field, el]);
      add(fs, wrapper);
    }
    add(form, fs);
  }

  const saveLabel = existing ? "Save changes" : `Create ${meta.one.toLowerCase()}`;
  const submit = h("button", { type: "submit", class: "btn primary" }, saveLabel);
  add(
    form,
    h(
      "datalist",
      { id: "vb-targets" },
      (ledger.targets || []).map((t) => h("option", { value: t })),
    ),
    precheckBox,
    errorBox,
    h(
      "div",
      { class: "form-actions" },
      submit,
      button(
        "Cancel",
        () => {
          store.draft = null;
          history.length > 1 ? history.back() : go(existing ? `#/entity/${enc(existing.id)}` : `#/${meta.route}`);
        },
        { quiet: true },
      ),
      draft ? h("span", { class: "muted" }, `Prefilled from agent finding ${draft.findingId}.`) : null,
    ),
  );

  function collect() {
    const data = { ...base };
    delete data.notes;
    for (const [field, el] of fields) {
      const v = el.read();
      if (v === "" || v === null || v === undefined || (Array.isArray(v) && !v.length)) delete data[field.key];
      else data[field.key] = v;
    }
    return data;
  }

  // Live dry run: the save guard's verdict on the draft, and the set it would give.
  let precheckSeq = 0;
  let blocked = false;
  async function precheck() {
    if (!VERIFIABLE.includes(kind)) return;
    const seq = ++precheckSeq;
    const data = collect();
    if (!existing) data.id = idInput.value.trim();
    let res;
    try {
      res = await post(`/api/entities/${existing ? enc(existing.id) : "_new"}/precheck`, {
        kind,
        data,
        file: fileInput ? fileInput.value.trim() : "",
      });
    } catch (err) {
      return; // the save itself reports what is wrong
    }
    if (seq !== precheckSeq) return;
    const problems = (res.problems || []).filter((p) => p.code !== "invalid");
    for (const message of localProblems()) {
      if (!problems.some((p) => p.code === "whole-target-reference")) problems.push({ code: "whole-target-reference", message });
    }
    blocked = problems.length > 0;
    submit.disabled = blocked;
    for (const [, el] of fields) if (el.setCounts) el.setCounts(res.selectors || []);
    const notices = noticeList(res.notices);
    swap(
      precheckBox,
      notices,
      problems.length
        ? h(
            "div",
            { class: "callout fail" },
            h("h3", null, "This draft cannot be saved"),
            h("p", null, "A test case verifies at most one requirement."),
            problemList(problems),
          )
        : res.ok && res.set && res.set.members
          ? h("p", { class: "muted precheck-ok" }, `Its ${kind === "user_need" ? "validation" : "verification"} set would be: ${setLine(res.set)}.`)
          : null,
    );
  }
  // Problems the form knows of before the server does (a whole claim without a reason).
  function localProblems() {
    return fields.flatMap(([, el]) => (el.problems ? el.problems() : []));
  }
  const schedulePrecheck = debounce(precheck, 300);
  form.addEventListener("input", () => schedulePrecheck());
  form.addEventListener("change", () => schedulePrecheck());

  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    errorBox.hidden = true;
    const local = localProblems();
    if (blocked || local.length) {
      errorBox.textContent = local.length
        ? `Resolve the problems above first: ${local.join("; ")}.`
        : "Resolve the problems above first: a test case verifies at most one requirement.";
      errorBox.hidden = false;
      return;
    }
    const data = collect();
    const missing = fields.filter(([f]) => f.required && !data[f.key]).map(([f]) => f.label);
    if (missing.length) {
      errorBox.textContent = `Fill in: ${missing.join(", ")}.`;
      errorBox.hidden = false;
      return;
    }
    submit.disabled = true;
    try {
      let savedId;
      if (existing) {
        data.id = existing.id;
        if (draft) {
          await post(`/api/findings/${enc(draft.findingId)}/apply`, {
            action: "update",
            entity: existing.id,
            data,
            version: existing.version,
            author: settings.author || undefined,
          });
        } else {
          // `version` makes a concurrent change a 409 instead of a silent overwrite.
          const res = await put(`/api/entities/${enc(existing.id)}`, { data, version: existing.version });
          for (const n of res.notices || []) toast(n.message, "warn", 8000);
        }
        savedId = existing.id;
      } else {
        data.id = idInput.value.trim();
        if (draft) {
          const res = await post(`/api/findings/${enc(draft.findingId)}/apply`, {
            action: "create",
            kind,
            data,
            file: fileInput.value.trim(),
          });
          savedId = res.entity.id;
        } else {
          const res = await post("/api/entities", { kind, data, file: fileInput.value.trim() });
          savedId = res.id;
        }
      }
      store.draft = null;
      await reloadModel({ page: false });
      toast(existing ? `Saved ${savedId}` : `Created ${savedId}`);
      go(`#/entity/${enc(savedId)}`);
    } catch (err) {
      errorBox.textContent = err.message;
      if (err.data && err.data.conflicts) swap(precheckBox, problemList(err.data.conflicts));
      errorBox.hidden = false;
      submit.disabled = false;
      errorBox.scrollIntoView({ block: "nearest" });
    }
  });

  add(
    page,
    h(
      "header",
      { class: "page-head" },
      existing
        ? h(
            "nav",
            { class: "crumbs", "aria-label": "Breadcrumb" },
            h("a", { href: `#/${meta.route}` }, meta.many),
            " / ",
            h("a", { href: `#/entity/${enc(existing.id)}` }, existing.id),
            " / edit",
          )
        : h(
            "nav",
            { class: "crumbs", "aria-label": "Breadcrumb" },
            h("a", { href: `#/${meta.route}` }, meta.many),
            " / new",
          ),
      h("h1", null, glyph(kind), existing ? `Edit ${existing.id}` : `New ${meta.one.toLowerCase()}`),
      existing && existing.location
        ? h(
            "p",
            { class: "muted" },
            `Changes are written to ${existing.location.path}; untouched fields and comments stay as they are.`,
          )
        : null,
    ),
    form,
  );
  if (VERIFIABLE.includes(kind)) schedulePrecheck();
  const first = form.querySelector(existing ? "#f-title" : "#f-title");
  if (first) setTimeout(() => first.focus(), 0);
  if (query && query.get("focus")) {
    const el = form.querySelector(`#f-${query.get("focus")}`);
    if (el) setTimeout(() => el.focus(), 0);
  }
  return page;
}
