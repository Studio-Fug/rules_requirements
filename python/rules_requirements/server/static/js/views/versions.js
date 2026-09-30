// SPDX-License-Identifier: AGPL-3.0-or-later
// Versions: what changed in the model, baselines, and committing edits.

import { enc, get, post, settings } from "../api.js";
import { button, empty, glyph, idTag, reportError, toast } from "../components.js";
import { add, h, plural } from "../dom.js";
import { go, hashOf, reloadModel } from "../nav.js";
import { KIND, store } from "../store.js";

const WORKTREE = "WORKTREE";

/** Full commit ids are noise in labels: show 7 characters (keeping ~N suffixes). */
function shortRef(ref) {
  return String(ref).replace(/\b([0-9a-f]{7})[0-9a-f]{33}\b/g, "$1");
}

function fmt(value) {
  if (value === null || value === undefined) return "";
  if (typeof value === "object") {
    if (value.text !== undefined)
      return `${value.kind || "comment"}: ${value.text}${value.status === "resolved" ? " (resolved)" : ""}`;
    if (value.target !== undefined) return value.level ? `${value.target} (${value.level})` : value.target;
    return JSON.stringify(value);
  }
  return String(value);
}

/** Old -> new for one field; lists are shown as token diffs. */
function fieldDiff(name, change) {
  const { old: before, new: after } = change;
  let body;
  if (Array.isArray(before) || Array.isArray(after)) {
    const a = (before || []).map(fmt);
    const b = (after || []).map(fmt);
    const tokens = [];
    for (const t of a) tokens.push(b.includes(t) ? h("span", { class: "tok same" }, t) : h("del", { class: "tok" }, t));
    for (const t of b) if (!a.includes(t)) tokens.push(h("ins", { class: "tok" }, t));
    body = h("span", { class: "toks" }, tokens.length ? tokens : h("span", { class: "muted" }, "empty"));
  } else if (before === null || before === undefined) {
    body = h("ins", { class: "val" }, fmt(after));
  } else if (after === null || after === undefined) {
    body = h("del", { class: "val" }, fmt(before));
  } else {
    body = h(
      "span",
      { class: "vals" },
      h("del", { class: "val" }, fmt(before)),
      h("span", { class: "arrow", "aria-label": "becomes" }, "→"),
      h("ins", { class: "val" }, fmt(after)),
    );
  }
  return h("tr", null, h("th", { scope: "row" }, name.replace(/_/g, " ")), h("td", null, body));
}

function changeCard(c) {
  const mark = { added: "+", removed: "−", modified: "~" }[c.change];
  const exists = store.byId.has(c.id);
  const fields = Object.entries(c.fields || {});
  return h(
    "li",
    { class: `change ${c.change}` },
    h(
      "div",
      { class: "change-head" },
      h("span", { class: "change-mark", "aria-label": c.change }, mark),
      glyph(c.kind),
      exists ? idTag(c.id, { compact: true }) : h("span", { class: "idtag missing" }, c.id),
      h("span", { class: "change-title" }, c.title),
      h("span", { class: "muted" }, `${KIND[c.kind] ? KIND[c.kind].one.toLowerCase() : c.kind} ${c.change}`),
    ),
    c.change === "modified" && fields.length
      ? h(
          "table",
          { class: "field-diff" },
          h(
            "tbody",
            null,
            fields.map(([k, v]) => fieldDiff(k, v)),
          ),
        )
      : null,
  );
}

async function diffPanel(from, to) {
  const holder = h("div", { class: "diff-result" });
  try {
    const d = await get(`/api/diff?from=${enc(from)}&to=${enc(to)}`);
    const s = d.summary || {};
    add(
      holder,
      h(
        "p",
        { class: "diff-summary" },
        h("strong", null, from === WORKTREE ? "working tree" : shortRef(from)),
        " to ",
        h("strong", null, to === WORKTREE ? "working tree" : shortRef(to)),
        ": ",
        h("span", { class: "add" }, `${s.added || 0} added`),
        ", ",
        h("span", { class: "rem" }, `${s.removed || 0} removed`),
        ", ",
        h("span", { class: "mod" }, `${s.modified || 0} modified`),
      ),
      d.changes.length
        ? h("ul", { class: "changes" }, d.changes.map(changeCard))
        : empty("No changes to the model between these versions."),
    );
  } catch (err) {
    add(holder, h("p", { class: "form-error" }, err.message));
  }
  return holder;
}

