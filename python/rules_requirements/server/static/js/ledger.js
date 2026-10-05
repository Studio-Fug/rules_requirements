// SPDX-License-Identifier: AGPL-3.0-or-later
// Verification sets and the case ledger. A test case verifies at most one
// requirement: every owner shown here comes from the server's attribution,
// and a case only changes owner through a model edit that passes its checks.

import { post } from "./api.js";
import { button, idTag, openDialog, sortableTable } from "./components.js";
import { h, natural, plural, swap } from "./dom.js";
import { KIND, store } from "./store.js";

export const VERIFIABLE = ["user_need", "requirement", "mitigation"];

const STATE_LABEL = {
  passed: "passed",
  failed: "failed",
  error: "error",
  skipped: "skipped",
  missing: "missing",
  "not-run": "not run",
  moved: "moved",
  quarantined: "quarantined",
};

const STATE_CLASS = {
  passed: "ok",
  failed: "fail",
  error: "fail",
  skipped: "warn",
  missing: "warn",
  "not-run": "warn",
  moved: "amber",
  quarantined: "fail",
};

/** The literal selector for exactly `path` (case_selectors.escape). */
export function escapeSelector(path) {
  return path.replace(/\\/g, "\\\\").replace(/\*/g, "\\*");
}

export function stateChip(state, { flaky = false, stale = false } = {}) {
  return h(
    "span",
    { class: "mstates" },
    h("span", { class: `mstate st-${STATE_CLASS[state] || "none"}` }, STATE_LABEL[state] || state),
    flaky ? h("span", { class: "mstate st-amber", title: "passed only on a retry" }, "flaky") : null,
    stale ? h("span", { class: "mstate st-stale", title: "from another build than the current one" }, "stale") : null,
  );
}

/** "set 17/23 passed · 6 not run" */
export function setLine(set) {
  if (!set || !set.members) return "empty set";
  const rest = [
    ["failed", "failed"],
    ["error", "error"],
    ["skipped", "skipped"],
    ["missing", "missing"],
    ["not_run", "not run"],
    ["moved", "moved"],
    ["quarantined", "quarantined"],
  ]
    .filter(([k]) => set[k])
    .map(([k, label]) => `${set[k]} ${label}`);
  return [`set ${set.passed}/${set.members} passed`, ...rest].join(" · ");
}

export function ownerTag(owner) {
  return owner ? idTag(owner) : h("span", { class: "muted" }, "no owner");
}

export function problemList(problems) {
  if (!problems || !problems.length) return null;
  return h(
    "ul",
    { class: "issues precheck-problems" },
    problems.map((p) =>
      h(
        "li",
        { class: "issue error" },
        h("span", { class: "sev error" }, "refused"),
        h("code", { class: "code-tag" }, p.code),
        h("span", { class: "issue-msg" }, p.message),
        h("span"),
      ),
    ),
  );
}

/** Where a case can go: every user need, requirement and mitigation, plus none. */
function ownerOptions(current, allowNone) {
  const opts = [h("option", { value: "" }, "Choose an owner…")];
  for (const kind of VERIFIABLE) {
    const ents = store.entities.filter((e) => e.kind === kind && e.id !== current);
    if (!ents.length) continue;
    opts.push(
      h(
        "optgroup",
        { label: KIND[kind].many },
        ents.map((e) => h("option", { value: e.id }, `${e.id}: ${e.title}`)),
      ),
    );
  }
  if (allowNone) opts.push(h("option", { value: "none" }, "none (it verifies nothing)"));
  return opts;
}

/**
 * Move one case to another owner (or none): a dry run first, then the model
 * edit. Resolves true when the case moved.
 */
