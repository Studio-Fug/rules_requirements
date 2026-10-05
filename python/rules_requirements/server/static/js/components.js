// SPDX-License-Identifier: AGPL-3.0-or-later
// Shared UI pieces: kind glyphs, status badges, id tags, pickers, dialogs,
// toasts and the code viewer.

import { get, enc } from "./api.js";
import { add, h, natural, s, swap } from "./dom.js";
import { KIND, store, statusClass } from "./store.js";

// --------------------------------------------------------------------------
// Shape = kind. Color = status.
// --------------------------------------------------------------------------

const SHAPES = {
  user_need: () => s("rect", { x: 1.5, y: 3.5, width: 13, height: 9, rx: 4.5 }),
  requirement: () => s("rect", { x: 2.5, y: 2.5, width: 11, height: 11, rx: 1.6 }),
  mitigation: () => s("polygon", { points: "8,1.6 13.6,4.8 13.6,11.2 8,14.4 2.4,11.2 2.4,4.8" }),
  risk: () => s("polygon", { points: "8,1.2 14.8,8 8,14.8 1.2,8" }),
  test_method: () => s("path", { d: "M3.5 1.8h6l3 3v9.4h-9z M9.5 1.8v3h3" }),
};

/** Just the kind's shape (for stamping into other SVGs). */
export function glyphShape(kind) {
  return (SHAPES[kind] || SHAPES.requirement)();
}

// Line icons for the non-model sections, drawn in the same stroke style.
const ICONS = {
  graph: () => [
    s("circle", { cx: 3.5, cy: 4, r: 2 }),
    s("circle", { cx: 12.5, cy: 4, r: 2 }),
    s("circle", { cx: 8, cy: 12.5, r: 2 }),
    s("path", { d: "M5.5 4h5M4.6 5.8l2.4 4.9M11.4 5.8L9 10.7" }),
  ],
  code: () => [s("path", { d: "M5.5 4L1.8 8l3.7 4M10.5 4l3.7 4-3.7 4" })],
  queue: () => [s("path", { d: "M2 4h12M2 8h12M2 12h7" })],
  versions: () => [
    s("circle", { cx: 4, cy: 3.5, r: 1.8 }),
    s("circle", { cx: 4, cy: 12.5, r: 1.8 }),
    s("circle", { cx: 12, cy: 5.5, r: 1.8 }),
    s("path", { d: "M4 5.3v5.4M12 7.3c0 2.6-3.2 2.4-6.4 4.2" }),
  ],
  cases: () => [
    s("path", { d: "M2.5 3.5h3M2.5 8h3M2.5 12.5h3" }),
    s("path", { d: "M7.5 3.5l3 0M7.5 8h3M7.5 12.5h3" }),
    s("circle", { cx: 13, cy: 8, r: 1.6 }),
  ],
  agents: () => [s("circle", { cx: 6.8, cy: 6.8, r: 4.3 }), s("path", { d: "M10 10l4.2 4.2M4.9 6.9l1.4 1.4 2.6-2.8" })],
};

export function icon(name) {
  return s(
    "svg",
    { class: "glyph icon", viewBox: "0 0 16 16", width: 16, height: 16, "aria-hidden": "true" },
    (ICONS[name] || ICONS.queue)(),
  );
}

export function glyph(kind, status = "", label = "") {
  const shape = (SHAPES[kind] || SHAPES.requirement)();
  return s(
    "svg",
    {
      class: `glyph glyph-${kind} st-${statusClass(status)}`,
      viewBox: "0 0 16 16",
      width: 16,
      height: 16,
      role: label ? "img" : null,
      "aria-label": label || null,
      "aria-hidden": label ? null : "true",
    },
    shape,
  );
}

export function badge(status, extra = "") {
  if (!status) return h("span", { class: "badge st-none" }, "—");
  return h("span", { class: `badge st-${statusClass(status)} ${extra}` }, status);
}

/** A link to an entity: glyph (shape = kind, fill = status) + id. */
export function idTag(id, opts = {}) {
  const row = store.byId.get(id);
  const kind = opts.kind || (row && row.kind) || "requirement";
  const status = opts.status !== undefined ? opts.status : (row && row.status) || "";
  const missing = !row && !opts.kind;
  const title = opts.title !== undefined ? opts.title : row ? row.title : "not defined in the model";
  const inner = [glyph(kind, status), h("span", { class: "idtag-id" }, id)];
  if (opts.showTitle && title) inner.push(h("span", { class: "idtag-title" }, title));
  const cls = ["idtag", missing ? "missing" : "", opts.compact ? "compact" : ""];
  if (opts.link === false || missing) return h("span", { class: cls, title }, inner);
  return h(
    "a",
    { class: cls, href: `#/entity/${enc(id)}`, title: `${id}: ${title}${status ? ` (${status})` : ""}` },
    inner,
  );
}

