// SPDX-License-Identifier: AGPL-3.0-or-later
// One entity: where it sits in the trace, its evidence, its code, its notes.

import { del, enc, get, patch, post, settings } from "../api.js";
import {
  badge,
  button,
  codeViewer,
  confirmDialog,
  empty,
  glyph,
  idTag,
  kindLabel,
  promptDialog,
  reportError,
  sortableTable,
  sourceRef,
  toast,
} from "../components.js";
import { add, h, plural, swap } from "../dom.js";
import { memberTable, moveDialog, setLine } from "../ledger.js";
import { go, reloadModel, rerender } from "../nav.js";
import { KIND, config, referrers, refsOf, store } from "../store.js";

const LANES = ["user_need", "requirement", "mitigation", "risk"];
const LANE_JOIN = { user_need: "satisfied by", requirement: "implement", mitigation: "control" };

const MITIGATION_TYPES = {
  inherent: "Inherent safety by design",
  protective: "Protective measure",
  information: "Information for safety",
};

// --------------------------------------------------------------------------
// The trace strip: the entity's place in the UN -> REQ <- MIT -> RISK chain.
// --------------------------------------------------------------------------

function uniq(ids) {
  return [...new Set(ids)].filter((id) => store.byId.has(id));
}

function chainFor(row) {
  const lanes = { user_need: [], requirement: [], mitigation: [], risk: [] };
  const extra = { parents: [], children: [] };
  const mitsOfReqs = (reqs) => uniq(reqs.flatMap((r) => referrers(r, "implemented_by").map((m) => m.id)));
  const risksOfMits = (mits) => uniq(mits.flatMap((m) => refsOf(store.byId.get(m), "mitigates")));
  const needsOfReqs = (reqs) => uniq(reqs.flatMap((r) => refsOf(store.byId.get(r), "satisfies")));
  const reqsOfMits = (mits) => uniq(mits.flatMap((m) => refsOf(store.byId.get(m), "implemented_by")));
  switch (row.kind) {
    case "requirement": {
      lanes.user_need = uniq(refsOf(row, "satisfies"));
      extra.parents = uniq(refsOf(row, "refines"));
      extra.children = uniq(referrers(row.id, "refines").map((e) => e.id));
      lanes.requirement = [row.id];
      lanes.mitigation = mitsOfReqs([row.id]);
      lanes.risk = risksOfMits(lanes.mitigation);
      break;
    }
    case "user_need": {
      lanes.user_need = [row.id];
      lanes.requirement = uniq(referrers(row.id, "satisfies").map((e) => e.id));
      lanes.mitigation = mitsOfReqs(lanes.requirement);
      lanes.risk = risksOfMits(lanes.mitigation);
      break;
    }
    case "mitigation": {
      lanes.mitigation = [row.id];
      lanes.requirement = uniq(refsOf(row, "implemented_by"));
      lanes.user_need = needsOfReqs(lanes.requirement);
      lanes.risk = uniq(refsOf(row, "mitigates"));
      break;
    }
    case "risk": {
      lanes.risk = [row.id];
      lanes.mitigation = uniq(referrers(row.id, "mitigates").map((e) => e.id));
      lanes.requirement = reqsOfMits(lanes.mitigation);
      lanes.user_need = needsOfReqs(lanes.requirement);
      break;
    }
    case "test_method": {
      lanes.requirement = uniq(referrers(row.id, "method").map((e) => e.id));
      lanes.user_need = needsOfReqs(lanes.requirement);
      lanes.mitigation = mitsOfReqs(lanes.requirement);
      lanes.risk = risksOfMits(lanes.mitigation);
      break;
    }
    default:
      break;
  }
  return { lanes, extra };
}

