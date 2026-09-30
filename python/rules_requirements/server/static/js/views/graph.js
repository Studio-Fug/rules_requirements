// SPDX-License-Identifier: AGPL-3.0-or-later
// Trace graph: the server's layered SVG with pan, zoom, focus and highlighting.

import { enc, get } from "../api.js";
import { button, copyText, empty, glyph, glyphShape, idPicker } from "../components.js";
import { add, h } from "../dom.js";
import { go, hashOf } from "../nav.js";
import { KIND, store } from "../store.js";

const GRAPH_KINDS = ["user_need", "requirement", "mitigation", "risk"];

function parseSvg(text) {
  const doc = new DOMParser().parseFromString(text, "image/svg+xml");
  const root = doc.documentElement;
  if (!root || root.nodeName !== "svg") return null;
  return document.importNode(root, true);
}

function enablePanZoom(viewport, svg) {
  const [x0, y0, w0, h0] = (svg.getAttribute("viewBox") || "0 0 100 100").split(/\s+/).map(Number);
  let vb = { x: x0, y: y0, w: w0, h: h0 };
  svg.removeAttribute("width");
  svg.removeAttribute("height");
  const apply = () => svg.setAttribute("viewBox", `${vb.x} ${vb.y} ${vb.w} ${vb.h}`);
  const fit = () => {
    vb = { x: x0 - 8, y: y0 - 8, w: w0 + 16, h: h0 + 16 };
    apply();
  };
  const zoom = (factor, cx = 0.5, cy = 0.5) => {
    const nw = Math.min(w0 * 6, Math.max(w0 / 12, vb.w * factor));
    const nh = (nw / vb.w) * vb.h;
    vb = { x: vb.x + (vb.w - nw) * cx, y: vb.y + (vb.h - nh) * cy, w: nw, h: nh };
    apply();
  };
  viewport.addEventListener(
    "wheel",
    (ev) => {
      ev.preventDefault();
      const r = svg.getBoundingClientRect();
      zoom(ev.deltaY > 0 ? 1.12 : 1 / 1.12, (ev.clientX - r.left) / r.width, (ev.clientY - r.top) / r.height);
    },
    { passive: false },
  );
  let drag = null;
  let moved = false;
  viewport.addEventListener("pointerdown", (ev) => {
    if (ev.button !== 0) return;
    drag = { x: ev.clientX, y: ev.clientY, vb: { ...vb } };
    moved = false;
  });
  viewport.addEventListener("pointermove", (ev) => {
    if (!drag) return;
    if (!moved && Math.abs(ev.clientX - drag.x) + Math.abs(ev.clientY - drag.y) > 4) {
      // Capture only once it is a real drag, so plain clicks still reach the nodes.
      moved = true;
      viewport.setPointerCapture(ev.pointerId);
      viewport.classList.add("dragging");
    }
    if (!moved) return;
    const r = svg.getBoundingClientRect();
    const dx = ((ev.clientX - drag.x) / r.width) * drag.vb.w;
    const dy = ((ev.clientY - drag.y) / r.height) * drag.vb.h;
    vb = { ...vb, x: drag.vb.x - dx, y: drag.vb.y - dy };
    apply();
  });
  const end = () => {
    drag = null;
    viewport.classList.remove("dragging");
  };
  viewport.addEventListener("pointerup", end);
  viewport.addEventListener("pointercancel", end);
  // A drag must not count as a click on the node it started on.
  viewport.addEventListener(
    "click",
    (ev) => {
      if (moved) {
        ev.preventDefault();
        ev.stopPropagation();
        moved = false;
      }
    },
    true,
  );
  viewport.addEventListener("keydown", (ev) => {
    const step = vb.w * 0.08;
    if (ev.key === "+" || ev.key === "=") zoom(1 / 1.2);
    else if (ev.key === "-") zoom(1.2);
    else if (ev.key === "0") fit();
    else if (ev.key === "ArrowLeft") vb.x -= step;
    else if (ev.key === "ArrowRight") vb.x += step;
    else if (ev.key === "ArrowUp") vb.y -= step;
    else if (ev.key === "ArrowDown") vb.y += step;
    else return;
    ev.preventDefault();
    apply();
  });
  fit();
  return { fit, zoom };
}

/** Put each node's kind shape in its top-right corner: shape = kind here too. */
function stampKinds(svg) {
  for (const g of svg.querySelectorAll("g.rr-node")) {
    const row = store.byId.get(g.dataset.id);
    const rect = g.querySelector("rect");
    if (!row || !rect) continue;
    const x = Number(rect.getAttribute("x")) + Number(rect.getAttribute("width")) - 20;
    const y = Number(rect.getAttribute("y")) + 5;
    const mark = document.createElementNS("http://www.w3.org/2000/svg", "g");
    mark.setAttribute("class", "node-kind");
    mark.setAttribute("transform", `translate(${x} ${y}) scale(0.8)`);
    mark.append(glyphShape(row.kind));
    g.append(mark);
  }
}

function enableHighlight(svg) {
  const edges = [...svg.querySelectorAll("path[data-rel]")].map((p) => {
    const t = p.querySelector("title");
    const [src = "", , dst = ""] = (t ? t.textContent : "").split(" ");
    return { p, src, dst };
  });
  const nodes = [...svg.querySelectorAll("g.rr-node")];
  const clear = () => {
    svg.classList.remove("focusing");
    for (const e of edges) e.p.classList.remove("hl");
    for (const n of nodes) n.classList.remove("hl");
  };
  const show = (id) => {
    clear();
    svg.classList.add("focusing");
    const near = new Set([id]);
    for (const e of edges) {
      if (e.src === id || e.dst === id) {
        e.p.classList.add("hl");
        near.add(e.src);
        near.add(e.dst);
      }
    }
    for (const n of nodes) if (near.has(n.dataset.id)) n.classList.add("hl");
  };
  for (const n of nodes) {
    n.addEventListener("pointerenter", () => show(n.dataset.id));
    n.addEventListener("pointerleave", clear);
    const a = n.closest("a");
    if (a) {
      a.addEventListener("focus", () => show(n.dataset.id));
      a.addEventListener("blur", clear);
    }
  }
}