export function kindLabel(kind, many = false) {
  const k = KIND[kind];
  return k ? (many ? k.many : k.one) : kind;
}

export function sourceRef(path, line) {
  const text = line ? `${path}:${line}` : path;
  return h(
    "a",
    { class: "src", href: `#/code?path=${enc(path)}${line ? `&line=${line}` : ""}`, title: "Open in the code viewer" },
    text,
  );
}

export function empty(message, action = null) {
  return h("div", { class: "empty" }, h("p", null, message), action);
}

export function loading(text = "Loading…") {
  return h("p", { class: "loading" }, text);
}

export function section(title, ...children) {
  return h("section", { class: "block" }, h("h2", null, title), ...children);
}

export function button(label, onClick, opts = {}) {
  return h(
    "button",
    {
      type: opts.type || "button",
      class: [
        "btn",
        opts.primary ? "primary" : "",
        opts.danger ? "danger" : "",
        opts.quiet ? "quiet" : "",
        opts.small ? "small" : "",
        opts.class || "",
      ],
      onClick,
      disabled: opts.disabled || false,
      title: opts.title || null,
      "aria-label": opts.ariaLabel || null,
    },
    label,
  );
}

// --------------------------------------------------------------------------
// Toasts
// --------------------------------------------------------------------------

export function toast(message, kind = "ok", ms = 4200) {
  const host = document.getElementById("toasts");
  const el = h("div", { class: `toast ${kind}`, role: kind === "error" ? "alert" : "status" }, message);
  add(host, el);
  while (host.children.length > 4) host.firstElementChild.remove();
  setTimeout(
    () => {
      el.classList.add("leaving");
      setTimeout(() => el.remove(), 250);
    },
    kind === "error" ? Math.max(ms, 7000) : ms,
  );
}

export function reportError(err) {
  // eslint-disable-next-line no-console
  if (!err || err.status === undefined) console.warn(err);
  toast(err && err.message ? err.message : String(err), "error");
}

// --------------------------------------------------------------------------
// Dialogs
// --------------------------------------------------------------------------

/**
 * openDialog({title, body, actions: [{label, primary, danger, run}]})
 * `run` may return false to keep the dialog open. Resolves with the label of
 * the action taken, or null when dismissed.
 */
export function openDialog({ title, body, actions = [], wide = false }) {
  return new Promise((resolve) => {
    const dlg = h("dialog", { class: ["dialog", wide ? "wide" : ""], "aria-label": title });
    const error = h("p", { class: "form-error", hidden: true });
    const buttons = actions.map((a) =>
      button(
        a.label,
        async () => {
          error.hidden = true;
          try {
            const keep = a.run ? await a.run() : undefined;
            if (keep === false) return;
            dlg.close(a.label);
          } catch (err) {
            error.textContent = err.message || String(err);
            error.hidden = false;
          }
        },
        { primary: a.primary, danger: a.danger },
      ),
    );
    add(
      dlg,
      h("h2", null, title),
      h("div", { class: "dialog-body" }, body),
      error,
      h(
        "div",
        { class: "dialog-actions" },
        button("Cancel", () => dlg.close(""), { quiet: true }),
        buttons,
      ),
    );
    dlg.addEventListener("close", () => {
      resolve(dlg.returnValue || null);
      dlg.remove();
    });
    document.body.append(dlg);
    dlg.showModal();
    const first = dlg.querySelector("input, textarea, select");
    if (first) first.focus();
  });
}

export async function confirmDialog(title, message, confirmLabel = "Confirm", danger = false) {
  const result = await openDialog({
    title,
    body: h("p", null, message),
    actions: [{ label: confirmLabel, primary: !danger, danger }],
  });
  return result === confirmLabel;
}

export async function promptDialog(title, label, initial = "", confirmLabel = "Save", hint = "") {
  const input = h("input", { type: "text", value: initial, "aria-label": label });
  let value = null;
  const result = await openDialog({
    title,
    body: h("label", { class: "field" }, h("span", null, label), input, hint ? h("small", null, hint) : null),
    actions: [
      {
        label: confirmLabel,
        primary: true,
        run: () => {
          value = input.value.trim();
        },
      },
    ],
  });
  return result === confirmLabel ? value : null;
}

// --------------------------------------------------------------------------
// Id picker: chips + type-ahead
// --------------------------------------------------------------------------