function traceStrip(row) {
  const { lanes, extra } = chainFor(row);
  const strip = h("section", { class: "strip", "aria-label": "Position in the trace" });
  LANES.forEach((kind, i) => {
    const ids = lanes[kind];
    const lane = h(
      "div",
      { class: ["lane", row.kind === kind ? "self" : ""] },
      h("h3", { class: "lane-head" }, glyph(kind), KIND[kind].many),
      ids.length
        ? h(
            "ul",
            { class: "lane-items" },
            ids.map((id) =>
              h(
                "li",
                { class: id === row.id ? "this" : "" },
                id === row.id
                  ? h("span", { class: "this-mark" }, idTag(id, { link: false, showTitle: true }))
                  : idTag(id, { showTitle: true }),
              ),
            ),
          )
        : h("p", { class: "lane-empty" }, "none"),
    );
    if (kind === "requirement" && (extra.parents.length || extra.children.length)) {
      add(
        lane,
        h(
          "div",
          { class: "lane-sub" },
          extra.parents.length
            ? h(
                "p",
                null,
                "refines ",
                extra.parents.map((id) => [idTag(id, { compact: true }), " "]),
              )
            : null,
          extra.children.length
            ? h(
                "p",
                null,
                "refined by ",
                extra.children.map((id) => [idTag(id, { compact: true }), " "]),
              )
            : null,
        ),
      );
    }
    add(strip, lane);
    if (i < LANES.length - 1)
      add(strip, h("div", { class: "lane-join", "aria-hidden": "true" }, h("span", null, LANE_JOIN[kind])));
  });
  if (row.kind === "test_method") strip.classList.add("from-method");
  return strip;
}

// --------------------------------------------------------------------------
// Details per kind
// --------------------------------------------------------------------------

function dl(pairs) {
  const items = pairs.filter(([, v]) => v !== null && v !== undefined && v !== "" && !(Array.isArray(v) && !v.length));
  if (!items.length) return null;
  return h(
    "dl",
    { class: "facts" },
    items.map(([k, v]) => [h("dt", null, k), h("dd", null, v)]),
  );
}

function scoreOf(sev, likelihood) {
  const c = config();
  const si = (c.severities || []).indexOf(sev);
  const li = (c.likelihoods || []).indexOf(likelihood);
  return si < 0 || li < 0 ? null : (si + 1) * (li + 1);
}

function estimate(sev, likelihood) {
  if (!sev && !likelihood) return null;
  const score = scoreOf(sev, likelihood);
  return h(
    "span",
    { class: "estimate" },
    h("strong", null, sev || "?"),
    " × ",
    h("strong", null, likelihood || "?"),
    score ? h("span", { class: "muted" }, ` score ${score}`) : null,
  );
}

function methodView(ent) {
  const m = ent.data.method;
  const level = ent.demanded;
  if (!m)
    return h(
      "span",
      null,
      h("span", { class: "level" }, level || "default"),
      h("span", { class: "muted" }, " (project default)"),
    );
  if (store.byId.has(m))
    return h("span", null, idTag(m, { showTitle: true }), " at ", h("span", { class: "level" }, level));
  return h("span", { class: "level" }, m);
}

/** verified_by / validated_by items: the target, and its selectors or "whole target". */
function claimsView(items) {
  if (!items || !items.length) return null;
  return h(
    "ul",
    { class: "plain claims" },
    items.map((v) => {
      const item = typeof v === "string" ? { target: v, whole: true } : v;
      return h(
        "li",
        null,
        h("code", null, item.target),
        " ",
        Array.isArray(item.cases)
          ? item.cases.map((c) => [h("code", { class: "selector" }, c), " "])
          : h("span", { class: "muted" }, item.whole ? "whole target" : ""),
        item.level ? h("span", { class: "level" }, item.level) : null,
      );
    }),
  );
}

function details(ent) {
  const d = ent.data;
  switch (ent.kind) {
    case "requirement":
      return dl([
        ["Demands", methodView(ent)],
        ["Category", d.category],
        ["Claims", claimsView(d.verified_by)],
        ["Modules", d.modules && d.modules.length ? d.modules.map((m) => h("span", { class: "tag" }, m)) : null],
      ]);
    case "risk": {
      const chain = ["hazard", "hazardous_situation", "harm"].filter((k) => d[k]);
      return h(
        "div",
        null,
        chain.length
          ? h(
              "ol",
              { class: "hazard-chain", "aria-label": "Hazard to harm" },
              chain.map((k) =>
                h(
                  "li",
                  null,
                  h(
                    "span",
                    { class: "hc-label" },
                    k === "hazardous_situation" ? "Hazardous situation" : k[0].toUpperCase() + k.slice(1),
                  ),
                  h("span", { class: "hc-text" }, d[k]),
                ),
              ),
            )
          : null,
        dl([
          ["Estimate", estimate(d.severity, d.likelihood)],
          [
            "Residual",
            d.residual_severity || d.residual_likelihood
              ? estimate(d.residual_severity || d.severity, d.residual_likelihood || d.likelihood)
              : null,
          ],
          ["Residual note", d.residual],
        ]),
      );
    }
    case "mitigation":
      return dl([
        ["Type", d.type ? `${MITIGATION_TYPES[d.type] || d.type}` : null],
        ["Claims", claimsView(d.verified_by)],
      ]);
    case "user_need":
      return d.validated_by && d.validated_by.length ? dl([["Claims", claimsView(d.validated_by)]]) : null;
    case "test_method":
      return dl([
        ["Level", d.level ? h("span", { class: "level" }, d.level) : null],
        ["Procedure", d.procedure ? h("p", { class: "prose" }, d.procedure) : null],
      ]);
    default:
      return null;
  }
}

