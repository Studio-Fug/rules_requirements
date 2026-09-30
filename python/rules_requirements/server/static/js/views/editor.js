// SPDX-License-Identifier: AGPL-3.0-or-later
// Create and edit form for every entity kind.

import { enc, get, post, put, settings } from "../api.js";
import { button, glyph, idPicker, reportError, toast } from "../components.js";
import { add, h } from "../dom.js";
import { go, reloadModel } from "../nav.js";
import { KIND, config, entitiesOf, levelNames, store } from "../store.js";

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
              label: "Verified by targets",
              type: "verified_by",
              hint: "Whole test targets (e.g. Bazel labels) that verify this requirement, for suites without per-case tags.",
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

function control(field, value) {
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
      const addRow = (target = "", level = "") => {
        const t = h("input", {
          type: "text",
          value: target,
          placeholder: "//pkg:test_target",
          "aria-label": "Test target",
        });
        const l = h(
          "select",
          { "aria-label": "Level it provides" },
          h("option", { value: "" }, "default level"),
          levelNames().map((n) => h("option", { value: n, selected: n === level }, n)),
        );
        const row = h(
          "div",
          { class: "vb-row" },
          t,
          l,
          button("Remove", () => row.remove(), { small: true, quiet: true }),
        );
        row.read = () =>
          t.value.trim() ? (l.value ? { target: t.value.trim(), level: l.value } : t.value.trim()) : null;
        add(rowsHost, row);
        return t;
      };
      for (const v of value || []) {
        if (typeof v === "string") addRow(v);
        else addRow(v.target, v.level || "");
      }
      const el = h(
        "div",
        { class: "vb", id },
        rowsHost,
        button("Add target", () => addRow().focus(), { small: true }),
      );
      el.read = () => [...rowsHost.children].map((r) => r.read()).filter(Boolean);
      return [labelEl(el), el];
    }
    default:
      return [labelEl(h("span", null, "?")), { read: () => value }];
  }
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
  const fields = [];

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
      const [wrapper, el] = control(field, base[field.key]);
      fields.push([field, el]);
      add(fs, wrapper);
    }
    add(form, fs);
  }

  const saveLabel = existing ? "Save changes" : `Create ${meta.one.toLowerCase()}`;
  const submit = h("button", { type: "submit", class: "btn primary" }, saveLabel);
  add(
    form,
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

  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    errorBox.hidden = true;
    const data = { ...base };
    delete data.notes;
    for (const [field, el] of fields) {
      const v = el.read();
      if (v === "" || v === null || v === undefined || (Array.isArray(v) && !v.length)) delete data[field.key];
      else data[field.key] = v;
    }
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
          await put(`/api/entities/${enc(existing.id)}`, { data, version: existing.version });
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
  const first = form.querySelector(existing ? "#f-title" : "#f-title");
  if (first) setTimeout(() => first.focus(), 0);
  if (query && query.get("focus")) {
    const el = form.querySelector(`#f-${query.get("focus")}`);
    if (el) setTimeout(() => el.focus(), 0);
  }
  return page;
}
