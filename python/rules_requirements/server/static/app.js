// SPDX-License-Identifier: AGPL-3.0-or-later
// rr serve — requirements workbench. Entry point: shell, router, search.

import { captureToken, post, settings } from "./js/api.js";
import { button, clearSourceCache, glyph, icon, idTag, openDialog, reportError, toast } from "./js/components.js";
import { h, mount, plural, swap } from "./js/dom.js";
import { KIND, KIND_BY_ROUTE, KINDS, refresh, store } from "./js/store.js";
import { stopPolling } from "./js/views/agents.js";
import { renderAgents } from "./js/views/agents.js";
import { renderCode } from "./js/views/code.js";
import { renderEditor } from "./js/views/editor.js";
import { renderEntity } from "./js/views/entity.js";
import { renderGraph } from "./js/views/graph.js";
import { renderList } from "./js/views/list.js";
import { renderOverview } from "./js/views/overview.js";
import { renderQueue } from "./js/views/queue.js";
import { renderVersions } from "./js/views/versions.js";
import { go, parseRoute, registerHooks, reloadModel } from "./js/nav.js";

// --------------------------------------------------------------------------
// Routing: #/<section>[/<arg>][?query]
// --------------------------------------------------------------------------

let renderSeq = 0;

async function render(opts = {}) {
  const seq = ++renderSeq;
  const keepScroll = Boolean(opts && opts.keepScroll === true);
  const main = document.getElementById("main");
  const scroll = main.scrollTop;
  const { parts, query } = parseRoute();
  const [section = "overview", arg = ""] = parts;
  markNav(section, arg);
  document.body.classList.remove("rail-open");
  if (section !== "agents") stopPolling();
  let view;
  try {
    if (section === "overview") view = await renderOverview();
    else if (KIND_BY_ROUTE[section]) view = await renderList(KIND_BY_ROUTE[section], query);
    else if (section === "entity" && arg) view = await renderEntity(arg);
    else if (section === "new" && KIND[arg]) view = await renderEditor({ kind: arg, query });
    else if (section === "edit" && arg) view = await renderEditor({ id: arg, query });
    else if (section === "graph") view = await renderGraph(query);
    else if (section === "code") view = await renderCode(query);
    else if (section === "versions") view = await renderVersions(query);
    else if (section === "agents") view = await renderAgents(query);
    else if (section === "queue") view = await renderQueue(query);
    else
      view = h(
        "div",
        { class: "page" },
        h("h1", null, "Not found"),
        h("p", null, "There is no page at this address. ", h("a", { href: "#/" }, "Go to the overview.")),
      );
  } catch (err) {
    reportError(err);
    view = h(
      "div",
      { class: "page" },
      h("h1", null, "This page could not load"),
      h("p", { class: "form-error" }, err.message || String(err)),
    );
  }
  if (seq !== renderSeq) return; // a newer navigation won
  mount(main, view);
  main.scrollTop = keepScroll ? scroll : 0;
  const heading = main.querySelector("h1");
  document.title = `${heading ? heading.textContent : "Workbench"} · ${projectName()}`;
}

function projectName() {
  const p = (store.state && store.state.project) || {};
  return p.name || "Requirements";
}

// --------------------------------------------------------------------------
// Chrome
// --------------------------------------------------------------------------

const NAV = [
  { group: "Model", items: [...KINDS.map((k) => ({ route: KIND[k].route, label: KIND[k].many, kind: k }))] },
  {
    group: "Trace",
    items: [
      { route: "graph", label: "Trace graph", icon: "graph" },
      { route: "code", label: "Implementation", icon: "code" },
      { route: "queue", label: "Work queue", icon: "queue" },
    ],
  },
  {
    group: "Change",
    items: [
      { route: "versions", label: "Versions", icon: "versions" },
      { route: "agents", label: "Agents", icon: "agents" },
    ],
  },
];

function navCount(item) {
  if (item.kind) return store.entities.filter((e) => e.kind === item.kind).length;
  if (item.route === "queue") return (store.state && store.state.counts && store.state.counts.gaps) || 0;
  if (item.route === "versions") return (store.state && store.state.git && (store.state.git.changed || []).length) || 0;
  return null;
}

function drawRail() {
  const rail = document.getElementById("rail");
  mount(
    rail,
    h(
      "a",
      { href: "#/overview", class: "nav-item nav-overview", dataset: { route: "overview" } },
      h("span", { class: "nav-label" }, "Overview"),
    ),
    NAV.map((g) =>
      h(
        "div",
        { class: "nav-group" },
        h("p", { class: "nav-group-label" }, g.group),
        g.items.map((item) => {
          const count = navCount(item);
          return h(
            "a",
            { href: `#/${item.route}`, class: "nav-item", dataset: { route: item.route } },
            item.kind ? glyph(item.kind) : icon(item.icon),
            h("span", { class: "nav-label" }, item.label),
            count !== null
              ? h("span", { class: ["nav-count", item.route === "versions" && count ? "hot" : ""] }, String(count))
              : null,
          );
        }),
      ),
    ),
    h(
      "p",
      { class: "rail-foot" },
      "rules_requirements",
      h("br"),
      h("a", { href: "/api/report", target: "_blank", rel: "noopener" }, "Report JSON"),
    ),
  );
}

function markNav(section, arg) {
  let route = section;
  if (section === "entity" || section === "edit") {
    const row = store.byId.get(arg);
    route = row ? KIND[row.kind].route : "";
  } else if (section === "new") route = KIND[arg] ? KIND[arg].route : "";
  for (const a of document.querySelectorAll(".nav-item")) {
    const on = a.dataset.route === route;
    a.classList.toggle("current", on);
    if (on) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  }
}

