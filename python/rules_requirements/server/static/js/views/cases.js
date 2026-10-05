// SPDX-License-Identifier: AGPL-3.0-or-later
// The case ledger: every test case of the loaded evidence and its one owner
// (or none), as attribution decided it. Moving a case edits the model.

import { get } from "../api.js";
import { button, empty, sortableTable } from "../components.js";
import { add, h, swap } from "../dom.js";
import { invariantLine, moveDialog, ownerTag } from "../ledger.js";
import { hashOf, reloadModel, rerender } from "../nav.js";

const STATES = [
  ["", "all"],
  ["owned", "owned"],
  ["unowned", "unowned"],
  ["quarantined", "quarantined"],
  ["unlocked", "not locked"],
];

function matches(row, state, q, target) {
  if (target && row.target !== target) return false;
  if (q && !row.case.toLowerCase().includes(q) && !String(row.owner || "").toLowerCase().includes(q)) return false;
  if (state === "owned") return Boolean(row.owner);
  if (state === "unowned") return !row.owner && !row.quarantine;
  if (state === "quarantined") return Boolean(row.quarantine);
  if (state === "unlocked") return Boolean(row.owner) && row.locked_to === "";
  return true;
}

export async function renderCases(query) {
  const data = await get("/api/cases");
  const page = h("div", { class: "page cases" });
  let state = query.get("state") || "";
  const input = h("input", {
    type: "search",
    value: query.get("q") || "",
    placeholder: "Filter by case, target or owner",
    "aria-label": "Filter cases",
  });
  const targetSel = h(
    "select",
    { "aria-label": "Target" },
    h("option", { value: "" }, "every target"),
    data.targets.map((t) => h("option", { value: t, selected: t === query.get("target") }, t)),
  );

  add(
    page,
    h(
      "header",
      { class: "page-head" },
      h("h1", null, "Case ledger"),
      h(
        "p",
        { class: "lede" },
        "A test case verifies at most one requirement; a set of cases may together verify one. Owners come from ",
        h("code", null, "verified_by"),
        " / ",
        h("code", null, "validated_by"),
        " claims (and, in hybrid mode, a single tag). Ambiguous cases are quarantined and count for nobody.",
      ),
      invariantLine(data.summary),
      h(
        "p",
        { class: "muted" },
        `Attribution: ${data.mode}`,
        data.lock ? ` · lock ${data.lock}` : " · no verification-set lock",
      ),
    ),
  );

  if (!data.summary.cases) {
    add(page, empty("No test evidence is loaded. Start rr serve with --evidence (for example bazel-testlogs)."));
    return page;
  }

  async function move(row) {
    if (await moveDialog(row)) {
      await reloadModel({ page: false });
      await rerender({ keepScroll: true });
    }
  }

  const table = sortableTable({
    columns: [
      {
        key: "case",
        label: "Case",
        sort: (r) => `${r.target}#${r.path}`,
        render: (r) =>
          h(
            "div",
            null,
            h("span", { class: "test-name" }, r.path),
            h("div", { class: "muted small" }, h("code", null, r.target)),
          ),
      },
      {
        key: "owner",
        label: "Owner",
        sort: (r) => r.owner || (r.quarantine ? "~quarantined" : "~~"),
        render: (r) =>
          r.quarantine
            ? h(
                "span",
                { class: "quarantine-tag", title: r.quarantine.detail },
                h("span", { class: "mstate st-fail" }, r.quarantine.code),
                " ",
                r.quarantine.entities.join(", "),
              )
            : h("span", null, ownerTag(r.owner), r.owner && r.via !== "model" ? h("span", { class: "muted" }, ` via ${r.via}`) : null),
      },
      {
        key: "status",
        label: "Result",
        render: (r) => h("span", { class: `result ${r.status}` }, r.status, r.flaky ? " (flaky)" : ""),
      },
      {
        key: "claimed_by",
        label: "Claimed by",
        sort: (r) => (r.claimed_by || []).join(","),
        render: (r) => (r.claimed_by && r.claimed_by.length ? r.claimed_by.join(", ") : h("span", { class: "muted" }, "—")),
      },
      {
        key: "locked_to",
        label: "Locked to",
        render: (r) => (r.locked_to === undefined ? "" : r.locked_to || h("span", { class: "muted" }, "—")),
      },
      {
        key: "act",
        label: "",
        sort: false,
        render: (r) =>
          r.quarantine && r.quarantine.code === "multi-tag"
            ? h("span", { class: "muted small", title: "tag the test with one id" }, "fix the test's tags")
            : button(r.owner ? "Move…" : "Assign…", () => move(r), { small: true, quiet: true }),
      },
    ],
    rows: [],
    emptyText: "No case matches.",
    rowAttrs: (r) => ({ class: r.quarantine ? "row-quarantined" : "", "data-case": r.case }),
  });

  const chips = h("div", { class: "chips", role: "group", "aria-label": "Filter by owner state" });
  const count = h("span", { class: "muted" });

  function update() {
    const q = input.value.trim().toLowerCase();
    const rows = data.cases.filter((r) => matches(r, state, q, targetSel.value));
    table.update(rows);
    swap(count, `${rows.length} shown`);
    history.replaceState(null, "", hashOf("cases", { state, q: input.value.trim(), target: targetSel.value }));
  }

  function drawChips() {
    swap(
      chips,
      ...STATES.filter(([s]) => s !== "unlocked" || data.lock).map(([s, label]) =>
        h(
          "button",
          {
            type: "button",
            class: ["chip", "toggle", state === s ? "on" : ""],
            "aria-pressed": state === s ? "true" : "false",
            onClick: () => {
              state = s;
              drawChips();
              update();
            },
          },
          label,
        ),
      ),
    );
  }

  input.addEventListener("input", update);
  targetSel.addEventListener("change", update);
  drawChips();
  add(page, h("div", { class: "toolbar" }, input, targetSel, chips, count), table);
  update();
  return page;
}