// --------------------------------------------------------------------------
// Evidence, code, traces
// --------------------------------------------------------------------------

function evidenceTable(ent) {
  const rows = ent.evidence || [];
  const verdict =
    ent.kind === "requirement"
      ? h(
          "p",
          { class: "verdict-line" },
          h("span", { class: "vl" }, "Demanded ", h("span", { class: "level" }, ent.demanded || "—")),
          h(
            "span",
            { class: "vl" },
            "Best passing evidence ",
            ent.provided ? h("span", { class: "level" }, ent.provided) : h("span", { class: "muted" }, "none yet"),
          ),
          ent.stale ? h("span", { class: "badge st-stale" }, "STALE") : null,
          ent.pyramid_violation
            ? h(
                "span",
                {
                  class: "badge st-amber",
                  title: "Demands hardware rigor but has no cheap analysis/simulation backing",
                },
                "no cheap backing",
              )
            : null,
        )
      : null;
  if (ent.verifiable) return h("div", null, verdict, verificationSet(ent));
  if (!rows.length)
    return h(
      "div",
      null,
      verdict,
      empty(
        ent.kind === "requirement"
          ? "No test evidence references this requirement yet."
          : "No evidence tagged directly on this entity.",
      ),
    );
  return h(
    "div",
    null,
    verdict,
    sortableTable({
      columns: [
        { key: "name", label: "Test", render: (r) => h("span", { class: "test-name", title: r.source || "" }, r.name) },
        { key: "target", label: "Target", render: (r) => (r.target ? h("code", null, r.target) : "") },
        { key: "level", label: "Level", render: (r) => h("span", { class: "level" }, r.level) },
        {
          key: "status",
          label: "Result",
          render: (r) =>
            h(
              "span",
              { class: `result ${r.status}` },
              r.status,
              r.stale ? h("span", { class: "badge st-stale" }, "stale") : null,
            ),
        },
        {
          key: "message",
          label: "Message",
          sort: false,
          render: (r) => (r.message ? h("span", { class: "msg" }, r.message) : ""),
        },
      ],
      rows,
    }),
  );
}

// --------------------------------------------------------------------------
// The verification set: the cases attribution gave this entity (one owner
// per case), the cases it expects, and the quarantined cases naming it.
// --------------------------------------------------------------------------

async function moveMember(ent, m) {
  const row = {
    case: m.case,
    path: m.case.slice(m.target.length + 1),
    owner: m.owned ? ent.id : null,
    quarantine: m.state === "quarantined" ? { code: m.reason } : null,
  };
  if (await moveDialog(row)) {
    await reloadModel({ page: false });
    await rerender({ keepScroll: true });
    toast(`Moved ${row.path}`);
  }
}

