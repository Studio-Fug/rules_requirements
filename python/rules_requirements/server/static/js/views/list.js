// SPDX-License-Identifier: AGPL-3.0-or-later
// One kind's entities: filterable, sortable table.

import { badge, button, empty, glyph, idTag, sortableTable, sourceRef } from "../components.js";
import { add, h, swap } from "../dom.js";
import { go, hashOf } from "../nav.js";
import { KIND, STATUS_ORDER, entitiesOf, statusClass } from "../store.js";

export async function renderList(kind, query) {
  const meta = KIND[kind];
  const rows = entitiesOf(kind);
  const page = h("div", { class: "page" });
  const filterText = query.get("q") || "";
  const active = new Set((query.get("status") || "").split(",").filter(Boolean));

  add(
    page,
    h(
      "header",
      { class: "page-head row" },
      h("h1", null, glyph(kind), meta.many, h("span", { class: "count" }, String(rows.length))),
      button(`New ${meta.one.toLowerCase()}`, () => go(`#/new/${kind}`), { primary: true }),
    ),
  );

  if (!rows.length) {
    add(
      page,
      empty(
        `No ${meta.many.toLowerCase()} yet.`,
        button(`Create the first ${meta.one.toLowerCase()}`, () => go(`#/new/${kind}`), { primary: true }),
      ),
    );
    return page;
  }

  const input = h("input", {
    type: "search",
    value: filterText,
    placeholder: `Filter ${meta.many.toLowerCase()} by id, title or tag`,
    "aria-label": "Filter",
  });
  const statuses = STATUS_ORDER[kind].filter((s) => rows.some((r) => r.status === s));
  const chipBar = h("div", { class: "chips", role: "group", "aria-label": "Filter by status" });

  const columns = [
    { key: "id", label: "Id", render: (r) => idTag(r.id), class: "c-id" },
    {
      key: "title",
      label: kind === "risk" ? "Risk" : "Title",
      render: (r) =>
        h(
          "div",
          null,
          h("a", { class: "row-title", href: `#/entity/${encodeURIComponent(r.id)}` }, r.title),
          r.tags && r.tags.length
            ? h(
                "div",
                { class: "tags" },
                r.tags.map((t) => h("span", { class: "tag" }, t)),
              )
            : null,
        ),
    },
  ];
  if (kind === "risk") {
    columns.push({
      key: "estimate",
      label: "Estimate",
      sort: (r) => `${r.data.severity || ""} ${r.data.likelihood || ""}`,
      render: (r) => h("span", { class: "estimate" }, r.data.severity || "?", " × ", r.data.likelihood || "?"),
    });
  }
  if (kind === "requirement") {
    columns.push({
      key: "method",
      label: "Demands",
      sort: (r) => r.data.method || "",
      render: (r) =>
        r.data.method ? h("span", { class: "level" }, r.data.method) : h("span", { class: "muted" }, "default"),
    });
  }
  if (kind === "test_method") {
    columns.push({
      key: "level",
      label: "Level",
      sort: (r) => r.data.level || "",
      render: (r) => h("span", { class: "level" }, r.data.level || "—"),
    });
  }
  columns.push(
    {
      key: "status",
      label: "Status",
      sort: (r) => STATUS_ORDER[kind].indexOf(r.status),
      render: (r) => badge(r.status),
    },
    {
      key: "open_notes",
      label: "Notes",
      sort: (r) => r.open_notes,
      render: (r) => (r.open_notes ? h("span", { class: "pill" }, String(r.open_notes)) : ""),
      class: "num",
    },
    {
      key: "issues",
      label: "Issues",
      sort: (r) => r.issues,
      render: (r) => (r.issues ? h("span", { class: "pill warn" }, String(r.issues)) : ""),
      class: "num",
    },
    {
      key: "path",
      label: "Defined in",
      sort: (r) => `${r.path}:${String(r.line).padStart(6, "0")}`,
      render: (r) => sourceRef(r.path, r.line),
      class: "c-src",
    },
  );

  function filtered() {
    const q = input.value.trim().toLowerCase();
    return rows.filter(
      (r) =>
        (!active.size || active.has(r.status)) &&
        (!q ||
          r.id.toLowerCase().includes(q) ||
          (r.title || "").toLowerCase().includes(q) ||
          (r.tags || []).some((t) => t.toLowerCase().includes(q))),
    );
  }

  const table = sortableTable({ columns, rows: filtered(), emptyText: "No entities match the filter." });

  function drawChips() {
    swap(
      chipBar,
      ...statuses.map((s) => {
        const n = rows.filter((r) => r.status === s).length;
        return h(
          "button",
          {
            type: "button",
            class: ["chip", "toggle", `st-${statusClass(s)}`, active.has(s) ? "on" : ""],
            "aria-pressed": active.has(s) ? "true" : "false",
            onClick: () => {
              if (active.has(s)) active.delete(s);
              else active.add(s);
              drawChips();
              update();
            },
          },
          h("span", { class: `swatch st-${statusClass(s)}` }),
          `${s.toLowerCase()} ${n}`,
        );
      }),
    );
  }

  function update() {
    table.update(filtered());
    const q = input.value.trim();
    const hash = hashOf(meta.route, { q, status: [...active].join(",") });
    history.replaceState(null, "", hash);
  }

  input.addEventListener("input", update);
  drawChips();
  add(page, h("div", { class: "toolbar" }, input, chipBar), table);
  return page;
}
