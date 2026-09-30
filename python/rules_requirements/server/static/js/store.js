// SPDX-License-Identifier: AGPL-3.0-or-later
// The client-side cache of the model: /api/state + /api/entities.

import { get } from "./api.js";
import { natural } from "./dom.js";

// Lane order: every view lays the kinds out in this order, left to right.
export const KINDS = ["user_need", "requirement", "mitigation", "risk", "test_method"];

export const KIND = {
  user_need: { route: "needs", one: "User need", many: "User needs", short: "Need" },
  requirement: { route: "requirements", one: "Requirement", many: "Requirements", short: "Requirement" },
  mitigation: { route: "mitigations", one: "Mitigation", many: "Mitigations", short: "Mitigation" },
  risk: { route: "risks", one: "Risk", many: "Risks", short: "Risk" },
  test_method: { route: "methods", one: "Test method", many: "Test methods", short: "Method" },
};

export const KIND_BY_ROUTE = Object.fromEntries(Object.entries(KIND).map(([k, v]) => [v.route, k]));

// Status -> visual class. Same meaning as the HTML report and graph colors.
const STATUS_CLASS = {
  VERIFIED: "ok",
  VALIDATED: "ok",
  MITIGATED: "ok",
  "UNDER-VERIFIED": "amber",
  PARTIAL: "warn",
  FAILED: "fail",
  STALE: "stale",
  UNVERIFIED: "none",
  UNVALIDATED: "none",
  OPEN: "none",
};

export function statusClass(status) {
  return STATUS_CLASS[status] || "none";
}

// The statuses each kind can take, best first (for distribution bars).
export const STATUS_ORDER = {
  user_need: ["VALIDATED", "PARTIAL", "FAILED", "UNVALIDATED"],
  requirement: ["VERIFIED", "UNDER-VERIFIED", "PARTIAL", "FAILED", "UNVERIFIED"],
  mitigation: ["VERIFIED", "PARTIAL", "FAILED", "UNVERIFIED"],
  risk: ["MITIGATED", "PARTIAL", "FAILED", "OPEN"],
  test_method: ["VERIFIED", "PARTIAL", "FAILED", "UNVERIFIED"],
};

export const store = {
  state: null,
  entities: [],
  byId: new Map(),
  draft: null, // a prefilled entity handed from the agents view to the editor
  listeners: new Set(),
};

export async function refresh() {
  const [state, list] = await Promise.all([get("/api/state"), get("/api/entities")]);
  store.state = state;
  store.entities = list.entities
    .slice()
    .sort((a, b) => KINDS.indexOf(a.kind) - KINDS.indexOf(b.kind) || natural(a.id, b.id));
  store.byId = new Map(store.entities.map((e) => [e.id, e]));
  for (const fn of store.listeners) fn();
  return store;
}

export function entitiesOf(kind) {
  return store.entities.filter((e) => e.kind === kind);
}

export function config() {
  return (store.state && store.state.config) || {};
}

export function levelNames() {
  return (config().levels || []).map((l) => l.name);
}

/** Every entity id referenced by `row` in a given relation (from its data). */
export function refsOf(row, key) {
  const v = row && row.data ? row.data[key] : undefined;
  if (!v) return [];
  return Array.isArray(v) ? v : [v];
}

/** Entities whose `key` field references `id`. */
export function referrers(id, key) {
  return store.entities.filter((e) => refsOf(e, key).includes(id));
}
