// SPDX-License-Identifier: AGPL-3.0-or-later
// The case ledger: every test case of the loaded evidence and its one owner
// (or none), as attribution decided it. Moving a case edits the model.

import { get, post } from "../api.js";
import { button, empty, openDialog, reportError, sortableTable, toast } from "../components.js";
import { add, h, plural, swap } from "../dom.js";
import { invariantLine, moveDialog, ownerTag } from "../ledger.js";
import { hashOf, reloadModel, rerender } from "../nav.js";

const STATES = [
  ["", "all"],
  ["owned", "owned"],
  ["unowned", "unowned"],
  ["quarantined", "quarantined"],
  ["unlocked", "not locked"],
  ["coarse", "coarse"],
];

function matches(row, state, q, target, lane) {
  if (target && row.target !== target) return false;
  if (lane && !(row.lanes || []).includes(lane)) return false;
  if (q && !row.case.toLowerCase().includes(q) && !String(row.owner || "").toLowerCase().includes(q)) return false;
  if (state === "owned") return Boolean(row.owner);
  if (state === "unowned") return !row.owner && !row.quarantine;
  if (state === "quarantined") return Boolean(row.quarantine);
  if (state === "unlocked") return Boolean(row.owner) && row.locked_to === "";
  if (state === "coarse") return Boolean(row.coarse);
  return true;
}

/** The owner cell: the one owner, or why there is none (never two ids that read as two owners). */
function ownerCell(r) {
  if (r.quarantine) {
    const multiTag = r.quarantine.code === "multi-tag";
    return h(
      "span",
      { class: "quarantine-tag", title: r.quarantine.detail },
      h("span", { class: "mstate st-fail" }, multiTag ? "quarantined (multi-tag)" : `quarantined (${r.quarantine.code})`),
      h(
        "div",
        { class: "muted small" },
        multiTag ? "its tags declare " : "named by ",
        r.quarantine.entities.join(", "),
        " · owner: none",
      ),
    );
  }
  return h("span", null, ownerTag(r.owner), r.owner && r.via !== "model" ? h("span", { class: "muted" }, ` via ${r.via}`) : null);
}

function entryList(title, entries) {
  if (!entries || !entries.length) return null;
  return [
    h("h3", null, `${title} (${entries.length})`),
    h(
      "ul",
      { class: "plain lock-plan" },
      entries.map((e) => h("li", null, h("code", { class: "case-key" }, e.case), e.from ? ` ${e.from} → ${e.owner}` : ` ${e.owner}`)),
    ),
  ];
}

/**
 * Update the verification-set lock over the loaded evidence (rr sets lock):
 * a dry run first; removals only when confirmed. Resolves true when written.
 */
async function lockDialog() {
  const plan = await post("/api/lock/update", { dry_run: true });
  const allow = h("input", { type: "checkbox", class: "lock-allow-removals" });
  const body = h(
    "div",
    { class: "lock-dialog" },
    h("p", null, "The lock only records the owners attribution decided; it never decides one."),
    plan.refused.length
      ? h("div", { class: "callout fail" }, h("h3", null, "Quarantined cases have no owner to lock"), h("ul", null, plan.refused.map((r) => h("li", null, r))))
      : null,
    plan.up_to_date ? h("p", { class: "muted" }, `${plan.path} is up to date.`) : null,
    entryList("Added", plan.added),
    entryList("Owner changes", plan.changed),
    entryList("Removed", plan.removed),
    plan.removed.length
      ? h(
          "label",
          { class: "check-row" },
          allow,
          ` Remove ${plural(plan.removed.length, "entry", "entries")} (a case missing from a target that ran, or no claim selects it); otherwise they are kept`,
        )
      : null,
  );
  const result = await openDialog({
    title: "Update the verification-set lock",
    wide: true,
    body,
    actions:
      plan.refused.length || (plan.up_to_date && !plan.removed.length)
        ? []
        : [
            {
              label: "Write the lock",
              primary: true,
              run: async () => {
                await post("/api/lock/update", { allow_removals: allow.checked });
                return undefined;
              },
            },
          ],
  });
  return result === "Write the lock";
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
        data.lock_status && data.lock_status.out_of_date ? h("span", { class: "mstate st-amber" }, " lock out of date") : null,
        data.lock_status && data.lock_status.path
          ? button(
              "Update lock…",
              async () => {
                try {
                  if (await lockDialog()) {
                    toast("Wrote the verification-set lock");
                    await reloadModel({ page: false });
                    await rerender({ keepScroll: true });
                  }
                } catch (err) {
                  reportError(err);
                }
              },
              { small: true, quiet: true },
            )
          : null,
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
        render: ownerCell,
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
        render: (r) =>
          h(
            "span",
            null,
            r.claimed_by && r.claimed_by.length ? r.claimed_by.join(", ") : h("span", { class: "muted" }, "—"),
            r.coarse ? h("span", { class: "mstate st-amber", title: "a whole-target claim selects this per-case result" }, "coarse") : null,
            r.declared && r.declared.length ? h("div", { class: "muted small" }, `declared: ${r.declared.join(", ")}`) : null,
          ),
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
  const laneSel = (data.lanes || []).length
    ? h(
        "select",
        { "aria-label": "Lane" },
        h("option", { value: "" }, "every lane"),
        data.lanes.map((l) => h("option", { value: l, selected: l === query.get("lane") }, `lane ${l}`)),
      )
    : null;

  function update() {
    const q = input.value.trim().toLowerCase();
    const lane = laneSel ? laneSel.value : "";
    const rows = data.cases.filter((r) => matches(r, state, q, targetSel.value, lane));
    table.update(rows);
    swap(count, `${rows.length} shown`);
    history.replaceState(null, "", hashOf("cases", { state, q: input.value.trim(), target: targetSel.value, lane }));
  }

  function drawChips() {
    swap(
      chips,
      ...STATES.filter(([s]) => (s !== "unlocked" || data.lock) && (s !== "coarse" || data.cases.some((r) => r.coarse))).map(([s, label]) =>
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
  if (laneSel) laneSel.addEventListener("change", update);
  drawChips();
  add(page, h("div", { class: "toolbar" }, input, targetSel, laneSel, chips, count), table);
  update();
  return page;
}