export async function renderGraph(query) {
  const focus = query.get("focus") || "";
  const depth = Number(query.get("depth") || 2);
  const methods = query.get("methods") === "1";
  const kindsParam = query.get("kinds");
  const kinds = kindsParam ? kindsParam.split(",").filter(Boolean) : GRAPH_KINDS.slice();
  const params = new URLSearchParams();
  if (focus) {
    params.set("focus", focus);
    params.set("depth", String(depth));
  }
  if (kindsParam) params.set("kinds", kinds.join(","));
  if (methods) params.set("methods", "1");
  const g = await get(`/api/graph?${params}`);

  const page = h("div", { class: "page graph-page" });
  const state = { focus, depth, methods, kinds };
  const navigate = () =>
    go(
      hashOf("graph", {
        focus: state.focus,
        depth: state.focus ? String(state.depth) : "",
        kinds: state.kinds.length === GRAPH_KINDS.length ? "" : state.kinds.join(","),
        methods: state.methods ? "1" : "",
      }),
    );

  const picker = idPicker({
    kinds: ["user_need", "requirement", "mitigation", "risk", "test_method"],
    value: focus ? [focus] : [],
    single: true,
    placeholder: "Focus on an entity…",
    label: "Focus entity",
  });
  picker.addEventListener("change", () => {
    state.focus = picker.getValue()[0] || "";
    navigate();
  });
  const depthInput = h("input", {
    type: "range",
    min: 1,
    max: 6,
    value: depth,
    "aria-label": "Depth",
    disabled: !focus,
  });
  const depthOut = h("output", null, String(depth));
  depthInput.addEventListener("input", () => (depthOut.textContent = depthInput.value));
  depthInput.addEventListener("change", () => {
    state.depth = Number(depthInput.value);
    navigate();
  });
  const kindToggles = GRAPH_KINDS.map((k) =>
    h(
      "label",
      { class: "check" },
      h("input", {
        type: "checkbox",
        checked: kinds.includes(k),
        onChange: (ev) => {
          state.kinds = ev.target.checked ? [...new Set([...state.kinds, k])] : state.kinds.filter((x) => x !== k);
          if (!state.kinds.length) state.kinds = [k];
          navigate();
        },
      }),
      glyph(k),
      KIND[k].many,
    ),
  );
  const methodToggle = h(
    "label",
    { class: "check" },
    h("input", {
      type: "checkbox",
      checked: methods,
      onChange: (ev) => {
        state.methods = ev.target.checked;
        navigate();
      },
    }),
    "Test methods",
  );

  const svg = g.svg ? parseSvg(g.svg) : null;
  const viewport = h("div", {
    class: "graph-viewport",
    tabindex: "0",
    "aria-label": "Trace graph. Drag to pan, scroll or +/- to zoom, 0 to fit.",
  });
  let pz = null;
  if (svg && g.nodes.length) {
    add(viewport, svg);
    pz = enablePanZoom(viewport, svg);
    stampKinds(svg);
    enableHighlight(svg);
  }

  add(
    page,
    h(
      "header",
      { class: "page-head row" },
      h(
        "h1",
        null,
        "Trace graph",
        h("span", { class: "count" }, `${g.nodes.length} entities, ${g.edges.length} traces`),
      ),
      h(
        "div",
        { class: "actions" },
        focus ? button("Show everything", () => go("#/graph")) : null,
        focus ? button(`Open ${focus}`, () => go(`#/entity/${enc(focus)}`)) : null,
        button("Copy Mermaid", () => copyText(g.mermaid, "Mermaid diagram copied")),
      ),
    ),
    h(
      "div",
      { class: "toolbar graph-tools" },
      h("div", { class: "focus-pick" }, picker),
      h("label", { class: "range" }, "Depth ", depthInput, depthOut),
      h("div", { class: "checks", role: "group", "aria-label": "Kinds shown" }, kindToggles, methodToggle),
      h(
        "div",
        { class: "zoom" },
        button("−", () => pz && pz.zoom(1.2), { small: true, ariaLabel: "Zoom out" }),
        button("+", () => pz && pz.zoom(1 / 1.2), { small: true, ariaLabel: "Zoom in" }),
        button("Fit", () => pz && pz.fit(), { small: true }),
      ),
    ),
    g.nodes.length
      ? viewport
      : empty(
          focus ? `${focus} has no traces to show at this depth.` : "The model is empty. Create a user need to start.",
        ),
    h(
      "p",
      { class: "legend" },
      GRAPH_KINDS.map((k) => h("span", { class: "legend-item" }, glyph(k), KIND[k].one)),
      h("span", { class: "legend-item" }, h("span", { class: "swatch st-ok" }), "verified"),
      h("span", { class: "legend-item" }, h("span", { class: "swatch st-amber" }), "under-verified"),
      h("span", { class: "legend-item" }, h("span", { class: "swatch st-warn" }), "partial"),
      h("span", { class: "legend-item" }, h("span", { class: "swatch st-fail" }), "failed"),
      h("span", { class: "legend-item" }, h("span", { class: "swatch st-none" }), "no evidence"),
      h("span", { class: "muted" }, "Dashed lines are refinements."),
    ),
  );
  return page;
}
