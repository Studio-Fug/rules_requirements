// SPDX-License-Identifier: AGPL-3.0-or-later
// Work queue: every gap and open note, routed to an agent or a human.

import { get } from "../api.js";
import { button, copyText, empty, idTag, sortableTable } from "../components.js";
import { add, h, plural } from "../dom.js";
import { hashOf } from "../nav.js";
import { store } from "../store.js";

export async function renderQueue(query) {
  const res = await get("/api/queue");
  const all = res.queue || [];
  const page = h("div", { class: "page queue" });
  add(
    page,
    h(
      "header",
      { class: "page-head row" },
      h(
        "div",
        null,
        h("h1", null, "Work queue", h("span", { class: "count" }, String(all.length))),
        h(
          "p",
          { class: "lede" },
          "Everything between the model and a complete verification argument. Agent items can be closed by writing tests or code; human-gate items need a bench, a reviewer or a decision.",
        ),
      ),
      h(
        "div",
        { class: "actions" },
        button("Copy as JSON", () =>
          copyText(JSON.stringify({ queue: visible() }, null, 2), `${plural(visible().length, "item")} copied as JSON`),
        ),
      ),
    ),
  );
  if (!all.length) {
    add(page, empty("The queue is empty: every requirement is verified at its demanded level and no notes are open."));
    return page;
  }
  const kinds = [...new Set(all.map((g) => g.kind))].sort();
  const kindSel = h(
    "select",
    { "aria-label": "Gap kind" },
    h("option", { value: "" }, "All kinds"),
    kinds.map((k) =>
      h("option", { value: k, selected: k === query.get("kind") }, `${k} (${all.filter((g) => g.kind === k).length})`),
    ),
  );
  const routeSel = h(
    "select",
    { "aria-label": "Route" },
    [
      ["", "Any route"],
      ["autonomous", "Agent can close"],
      ["human-gate", "Needs a human"],
    ].map(([v, l]) => h("option", { value: v, selected: v === (query.get("route") || "") }, l)),
  );
  const text = h("input", {
    type: "search",
    value: query.get("q") || "",
    placeholder: "Filter by id or text",
    "aria-label": "Filter",
  });

  function visible() {
    const q = text.value.trim().toLowerCase();
    return all.filter(
      (g) =>
        (!kindSel.value || g.kind === kindSel.value) &&
        (!routeSel.value || g.route === routeSel.value) &&
        (!q || g.entity.toLowerCase().includes(q) || g.message.toLowerCase().includes(q)),
    );
  }
  const table = sortableTable({
    columns: [
      {
        key: "route",
        label: "Route",
        render: (g) => h("span", { class: ["route", g.route] }, g.route === "human-gate" ? "human gate" : "agent"),
      },
      { key: "kind", label: "Kind", render: (g) => h("code", { class: "code-tag" }, g.kind) },
      {
        key: "entity",
        label: "Entity",
        render: (g) =>
          store.byId.has(g.entity)
            ? idTag(g.entity, { showTitle: true })
            : h("span", { class: "idtag missing" }, g.entity),
      },
      {
        key: "message",
        label: "Detail",
        render: (g) =>
          h(
            "span",
            null,
            g.message,
            g.demanded_level
              ? h(
                  "span",
                  { class: "muted" },
                  ` (demands ${g.demanded_level}${g.provided_level ? `, has ${g.provided_level}` : ""})`,
                )
              : null,
          ),
      },
    ],
    rows: visible(),
    emptyText: "Nothing matches the filter.",
  });
  const update = () => {
    table.update(visible());
    history.replaceState(
      null,
      "",
      hashOf("queue", { kind: kindSel.value, route: routeSel.value, q: text.value.trim() }),
    );
  };
  kindSel.addEventListener("change", update);
  routeSel.addEventListener("change", update);
  text.addEventListener("input", update);
  add(page, h("div", { class: "toolbar" }, text, kindSel, routeSel), table);
  return page;
}