/** A banner when the verification-set lock no longer matches this set (what `rr sets lock` would change). */
function lockBanner(ent) {
  const lock = ent.lock || {};
  if (!lock.out_of_date) return null;
  if (lock.missing) {
    return h(
      "div",
      { class: "callout amber lock-banner" },
      h("h3", null, "The verification-set lock is missing or cannot be read"),
      h("p", null, `config.sets_lock names ${lock.path}. Generate it with `, h("code", null, "rr sets lock --write"), "."),
    );
  }
  const line = (e) => h("li", null, h("code", { class: "case-key" }, e.case), e.from ? ` ${e.from} → ${e.owner}` : "");
  const part = (label, entries) =>
    entries && entries.length
      ? [h("p", { class: "small" }, `${plural(entries.length, "entry", "entries")} ${label}:`), h("ul", { class: "plain" }, entries.map(line))]
      : null;
  return h(
    "div",
    { class: "callout amber lock-banner" },
    h("h3", null, `The verification-set lock is out of date for ${ent.id}`),
    h(
      "p",
      null,
      `${lock.path} pins the cases of each set; over the loaded evidence it would change. `,
      h("a", { href: "#/cases" }, "Update the lock"),
      " in the case ledger, or run ",
      h("code", null, "rr sets lock --write"),
      ", and review the diff.",
    ),
    part("to add", lock.added),
    part("to change owner", lock.changed),
    part("to remove (kept until confirmed)", lock.removed),
  );
}

function verificationSet(ent) {
  const set = ent.set || {};
  const quarantined = ent.quarantined || [];
  const head = h(
    "p",
    { class: "set-line" },
    h("strong", null, ent.kind === "user_need" ? "Validation set: " : "Verification set: "),
    setLine(set),
    set.complete ? h("span", { class: "badge st-ok" }, "complete") : null,
    ent.basis && ent.basis !== "own" ? h("span", { class: "muted" }, ` · verdict ${ent.basis}`) : null,
  );
  const derived =
    ent.derived_from && ent.derived_from.length
      ? h("p", { class: "derived" }, "Rolls up from ", ent.derived_from.map((id) => [idTag(id), " "]))
      : null;
  const banner = quarantined.length
    ? h(
        "div",
        { class: "callout fail quarantine-banner" },
        h("h3", null, `${plural(quarantined.length, "quarantined case")} — ${ent.id} reads INVALID`),
        h(
          "p",
          null,
          "A test case verifies at most one requirement. These name more than one owner, so they count for nobody " +
            "until each has exactly one.",
        ),
        h(
          "ul",
          { class: "plain quarantine-list" },
          quarantined.map((q) =>
            h(
              "li",
              null,
              h("code", { class: "case-key" }, q.case),
              " ",
              h("span", { class: "mstate st-fail" }, q.code),
              " ",
              q.entities.map((id) => [idTag(id), " "]),
              q.claims && q.claims.length
                ? h(
                    "div",
                    { class: "muted small" },
                    q.claims.map((c) => `${c.entity} claims ${c.selector}${c.location ? ` (${c.location})` : ""}`).join("; "),
                  )
                : null,
            ),
          ),
        ),
      )
    : null;
  const members = ent.members || [];
  return h(
    "div",
    { class: "vset" },
    head,
    derived,
    banner,
    lockBanner(ent),
    members.length
      ? memberTable(members, { onMove: (m) => moveMember(ent, m) })
      : empty(
          ent.kind === "user_need"
            ? "No test case validates this need yet. Claim cases with validated_by."
            : "No test case verifies this yet. Claim cases with verified_by (Edit), or assign one in the case ledger.",
        ),
  );
}

function codeRefs(title, refs, holder) {
  if (!refs || !refs.length) return null;
  return h(
    "div",
    { class: "refs" },
    h("h3", null, title),
    h(
      "ul",
      { class: "plain ref-list" },
      refs.map((r) =>
        h(
          "li",
          null,
          h(
            "button",
            {
              type: "button",
              class: "linkish src",
              title: "Show the code here",
              onClick: () => {
                swap(holder, codeViewer(r.path, r.line, { height: "420px", onClose: () => swap(holder) }));
                holder.scrollIntoView({ block: "nearest", behavior: "smooth" });
              },
            },
            `${r.path}:${r.line}`,
          ),
          r.symbol ? h("span", { class: "symbol" }, r.symbol) : null,
          r.text ? h("span", { class: "ref-text" }, r.text) : null,
          h("a", { class: "quiet-link", href: `#/code?path=${enc(r.path)}&line=${r.line}` }, "open"),
        ),
      ),
    ),
  );
}

