// SPDX-License-Identifier: AGPL-3.0-or-later
// Overview: where the model stands, lane by lane, and what needs attention.

import { get } from "../api.js";
import { badge, empty, glyph, idTag, section, sourceRef } from "../components.js";
import { add, h, plural } from "../dom.js";
import { KIND, STATUS_ORDER, entitiesOf, statusClass, store } from "../store.js";

const GOOD = { user_need: "validated", requirement: "verified", mitigation: "verified", risk: "mitigated" };

function ledgerRow(kind) {
  const rows = entitiesOf(kind);
  const order = STATUS_ORDER[kind];
  const counts = new Map(order.map((s) => [s, 0]));
  for (const r of rows) counts.set(r.status, (counts.get(r.status) || 0) + 1);
  const total = rows.length;
  const good = counts.get(order[0]) || 0;
  const bar = h(
    "div",
    { class: "dist", role: "img", "aria-label": order.map((s) => `${counts.get(s) || 0} ${s}`).join(", ") },
    total
      ? order
          .filter((s) => counts.get(s))
          .map((s) =>
            h("span", {
              class: `dist-seg st-${statusClass(s)}`,
              style: { flexGrow: String(counts.get(s)) },
              title: `${counts.get(s)} ${s}`,
            }),
          )
      : h("span", { class: "dist-seg st-none", style: { flexGrow: "1" } }),
  );
  const legend = h(
    "div",
    { class: "dist-legend" },
    order
      .filter((s) => counts.get(s))
      .map((s) =>
        h(
          "span",
          { class: "legend-item" },
          h("span", { class: `swatch st-${statusClass(s)}` }),
          `${counts.get(s)} ${s.toLowerCase()}`,
        ),
      ),
  );
  return h(
    "a",
    { class: "ledger-row", href: `#/${KIND[kind].route}` },
    h("span", { class: "ledger-kind" }, glyph(kind), KIND[kind].many),
    h(
      "span",
      { class: "ledger-figure" },
      total ? h("strong", null, String(good)) : "—",
      total ? ` of ${total} ${GOOD[kind]}` : " none yet",
    ),
    h("span", { class: "ledger-bar" }, bar, legend),
  );
}

export async function renderOverview() {
  const report = await get("/api/report");
  const st = store.state;
  const project = st.project || {};
  const counts = st.counts || {};
  const page = h("div", { class: "page overview" });

  add(
    page,
    h(
      "header",
      { class: "page-head" },
      h("h1", null, project.name || "Requirements"),
      project.description || project.source ? h("p", { class: "lede" }, project.description || project.source) : null,
    ),
  );

  add(
    page,
    h(
      "section",
      { class: "ledger", "aria-label": "Verification and validation status" },
      ["user_need", "requirement", "mitigation", "risk"].map(ledgerRow),
      h(
        "p",
        { class: "ledger-foot" },
        `${plural(counts.test_cases || 0, "test case")} traced`,
        st.evidence_paths && st.evidence_paths.length
          ? ""
          : ". No test evidence is loaded; start rr serve with --evidence (for example bazel-testlogs) to trace results",
        st.annotations_scanned ? "" : ". Source annotations were not scanned",
        ".",
      ),
    ),
  );

  const alerts = [];
  if (report.high_open_risks && report.high_open_risks.length) {
    alerts.push(
      h(
        "div",
        { class: "callout fail" },
        h("h2", null, "High-severity risks without verified control"),
        h(
          "ul",
          { class: "plain" },
          report.high_open_risks.map((id) => {
            const r = (report.risks || []).find((x) => x.id === id) || {};
            return h(
              "li",
              null,
              idTag(id),
              " ",
              h("span", null, r.title || ""),
              " ",
              badge(r.status),
              r.severity ? h("span", { class: "muted" }, ` ${r.severity}`) : null,
            );
          }),
        ),
      ),
    );
  }
  if (report.pyramid_violations && report.pyramid_violations.length) {
    alerts.push(
      h(
        "div",
        { class: "callout amber" },
        h("h2", null, "Hardware evidence without a cheaper test behind it"),
        h(
          "p",
          null,
          "These requirements rest only on expensive physical evidence. Add an analysis or simulation test that backs the result.",
        ),
        h(
          "p",
          null,
          report.pyramid_violations.map((id) => [idTag(id), " "]),
        ),
      ),
    );
  }
  if (report.unknown_evidence && Object.keys(report.unknown_evidence).length) {
    alerts.push(
      h(
        "div",
        { class: "callout fail" },
        h("h2", null, "Tests reference ids that do not exist"),
        h(
          "ul",
          { class: "plain" },
          Object.entries(report.unknown_evidence).map(([id, cases]) =>
            h("li", null, h("code", null, id), " ", h("span", { class: "muted" }, cases.join(", "))),
          ),
        ),
      ),
    );
  }
  if (alerts.length) add(page, h("div", { class: "alerts" }, alerts));

  const cols = h("div", { class: "cols" });
  const issues = st.issues || [];
  add(
    cols,
    section(
      `Model issues (${issues.length})`,
      issues.length
        ? h(
            "ul",
            { class: "issues" },
            issues
              .slice(0, 40)
              .map((i) =>
                h(
                  "li",
                  { class: `issue ${i.severity}` },
                  h("span", { class: `sev ${i.severity}` }, i.severity),
                  i.entity && store.byId.get(i.entity) ? idTag(i.entity) : h("span"),
                  h("span", { class: "issue-msg" }, i.message),
                  i.path ? sourceRef(i.path, i.line) : h("span"),
                ),
              ),
          )
        : empty("The model validates cleanly."),
    ),
  );

  const gaps = report.gaps || [];
  const byKind = new Map();
  for (const g of gaps) byKind.set(g.kind, (byKind.get(g.kind) || 0) + 1);
  add(
    cols,
    section(
      `Gaps (${gaps.length})`,
      gaps.length
        ? h(
            "div",
            null,
            h(
              "p",
              { class: "gap-kinds" },
              [...byKind.entries()]
                .sort((a, b) => b[1] - a[1])
                .map(([k, n]) => h("a", { class: "chip", href: `#/queue?kind=${encodeURIComponent(k)}` }, `${k} ${n}`)),
            ),
            h(
              "ul",
              { class: "issues" },
              gaps
                .slice(0, 12)
                .map((g) =>
                  h(
                    "li",
                    { class: "issue" },
                    h("span", { class: ["route", g.route] }, g.route === "human-gate" ? "human" : "agent"),
                    store.byId.get(g.entity) ? idTag(g.entity) : h("code", null, g.entity),
                    h("span", { class: "issue-msg" }, g.message),
                    h("span"),
                  ),
                ),
            ),
            h("p", null, h("a", { href: "#/queue" }, `Open the work queue (${gaps.length})`)),
          )
        : empty("No gaps: every requirement is verified at its demanded level."),
    ),
  );
  add(page, cols);
  return page;
}