/**
 * idPicker({kinds, value, single, placeholder, label}) -> element with
 * `.getValue()` returning the selected ids.
 */
export function idPicker({ kinds, value = [], single = false, placeholder = "Type an id or title…", label = "" }) {
  let selected = Array.isArray(value) ? value.slice() : value ? [value] : [];
  const chips = h("span", { class: "picker-chips" });
  const input = h("input", {
    type: "text",
    placeholder,
    "aria-label": label || placeholder,
    autocomplete: "off",
    role: "combobox",
    "aria-expanded": "false",
  });
  const list = h("ul", { class: "picker-list", role: "listbox", hidden: true });
  const root = h("div", { class: "picker" }, chips, input, list);
  let active = -1;
  let options = [];

  function renderChips() {
    swap(
      chips,
      ...selected.map((id) =>
        h(
          "span",
          { class: "chip" },
          idTag(id, { link: false, compact: true }),
          h(
            "button",
            { type: "button", class: "chip-x", "aria-label": `Remove ${id}`, onClick: () => remove(id) },
            "×",
          ),
        ),
      ),
    );
    input.placeholder = single && selected.length ? "" : placeholder;
  }

  function candidates() {
    const q = input.value.trim().toLowerCase();
    return store.entities
      .filter((e) => kinds.includes(e.kind) && !selected.includes(e.id))
      .filter((e) => !q || e.id.toLowerCase().includes(q) || (e.title || "").toLowerCase().includes(q))
      .sort((a, b) => {
        const ap = a.id.toLowerCase().startsWith(q) ? 0 : 1;
        const bp = b.id.toLowerCase().startsWith(q) ? 0 : 1;
        return ap - bp || natural(a.id, b.id);
      })
      .slice(0, 40);
  }

  function renderList() {
    options = candidates();
    active = options.length ? Math.max(0, Math.min(active, options.length - 1)) : -1;
    swap(
      list,
      ...options.map((e, i) =>
        h(
          "li",
          {
            role: "option",
            class: i === active ? "active" : "",
            "aria-selected": i === active ? "true" : "false",
            onMousedown: (ev) => {
              ev.preventDefault();
              add(e.id);
            },
          },
          idTag(e.id, { link: false, compact: true }),
          h("span", { class: "opt-title" }, e.title),
        ),
      ),
    );
    if (!options.length) add(list, h("li", { class: "none" }, "No matches"));
    list.hidden = false;
    input.setAttribute("aria-expanded", "true");
  }

  function close() {
    list.hidden = true;
    input.setAttribute("aria-expanded", "false");
  }

  function add(id) {
    if (!selected.includes(id)) selected = single ? [id] : [...selected, id];
    input.value = "";
    renderChips();
    if (single) close();
    else renderList();
    root.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function remove(id) {
    selected = selected.filter((x) => x !== id);
    renderChips();
    root.dispatchEvent(new Event("change", { bubbles: true }));
    input.focus();
  }

  input.addEventListener("focus", renderList);
  input.addEventListener("input", () => {
    active = 0;
    renderList();
  });
  input.addEventListener("blur", () => setTimeout(close, 120));
  input.addEventListener("keydown", (ev) => {
    if (ev.key === "ArrowDown") {
      ev.preventDefault();
      if (list.hidden) renderList();
      active = Math.min(options.length - 1, active + 1);
      renderList();
    } else if (ev.key === "ArrowUp") {
      ev.preventDefault();
      active = Math.max(0, active - 1);
      renderList();
    } else if (ev.key === "Enter") {
      if (!list.hidden && options[active]) {
        ev.preventDefault();
        add(options[active].id);
      } else if (input.value.trim() && /^[A-Za-z]+-\d+$/.test(input.value.trim())) {
        ev.preventDefault();
        add(input.value.trim());
      }
    } else if (ev.key === "Escape") {
      if (!list.hidden) {
        ev.stopPropagation();
        close();
      }
    } else if (ev.key === "Backspace" && !input.value && selected.length) {
      remove(selected[selected.length - 1]);
    }
  });
  root.addEventListener("click", (ev) => {
    if (ev.target === root || ev.target === chips) input.focus();
  });

  root.getValue = () => selected.slice();
  renderChips();
  return root;
}

// --------------------------------------------------------------------------
// Code viewer
// --------------------------------------------------------------------------

const sourceCache = new Map();

export async function fetchSource(path) {
  if (!sourceCache.has(path))
    sourceCache.set(
      path,
      get(`/api/source?path=${enc(path)}`).then((r) => r.lines),
    );
  try {
    return await sourceCache.get(path);
  } catch (err) {
    sourceCache.delete(path);
    throw err;
  }
}

export function clearSourceCache() {
  sourceCache.clear();
}

/** A scrollable, line-numbered view of `path` with `line` highlighted. */
export function codeViewer(path, line = 0, { height = "", onClose = null } = {}) {
  const body = h("div", { class: "code-body", tabindex: "0", "aria-label": `Source of ${path}` }, loading());
  if (height) body.style.maxHeight = height;
  const head = h(
    "div",
    { class: "code-head" },
    h("span", { class: "code-path" }, path, line ? h("span", { class: "code-line" }, `:${line}`) : null),
    onClose ? button("Close", onClose, { quiet: true, small: true }) : null,
  );
  const root = h("div", { class: "code" }, head, body);
  fetchSource(path)
    .then((lines) => {
      const pre = h("pre", { class: "code-pre" });
      const width = String(lines.length).length;
      lines.forEach((text, i) => {
        const n = i + 1;
        add(
          pre,
          h(
            "span",
            { class: ["code-row", n === line ? "hit" : ""] },
            h("span", { class: "code-n", "aria-hidden": "true" }, String(n).padStart(width, " ")),
            h("span", { class: "code-t" }, text || " "),
          ),
        );
      });
      swap(body, pre);
      if (line) {
        requestAnimationFrame(() => {
          const hit = pre.children[line - 1];
          if (hit) body.scrollTop = Math.max(0, hit.offsetTop - body.clientHeight / 3);
        });
      }
    })
    .catch((err) => swap(body, h("p", { class: "form-error" }, err.message)));
  return root;
}

// --------------------------------------------------------------------------
// Sortable table
// --------------------------------------------------------------------------

/**
 * sortableTable({columns: [{key, label, sort?, render, class?}], rows, initial, emptyText})
 * `sort(row)` returns the sort key; `render(row)` returns a node or text.
 */
export function sortableTable({ columns, rows, initial = null, emptyText = "Nothing to show.", rowAttrs = null }) {
  let sortKey = initial ? initial.key : columns[0].key;
  let dir = initial ? initial.dir : 1;
  const table = h("table", { class: "grid" });
  const thead = h("thead");
  const tbody = h("tbody");
  add(table, thead, tbody);

  function render() {
    swap(
      thead,
      h(
        "tr",
        null,
        columns.map((c) => {
          const sorted = c.key === sortKey;
          const th = h("th", {
            scope: "col",
            class: c.class || "",
            "aria-sort": sorted ? (dir > 0 ? "ascending" : "descending") : "none",
          });
          if (c.sort === false) add(th, c.label);
          else
            add(
              th,
              h(
                "button",
                {
                  type: "button",
                  class: "th-sort",
                  onClick: () => {
                    if (sortKey === c.key) dir = -dir;
                    else {
                      sortKey = c.key;
                      dir = 1;
                    }
                    render();
                  },
                },
                c.label,
                h("span", { class: "sort-mark", "aria-hidden": "true" }, sorted ? (dir > 0 ? "▲" : "▼") : ""),
              ),
            );
          return th;
        }),
      ),
    );
    const col = columns.find((c) => c.key === sortKey) || columns[0];
    const keyOf = col.sort || ((r) => r[col.key]);
    const sorted = rows.slice().sort((a, b) => {
      const ka = keyOf(a);
      const kb = keyOf(b);
      if (typeof ka === "number" && typeof kb === "number") return (ka - kb) * dir;
      return natural(ka ?? "", kb ?? "") * dir;
    });
    swap(
      tbody,
      ...(sorted.length
        ? sorted.map((r) =>
            h(
              "tr",
              rowAttrs ? rowAttrs(r) : null,
              columns.map((c) => h("td", { class: c.class || "" }, c.render ? c.render(r) : r[c.key])),
            ),
          )
        : [h("tr", null, h("td", { colspan: columns.length, class: "empty-cell" }, emptyText))]),
    );
  }
  render();
  const wrap = h("div", { class: "table-wrap" }, table);
  wrap.update = (newRows) => {
    rows = newRows;
    render();
  };
  return wrap;
}

// --------------------------------------------------------------------------
// Misc
// --------------------------------------------------------------------------

export async function copyText(text, what = "Copied") {
  try {
    await navigator.clipboard.writeText(text);
    toast(what);
  } catch {
    const area = h("textarea", { class: "copy-area", readonly: true, rows: 12 }, text);
    await openDialog({ title: "Copy", body: h("div", null, h("p", null, "Copy the text below."), area), actions: [] });
  }
}