function traceTable(title, items, direction) {
  if (!items.length) return null;
  return h(
    "div",
    { class: "traces" },
    h("h3", null, title),
    h(
      "table",
      { class: "grid compact" },
      h(
        "tbody",
        null,
        items.map((t) => {
          const isLevel = t.relation === "method" && t.missing;
          return h(
            "tr",
            null,
            h(
              "td",
              { class: "rel" },
              direction === "out" ? t.relation.replace("_", " ") : `${t.relation.replace("_", " ")} this`,
            ),
            h(
              "td",
              null,
              isLevel
                ? h("span", { class: "level" }, t.id)
                : t.missing
                  ? h("span", { class: "idtag missing" }, t.id, " (undefined)")
                  : idTag(t.id, { showTitle: true }),
            ),
            h("td", { class: "c-status" }, isLevel ? "" : badge(t.status)),
          );
        }),
      ),
    ),
  );
}

// --------------------------------------------------------------------------
// Notes
// --------------------------------------------------------------------------

function notesPanel(ent) {
  const notes = (ent.data.notes || []).slice();
  const kinds = config().note_kinds || ["comment", "gap", "question", "todo"];
  const list = h(
    "ul",
    { class: "notes" },
    notes.length
      ? notes.map((n) =>
          h(
            "li",
            { class: ["note", n.status === "resolved" ? "resolved" : "", `k-${n.kind || "comment"}`] },
            h(
              "div",
              { class: "note-head" },
              h("span", { class: `note-kind k-${n.kind || "comment"}` }, n.kind || "comment"),
              n.status === "resolved" ? h("span", { class: "note-status" }, "resolved") : null,
              h("span", { class: "note-meta" }, [n.author || "", n.created || ""].filter(Boolean).join(", ")),
            ),
            h("p", { class: "note-text" }, n.text),
            h(
              "div",
              { class: "note-actions" },
              button(
                n.status === "resolved" ? "Reopen" : "Resolve",
                () =>
                  mutate(
                    () =>
                      patch(`/api/entities/${enc(ent.id)}/notes/${enc(n.id)}`, {
                        status: n.status === "resolved" ? "open" : "resolved",
                      }),
                    n.status === "resolved" ? "Reopened" : "Resolved",
                  ),
                { small: true, quiet: true },
              ),
              button(
                "Delete",
                async () => {
                  if (await confirmDialog("Delete this note?", n.text.slice(0, 200), "Delete note", true)) {
                    await mutate(() => del(`/api/entities/${enc(ent.id)}/notes/${enc(n.id)}`), "Note deleted");
                  }
                },
                { small: true, quiet: true },
              ),
            ),
          ),
        )
      : h(
          "li",
          { class: "empty-note" },
          "No notes. Notes record gaps, questions and to-dos; open ones join the work queue.",
        ),
  );
  const text = h("textarea", {
    rows: 3,
    placeholder: "Add a note: a gap, a question for review, a to-do…",
    "aria-label": "Note text",
  });
  const kind = h(
    "select",
    { "aria-label": "Note kind" },
    kinds.map((k) => h("option", { value: k }, k)),
  );
  const add = async () => {
    const value = text.value.trim();
    if (!value) {
      text.focus();
      return;
    }
    await mutate(
      () =>
        post(`/api/entities/${enc(ent.id)}/notes`, {
          text: value,
          kind: kind.value,
          author: settings.author || undefined,
        }),
      "Note added",
    );
  };
  text.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && (ev.metaKey || ev.ctrlKey)) {
      ev.preventDefault();
      add();
    }
  });
  const open = notes.filter((n) => n.status !== "resolved").length;
  return h(
    "section",
    { class: "block notes-panel" },
    h("h2", null, "Notes", open ? h("span", { class: "count" }, String(open)) : null),
    list,
    h(
      "div",
      { class: "note-form" },
      text,
      h("div", { class: "row" }, kind, button("Add note", add, { primary: true, small: true })),
    ),
  );
}

async function mutate(fn, message) {
  try {
    await fn();
    await reloadModel({ page: false });
    await rerender({ keepScroll: true });
    if (message) toast(message);
  } catch (err) {
    reportError(err);
  }
}

// --------------------------------------------------------------------------
// Actions
// --------------------------------------------------------------------------

async function doRename(ent) {
  const next = await promptDialog(
    `Rename ${ent.id}`,
    "New id",
    ent.id,
    "Rename",
    "References in the model are updated. Source annotations are not rewritten; the annotation check will flag them.",
  );
  if (!next || next === ent.id) return;
  try {
    const res = await post(`/api/entities/${enc(ent.id)}/rename`, { new_id: next });
    await reloadModel({ page: false });
    toast(
      res.updated_references && res.updated_references.length
        ? `Renamed; updated ${plural(res.updated_references.length, "reference")}`
        : "Renamed",
    );
    go(`#/entity/${enc(next)}`);
  } catch (err) {
    reportError(err);
  }
}

