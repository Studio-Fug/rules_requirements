// SPDX-License-Identifier: AGPL-3.0-or-later
// Tiny DOM helpers. Everything user- or model-provided goes in as text nodes,
// never as HTML.

const PROPS = new Set(["value", "checked", "selected", "disabled", "indeterminate"]);

function appendAll(el, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false || child === true) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

function applyAttrs(el, attrs) {
  const later = [];
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") el.setAttribute("class", Array.isArray(value) ? value.filter(Boolean).join(" ") : value);
    else if (key === "style" && typeof value === "object") Object.assign(el.style, value);
    else if (key === "dataset") Object.assign(el.dataset, value);
    else if (key.startsWith("on") && typeof value === "function")
      el.addEventListener(key.slice(2).toLowerCase(), value);
    else if (PROPS.has(key)) later.push([key, value]);
    else if (value === true) el.setAttribute(key, "");
    else el.setAttribute(key, String(value));
  }
  return later;
}

/** h("a", {href: "#/x", class: "c"}, "text", child) */
export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  const later = applyAttrs(el, attrs);
  appendAll(el, children);
  for (const [k, v] of later) el[k] = v;
  return el;
}

const SVGNS = "http://www.w3.org/2000/svg";

export function s(tag, attrs, ...children) {
  const el = document.createElementNS(SVGNS, tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") el.setAttribute("class", value);
    else el.setAttribute(key, String(value));
  }
  appendAll(el, children);
  return el;
}

export function frag(...children) {
  const f = document.createDocumentFragment();
  appendAll(f, children);
  return f;
}

/** Null-safe append (native append() would insert the text "null"). */
export function add(el, ...children) {
  appendAll(el, children);
  return el;
}

/** Null-safe replaceChildren. */
export function swap(el, ...children) {
  el.replaceChildren();
  appendAll(el, children);
  return el;
}

export function mount(el, ...children) {
  el.replaceChildren();
  appendAll(el, children);
  return el;
}

/** Natural sort key: REQ-2 before REQ-10. */
export function natural(a, b) {
  return String(a).localeCompare(String(b), undefined, { numeric: true, sensitivity: "base" });
}

export function debounce(fn, ms = 150) {
  let t = 0;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}

export function plural(n, one, many = `${one}s`) {
  return `${n} ${n === 1 ? one : many}`;
}

export function timeAgo(epochSeconds) {
  if (!epochSeconds) return "";
  const d = Math.max(0, Date.now() / 1000 - epochSeconds);
  if (d < 60) return "just now";
  if (d < 3600) return `${Math.round(d / 60)} min ago`;
  if (d < 86400) return `${Math.round(d / 3600)} h ago`;
  return new Date(epochSeconds * 1000).toLocaleDateString();
}