function drawTopbar() {
  const bar = document.getElementById("topbar");
  const st = store.state || {};
  const git = st.git || {};
  const changed = (git.changed || []).length;
  const author = settings.author || st.author || "";
  mount(
    bar,
    button("Menu", () => document.body.classList.toggle("rail-open"), {
      quiet: true,
      small: true,
      class: "menu-btn",
      ariaLabel: "Toggle navigation",
    }),
    h(
      "a",
      { class: "brand", href: "#/overview" },
      h("span", { class: "brand-mark", "aria-hidden": "true" }, glyph("requirement", "VERIFIED")),
      h("span", null, projectName()),
    ),
    git.git
      ? h(
          "span",
          { class: "branch", title: `HEAD ${git.head}` },
          h("span", { class: "branch-name" }, git.branch || "detached"),
          changed
            ? h("a", { class: "chip hot", href: "#/versions" }, `${plural(changed, "uncommitted model change")}`)
            : h("span", { class: "chip" }, "clean"),
        )
      : h("span", { class: "branch muted" }, "not a git checkout"),
    searchBox(),
    h(
      "button",
      { type: "button", class: "btn quiet small author", onClick: editAuthor, title: "Recorded on notes and commits" },
      author ? `You are ${author.replace(/\s*<.*>/, "")}` : "Set your name",
    ),
    button("Reload", reloadFromDisk, { small: true, title: "Re-read model files, evidence and annotations from disk" }),
  );
}

function drawChrome() {
  drawTopbar();
  drawRail();
  const { parts } = parseRoute();
  markNav(parts[0] || "overview", parts[1] || "");
}

async function reloadFromDisk() {
  try {
    await post("/api/reload");
    clearSourceCache();
    await reloadModel();
    toast("Reloaded from disk");
  } catch (err) {
    reportError(err);
  }
}

async function editAuthor() {
  const input = h("input", {
    type: "text",
    value: settings.author || (store.state && store.state.author) || "",
    placeholder: "Ada Lovelace <ada@example.com>",
    "aria-label": "Your name and email",
  });
  const res = await openDialog({
    title: "Who is editing?",
    body: h(
      "label",
      { class: "field" },
      h("span", null, "Name and email"),
      input,
      h("small", null, "Stored in this browser. Used as the author of notes and model commits."),
    ),
    actions: [{ label: "Save", primary: true, run: () => (settings.author = input.value) }],
  });
  if (res) drawTopbar();
}

// --------------------------------------------------------------------------
// Search: ids and titles, `/` to focus
// --------------------------------------------------------------------------

function searchBox() {
  const input = h("input", {
    type: "search",
    id: "search",
    placeholder: "Search ids and titles  ( / )",
    "aria-label": "Search the model",
    autocomplete: "off",
  });
  const list = h("ul", { class: "search-list", role: "listbox", hidden: true });
  let hits = [];
  let active = 0;
  function update() {
    const q = input.value.trim().toLowerCase();
    if (!q) {
      list.hidden = true;
      return;
    }
    hits = store.entities
      .filter((e) => e.id.toLowerCase().includes(q) || (e.title || "").toLowerCase().includes(q))
      .sort((a, b) => (a.id.toLowerCase() === q ? -1 : b.id.toLowerCase() === q ? 1 : 0))
      .slice(0, 12);
    active = Math.min(active, Math.max(0, hits.length - 1));
    swap(
      list,
      ...(hits.length
        ? hits.map((e, i) =>
            h(
              "li",
              {
                role: "option",
                class: i === active ? "active" : "",
                "aria-selected": i === active ? "true" : "false",
                onMousedown: (ev) => {
                  ev.preventDefault();
                  choose(e.id);
                },
              },
              idTag(e.id, { link: false, compact: true }),
              h("span", { class: "opt-title" }, e.title),
            ),
          )
        : [h("li", { class: "none" }, "No matching ids or titles")]),
    );
    list.hidden = false;
  }
  function choose(id) {
    input.value = "";
    list.hidden = true;
    input.blur();
    go(`#/entity/${encodeURIComponent(id)}`);
  }
  input.addEventListener("input", () => {
    active = 0;
    update();
  });
  input.addEventListener("keydown", (ev) => {
    if (ev.key === "ArrowDown") {
      ev.preventDefault();
      active = Math.min(hits.length - 1, active + 1);
      update();
    } else if (ev.key === "ArrowUp") {
      ev.preventDefault();
      active = Math.max(0, active - 1);
      update();
    } else if (ev.key === "Enter" && hits[active]) {
      ev.preventDefault();
      choose(hits[active].id);
    } else if (ev.key === "Escape") {
      input.value = "";
      list.hidden = true;
      input.blur();
    }
  });
  input.addEventListener("blur", () => setTimeout(() => (list.hidden = true), 120));
  return h("div", { class: "search" }, input, list);
}

document.addEventListener("keydown", (ev) => {
  const t = ev.target;
  const typing =
    t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable);
  if (ev.key === "/" && !typing && !ev.metaKey && !ev.ctrlKey && !ev.altKey) {
    const box = document.getElementById("search");
    if (box) {
      ev.preventDefault();
      box.focus();
    }
  }
});

// --------------------------------------------------------------------------
// Boot
// --------------------------------------------------------------------------

async function boot() {
  captureToken();
  registerHooks({ render, chrome: drawChrome });
  try {
    await refresh();
  } catch (err) {
    mount(
      document.getElementById("main"),
      h(
        "div",
        { class: "page" },
        h("h1", null, "Cannot load the model"),
        h("p", { class: "form-error" }, err.message),
        h("p", null, "Check the terminal running rr serve, then reload this page."),
      ),
    );
    return;
  }
  drawChrome();
  window.addEventListener("hashchange", () => render());
  await render();
}

boot();