const REF_KEYS = ["satisfies", "refines", "mitigates", "implemented_by", "mitigated_by", "method"];

function referencesTo(id) {
  return store.entities.flatMap((e) =>
    REF_KEYS.filter((k) => refsOf(e, k).includes(id)).map((k) => `${e.id} (${k.replace("_", " ")})`),
  );
}

async function doDelete(ent) {
  const where = ent.location && ent.location.path ? ` from ${ent.location.path}` : "";
  const refs = referencesTo(ent.id);
  if (refs.length) {
    const force = await confirmDialog(
      `${ent.id} is still referenced`,
      `${refs.join(", ")} ${refs.length === 1 ? "refers" : "refer"} to ${ent.id}. Delete it${where} and remove those references too?`,
      "Delete and remove references",
      true,
    );
    if (!force) return;
    try {
      await del(`/api/entities/${enc(ent.id)}?force=1`);
    } catch (err) {
      reportError(err);
      return;
    }
    await reloadModel({ page: false });
    toast(`Deleted ${ent.id} and ${plural(refs.length, "reference")}`);
    go(`#/${KIND[ent.kind].route}`);
    return;
  }
  if (
    !(await confirmDialog(
      `Delete ${ent.id}?`,
      `This removes ${ent.id} "${ent.data.title}"${where}. You can recover it from version control.`,
      `Delete ${ent.id}`,
      true,
    ))
  )
    return;
  try {
    await del(`/api/entities/${enc(ent.id)}`);
  } catch (err) {
    if (err.status !== 409) {
      reportError(err);
      return;
    }
    const force = await confirmDialog(
      `${ent.id} is still referenced`,
      `${err.message}. Delete it and remove those references too?`,
      "Delete and remove references",
      true,
    );
    if (!force) return;
    try {
      await del(`/api/entities/${enc(ent.id)}?force=1`);
    } catch (err2) {
      reportError(err2);
      return;
    }
  }
  await reloadModel({ page: false });
  toast(`Deleted ${ent.id}`);
  go(`#/${KIND[ent.kind].route}`);
}

function agentActions(ent) {
  const llm = (store.state && store.state.llm) || {};
  const options = [];
  if (ent.kind === "requirement") {
    options.push(["Check test adequacy", "test_adequacy", { entities: [ent.id] }]);
    options.push(["Review implementation", "implementation_review", { entities: [ent.id] }]);
  } else if (ent.kind === "risk") {
    options.push(["Check mitigation adequacy", "mitigation_adequacy", { entities: [ent.id] }]);
  } else if (ent.kind === "mitigation") {
    const risks = refsOf(store.byId.get(ent.id), "mitigates");
    if (risks.length) options.push(["Check mitigation adequacy", "mitigation_adequacy", { entities: risks }]);
  }
  if (!options.length) return null;
  return options.map(([label, workflow, params]) =>
    button(
      label,
      async () => {
        try {
          const job = await post("/api/agents/run", { workflow, params });
          go(`#/agents?job=${enc(job.id)}`);
        } catch (err) {
          reportError(err);
        }
      },
      {
        disabled: !llm.available,
        title: llm.available ? `Runs ${workflow} with ${llm.name}` : llm.reason || "No LLM configured",
      },
    ),
  );
}

// --------------------------------------------------------------------------
// Page
// --------------------------------------------------------------------------

