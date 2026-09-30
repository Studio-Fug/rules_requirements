// SPDX-License-Identifier: AGPL-3.0-or-later
// Navigation helpers shared by the shell and the views (no view imports here,
// so there are no module cycles).

import { refresh } from "./store.js";

let hooks = { render: async () => {}, chrome: () => {} };

export function registerHooks(next) {
  hooks = next;
}

export function parseRoute() {
  const raw = location.hash.replace(/^#\/?/, "");
  const [pathPart, queryPart = ""] = raw.split("?");
  const parts = pathPart.split("/").filter(Boolean).map(decodeURIComponent);
  return { parts, query: new URLSearchParams(queryPart) };
}

/** Navigate to `hash` (re-rendering even when it is the current one). */
export function go(hash) {
  if (location.hash === hash) return hooks.render();
  location.hash = hash;
  return undefined;
}

/** Redraw the current page; `{keepScroll: true}` keeps the scroll position. */
export function rerender(opts = {}) {
  return hooks.render(opts);
}

/** Re-fetch the model, redraw the chrome and (by default) the current page. */
export async function reloadModel({ page = true } = {}) {
  await refresh();
  hooks.chrome();
  if (page) await hooks.render();
}

/** Build a hash with query parameters. */
export function hashOf(path, params = {}) {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== "") q.set(k, v);
  const qs = q.toString();
  return `#/${path}${qs ? `?${qs}` : ""}`;
}