export async function moveDialog(row) {
  const select = h("select", { "aria-label": "New owner", class: "move-to" }, ownerOptions(row.owner, Boolean(row.owner || row.quarantine)));
  const preview = h("div", { class: "move-preview", "aria-live": "polite" });
  const expand = h("input", { type: "checkbox", class: "move-expand" });
  const expandRow = h(
    "label",
    { class: "check-row", hidden: true },
    expand,
    " Rewrite the glob or whole-target claim into literal selectors of the cases it selects now",
  );
  let last = null;

  async function dryRun() {
    last = null;
    if (!select.value) {
      swap(preview);
      return;
    }
    swap(preview, h("p", { class: "muted" }, "Checking…"));
    try {
      last = await post("/api/cases/move", { case: row.case, to: select.value, dry_run: true });
    } catch (err) {
      swap(preview, h("p", { class: "form-error" }, err.message));
      return;
    }
    const blocking = last.problems.filter((p) => p.code !== "needs-expand");
    expandRow.hidden = !last.problems.some((p) => p.code === "needs-expand");
    swap(
      preview,
      h("h3", null, "The model edit"),
      h(
        "ul",
        { class: "plain move-plan" },
        last.plan.map((step) => h("li", null, step)),
      ),
      problemList(blocking),
      blocking.length ? null : h("p", { class: "muted" }, "It passes the checks: each case keeps at most one owner."),
    );
  }
  select.addEventListener("change", dryRun);

  const result = await openDialog({
    title: row.owner ? `Move ${row.path || row.case}` : `Assign ${row.path || row.case}`,
    wide: true,
    body: h(
      "div",
      { class: "move-dialog" },
      h("p", null, h("code", { class: "case-key" }, row.case)),
      h(
        "p",
        null,
        row.owner ? ["Owned by ", ownerTag(row.owner), " today. "] : row.quarantine ? "Quarantined today. " : "No entity owns it today. ",
        "A test case verifies at most one requirement.",
      ),
      h("label", { class: "field" }, h("span", null, "Give it to"), select),
      expandRow,
      preview,
    ),
    actions: [
      {
        label: "Move",
        primary: true,
        run: async () => {
          if (!select.value) throw new Error("Choose the new owner.");
          if (!last) await dryRun();
          if (!last) return false;
          const blocking = last.problems.filter((p) => p.code !== "needs-expand");
          if (blocking.length) throw new Error("This move would break the one-owner rule; see above.");
          if (!expandRow.hidden && !expand.checked) throw new Error("Confirm the rewrite of the claim first.");
          await post("/api/cases/move", { case: row.case, to: select.value, expand: expand.checked });
          return undefined;
        },
      },
    ],
  });
  return result === "Move";
}

/** The members of a verification set, one row each. */
export function memberTable(members, { onMove = null } = {}) {
  return sortableTable({
    columns: [
      {
        key: "name",
        label: "Case",
        render: (m) =>
          h(
            "div",
            null,
            h("span", { class: "test-name" }, m.case ? m.case.slice(m.target.length + 1) : m.selector),
            h("div", { class: "muted small" }, h("code", null, m.target)),
          ),
      },
      {
        key: "state",
        label: "State",
        sort: (m) => Object.keys(STATE_LABEL).indexOf(m.state),
        render: (m) => stateChip(m.state, m),
      },
      {
        key: "selector",
        label: "Selector",
        render: (m) =>
          h(
            "span",
            { title: m.origin || "" },
            h("code", null, m.selector),
            m.via !== "model" ? h("span", { class: "muted" }, ` via ${m.via}`) : null,
          ),
      },
      { key: "level", label: "Level", render: (m) => (m.level ? h("span", { class: "level" }, m.level) : "") },
      {
        key: "reason",
        label: "Why",
        sort: false,
        render: (m) => (m.reason || m.message ? h("span", { class: "msg muted" }, m.reason || m.message) : ""),
      },
      {
        key: "act",
        label: "",
        sort: false,
        render: (m) =>
          onMove && m.case && (m.owned || m.state === "quarantined")
            ? button("Move…", () => onMove(m), { small: true, quiet: true })
            : "",
      },
    ],
    rows: members.slice().sort((a, b) => natural(a.name, b.name)),
    emptyText: "No case belongs to this set yet.",
  });
}

/** "N test cases · 0 quarantined · each case → ≤1 requirement" */
export function invariantLine(summary, { link = false } = {}) {
  const body = [
    h("strong", null, plural(summary.cases, "test case")),
    ` · ${summary.owned} owned · ${summary.unowned} unowned · `,
    h("strong", null, `${summary.quarantined} quarantined`),
    " · each case → ≤1 requirement",
  ];
  const cls = ["invariant", summary.quarantined ? "bad" : "good"];
  if (!link) return h("p", { class: cls }, body);
  const href = summary.quarantined ? "#/cases?state=quarantined" : "#/cases";
  return h("a", { class: cls, href }, body);
}