export async function renderEntity(id) {
  const ent = await get(`/api/entities/${enc(id)}`);
  const row = store.byId.get(id) || { id, kind: ent.kind, data: ent.data, status: ent.status };
  const d = ent.data;
  const page = h("div", { class: `page entity k-${ent.kind}` });

  add(
    page,
    h(
      "nav",
      { class: "crumbs", "aria-label": "Breadcrumb" },
      h("a", { href: `#/${KIND[ent.kind].route}` }, KIND[ent.kind].many),
      h("span", { "aria-hidden": "true" }, " / "),
      ent.id,
    ),
    h(
      "header",
      { class: "entity-head" },
      h(
        "div",
        { class: "entity-id" },
        glyph(ent.kind, ent.status, `${kindLabel(ent.kind)}, ${ent.status || "no status"}`),
        h("span", { class: "eid" }, ent.id),
        badge(ent.status),
        ent.stale ? h("span", { class: "badge st-stale" }, "STALE") : null,
        ent.pyramid_violation ? h("span", { class: "badge st-amber" }, "no cheap backing") : null,
      ),
      h("h1", null, d.title),
      h(
        "p",
        { class: "entity-meta" },
        h("span", null, kindLabel(ent.kind)),
        d.status ? h("span", null, `lifecycle: ${d.status}`) : null,
        d.owner ? h("span", null, `owner: ${d.owner}`) : null,
        ent.location && ent.location.path
          ? h("span", null, "defined in ", sourceRef(ent.location.path, ent.location.line))
          : null,
        d.tags && d.tags.length
          ? h(
              "span",
              null,
              d.tags.map((t) => h("span", { class: "tag" }, t)),
            )
          : null,
      ),
      h(
        "div",
        { class: "actions" },
        button("Edit", () => go(`#/edit/${enc(ent.id)}`), { primary: true }),
        button("Rename", () => doRename(ent)),
        button("Show in graph", () => go(`#/graph?focus=${enc(ent.id)}&depth=2`)),
        agentActions(ent),
        button("Delete", () => doDelete(ent), { danger: true, quiet: true }),
      ),
    ),
  );

  if (ent.kind !== "test_method" || chainFor(row).lanes.requirement.length) add(page, traceStrip(row));

  const main = h("div", { class: "entity-main" });
  const aside = h("aside", { class: "entity-aside" });

  const prose = [];
  if (d.description) prose.push(h("p", { class: "prose" }, d.description));
  if (d.rationale)
    prose.push(h("div", { class: "rationale" }, h("h3", null, "Rationale"), h("p", { class: "prose" }, d.rationale)));
  const facts = details(ent);
  if (prose.length || facts) add(main, h("section", { class: "block" }, h("h2", null, "Statement"), prose, facts));

  if (ent.kind !== "risk" && ent.kind !== "test_method") {
    add(
      main,
      h(
        "section",
        { class: "block" },
        h("h2", null, ent.kind === "user_need" ? "Validation evidence" : "Verification"),
        evidenceTable(ent),
      ),
    );
  }

  const holder = h("div", { class: "code-holder" });
  const impl = codeRefs("Implemented in", ent.implemented_in, holder);
  const ver = codeRefs("Verified in", ent.verified_in, holder);
  if (impl || ver || ent.kind === "requirement") {
    add(
      main,
      h(
        "section",
        { class: "block" },
        h("h2", null, "Code"),
        impl || ver
          ? [impl, ver]
          : empty(
              store.state.annotations_scanned
                ? "No source annotation names this requirement. Tag the implementing code with @rr(" + ent.id + ")."
                : "Source annotations were not scanned.",
            ),
        holder,
      ),
    );
  }

  const out = traceTable("References", ent.outgoing || [], "out");
  const inc = traceTable("Referenced by", ent.incoming || [], "in");
  if (out || inc) add(main, h("section", { class: "block" }, h("h2", null, "Traces"), out, inc));

  const problems = [
    ...(ent.issues || []).map((i) => ({ sev: i.severity, text: i.message, code: i.code })),
    ...(ent.gaps || []).map((g) => ({ sev: g.route === "human-gate" ? "gate" : "gap", text: g.message, code: g.kind })),
  ];
  if (problems.length) {
    main.prepend(
      h(
        "section",
        { class: "block problems" },
        h("h2", null, "Needs attention"),
        h(
          "ul",
          { class: "issues" },
          problems.map((p) =>
            h(
              "li",
              { class: `issue ${p.sev}` },
              h("span", { class: `sev ${p.sev}` }, p.sev === "gate" ? "human gate" : p.sev),
              h("code", { class: "code-tag" }, p.code),
              h("span", { class: "issue-msg" }, p.text),
              h("span"),
            ),
          ),
        ),
      ),
    );
  }

  add(aside, notesPanel(ent));
  add(page, h("div", { class: "entity-body" }, main, aside));
  return page;
}