function refSelect(refs, value, label) {
  const commits = refs.commits || [];
  const opts = [
    h("option", { value: WORKTREE, selected: value === WORKTREE }, "Working tree (unsaved to git)"),
    h("option", { value: "HEAD", selected: value === "HEAD" }, `HEAD (${refs.head || "current"})`),
    refs.branches.length
      ? h(
          "optgroup",
          { label: "Branches" },
          refs.branches.map((b) => h("option", { value: b, selected: b === value }, b)),
        )
      : null,
    refs.tags.length
      ? h(
          "optgroup",
          { label: "Baselines (tags)" },
          refs.tags.map((t) => h("option", { value: t.name, selected: t.name === value }, t.name)),
        )
      : null,
    commits.length
      ? h(
          "optgroup",
          { label: "Recent commits" },
          commits.map((c) =>
            h("option", { value: c.sha, selected: c.sha === value }, `${c.short} ${c.subject.slice(0, 50)}`),
          ),
        )
      : null,
  ];
  const known = [WORKTREE, "HEAD", ...refs.branches, ...refs.tags.map((t) => t.name), ...commits.map((c) => c.sha)];
  if (value && !known.includes(value)) opts.push(h("option", { value, selected: true }, shortRef(value)));
  return h("select", { "aria-label": label }, opts);
}

export async function renderVersions(query) {
  const [status, refs, log] = await Promise.all([
    get("/api/git/status"),
    get("/api/git/refs"),
    get("/api/git/log?limit=25"),
  ]);
  const page = h("div", { class: "page versions" });
  add(
    page,
    h(
      "header",
      { class: "page-head" },
      h("h1", null, "Versions"),
      h(
        "p",
        { class: "lede" },
        "Compare the model across branches, baselines and commits, name baselines, and commit your edits.",
      ),
    ),
  );

  if (!status.git) {
    add(
      page,
      empty(
        "This workspace is not a git checkout, so there is no history to compare. Model edits are still saved to the files.",
      ),
    );
    return page;
  }

  // Working tree + commit
  const changed = status.changed || [];
  const message = h("input", {
    type: "text",
    placeholder: "Describe the change, e.g. Add REQ-8 for setpoint logging",
    "aria-label": "Commit message",
  });
  const commitBtn = button(
    "Commit model changes",
    async () => {
      if (!message.value.trim()) {
        message.focus();
        toast("Write a commit message first", "error");
        return;
      }
      try {
        const res = await post("/api/git/commit", {
          message: message.value.trim(),
          author: settings.author || undefined,
        });
        toast(`Committed ${res.commit}`);
        await reloadModel();
      } catch (err) {
        reportError(err);
      }
    },
    { primary: true, disabled: !changed.length },
  );
  message.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") commitBtn.click();
  });
  add(
    page,
    h(
      "section",
      { class: "block worktree" },
      h(
        "h2",
        null,
        "Working tree",
        h("span", { class: "branch-name" }, status.branch || "detached"),
        h("span", { class: "muted" }, ` at ${status.head}`),
      ),
      changed.length
        ? [
            h("p", null, `${plural(changed.length, "model file")} changed since the last commit:`),
            h(
              "ul",
              { class: "plain files" },
              changed.map((f) => h("li", null, h("code", null, f))),
            ),
            h("div", { class: "commit-form" }, message, commitBtn),
            h(
              "p",
              { class: "muted small" },
              `Only model files are committed. Author: ${settings.author || (store.state && store.state.author) || status.user || "your git identity"}.`,
            ),
          ]
        : h("p", { class: "muted" }, "No uncommitted model changes."),
    ),
  );

  // Compare
  const from = query.get("from") || (!changed.length && refs.tags.length ? refs.tags[0].name : "HEAD");
  const to = query.get("to") || WORKTREE;
  const refsWithLog = { ...refs, commits: log.commits || [] };
  const fromSel = refSelect(refsWithLog, from, "Compare from");
  const toSel = refSelect(refsWithLog, to, "Compare to");
  const compare = () => go(hashOf("versions", { from: fromSel.value, to: toSel.value }));
  fromSel.addEventListener("change", compare);
  toSel.addEventListener("change", compare);
  add(
    page,
    h(
      "section",
      { class: "block" },
      h("h2", null, "Compare the model"),
      h(
        "div",
        { class: "toolbar compare" },
        h("label", null, "From ", fromSel),
        h("label", null, "to ", toSel),
        button("Swap", () => go(hashOf("versions", { from: toSel.value, to: fromSel.value })), { small: true }),
      ),
      await diffPanel(from, to),
    ),
  );

  // Baselines
  const tagName = h("input", { type: "text", placeholder: "baseline/v1.0", "aria-label": "Baseline name" });
  const tagMsg = h("input", {
    type: "text",
    placeholder: "What this baseline is (optional)",
    "aria-label": "Baseline description",
  });
  const tagRef = refSelect({ ...refsWithLog, branches: refs.branches }, "HEAD", "Commit to name");
  tagRef.querySelector(`option[value="${WORKTREE}"]`).remove();
  add(
    page,
    h(
      "section",
      { class: "block" },
      h("h2", null, "Baselines"),
      h(
        "p",
        { class: "muted" },
        "A baseline is a named git tag on a commit: the model as reviewed, released or submitted.",
      ),
      refs.tags.length
        ? h(
            "table",
            { class: "grid compact" },
            h(
              "thead",
              null,
              h(
                "tr",
                null,
                ["Name", "Date", "Description", "Commit", ""].map((c) => h("th", { scope: "col" }, c)),
              ),
            ),
            h(
              "tbody",
              null,
              refs.tags.map((t) =>
                h(
                  "tr",
                  null,
                  h("td", null, h("strong", null, t.name)),
                  h("td", null, t.date),
                  h("td", null, t.message),
                  h("td", null, h("code", null, t.sha)),
                  h(
                    "td",
                    { class: "actions-cell" },
                    h("a", { href: hashOf("versions", { from: t.name, to: WORKTREE }) }, "Compare with working tree"),
                  ),
                ),
              ),
            ),
          )
        : h("p", { class: "muted" }, "No baselines yet."),
      h(
        "div",
        { class: "tag-form" },
        tagName,
        tagMsg,
        tagRef,
        button(
          "Name baseline",
          async () => {
            if (!tagName.value.trim()) {
              tagName.focus();
              return;
            }
            try {
              await post("/api/git/tag", {
                name: tagName.value.trim(),
                message: tagMsg.value.trim(),
                ref: tagRef.value,
              });
              toast(`Named baseline ${tagName.value.trim()}`);
              go(hashOf("versions", { from: tagName.value.trim(), to: WORKTREE }));
            } catch (err) {
              reportError(err);
            }
          },
          { primary: true },
        ),
      ),
    ),
  );

  // History
  const commits = log.commits || [];
  add(
    page,
    h(
      "section",
      { class: "block" },
      h("h2", null, "Model history"),
      commits.length
        ? h(
            "ol",
            { class: "history" },
            commits.map((c, i) =>
              h(
                "li",
                null,
                h("code", null, c.short),
                h(
                  "a",
                  { href: hashOf("versions", { from: i === commits.length - 1 ? c.sha : `${c.sha}~1`, to: c.sha }) },
                  c.subject,
                ),
                h("span", { class: "muted" }, `${c.author}, ${c.date}`),
              ),
            ),
          )
        : h("p", { class: "muted" }, "No commits touch the model files yet."),
    ),
  );
  return page;
}
