// SPDX-License-Identifier: AGPL-3.0-or-later
// Implementation browser: every @rr(...) annotation in the source tree.

import { enc, get } from "../api.js";
import { codeViewer, empty, idTag } from "../components.js";
import { add, h, natural, plural, swap } from "../dom.js";
import { hashOf } from "../nav.js";

function annotationRow(a) {
  return h(
    "tr",
    { class: a.unknown && a.unknown.length ? "bad" : "" },
    h("td", { class: "num" }, h("a", { href: `#/code?path=${enc(a.path)}&line=${a.line}` }, String(a.line))),
    h("td", null, h("span", { class: `rel-chip ${a.relation}` }, a.relation)),
    h(
      "td",
      null,
      a.ids.map((id) => [
        a.unknown && a.unknown.includes(id)
          ? h("span", { class: "idtag missing", title: "Not defined in the model" }, id)
          : idTag(id, { compact: true }),
        " ",
      ]),
    ),
    h("td", null, a.symbol ? h("span", { class: "symbol" }, a.symbol) : ""),
    h("td", { class: "ref-text" }, a.text || ""),
  );
}

export async function renderCode(query) {
  const res = await get("/api/annotations");
  const all = res.annotations || [];
  const path = query.get("path") || "";
  const line = Number(query.get("line") || 0);
  const page = h("div", { class: "page code-page" });

  if (path) {
    const here = all.filter((a) => a.path === path).sort((a, b) => a.line - b.line);
    add(
      page,
      h(
        "header",
        { class: "page-head" },
        h(
          "nav",
          { class: "crumbs", "aria-label": "Breadcrumb" },
          h("a", { href: "#/code" }, "Implementation"),
          " / ",
          path,
        ),
        h("h1", { class: "path-title" }, path.split("/").pop()),
        h("p", { class: "muted" }, path, here.length ? `, ${plural(here.length, "annotation")}` : ""),
      ),
      codeViewer(path, line, { height: "62vh" }),
    );
    if (here.length) {
      add(
        page,
        h(
          "section",
          { class: "block" },
          h("h2", null, "Annotations in this file"),
          h(
            "div",
            { class: "table-wrap" },
            h(
              "table",
              { class: "grid compact" },
              h(
                "thead",
                null,
                h(
                  "tr",
                  null,
                  ["Line", "Relation", "Ids", "Symbol", "Description"].map((c) => h("th", { scope: "col" }, c)),
                ),
              ),
              h("tbody", null, here.map(annotationRow)),
            ),
          ),
        ),
      );
    }
    return page;
  }

  add(
    page,
    h(
      "header",
      { class: "page-head" },
      h("h1", null, "Implementation", h("span", { class: "count" }, String(all.length))),
      h(
        "p",
        { class: "lede" },
        "Source annotations such as ",
        h("code", null, "# @rr(REQ-1): what this code does"),
        " link code and tests to the model. Unknown ids are errors: the build's annotation check fails on them.",
      ),
    ),
  );
  if (!res.scanned) {
    add(page, empty("Source annotations were not scanned. Restart rr serve without --no-scan to browse them."));
    return page;
  }
  if (!all.length) {
    add(
      page,
      empty(
        "No annotations found yet. Tag implementing code with @rr(REQ-…) comments and tests with @rr.verifies(…) or the framework hooks.",
      ),
    );
    return page;
  }

  const filter = h("input", {
    type: "search",
    value: query.get("q") || "",
    placeholder: "Filter by id, path or symbol",
    "aria-label": "Filter annotations",
  });
  const relation = h(
    "select",
    { "aria-label": "Relation" },
    [
      ["", "Any relation"],
      ["implements", "Implements"],
      ["verifies", "Verifies"],
    ].map(([v, l]) => h("option", { value: v, selected: v === (query.get("relation") || "") }, l)),
  );
  const onlyBad = h("input", { type: "checkbox", checked: query.get("unknown") === "1" });
  const results = h("div", { class: "file-groups" });
  const unknownCount = all.filter((a) => a.unknown && a.unknown.length).length;

  function draw() {
    const q = filter.value.trim().toLowerCase();
    const rows = all.filter(
      (a) =>
        (!relation.value || a.relation === relation.value) &&
        (!onlyBad.checked || (a.unknown && a.unknown.length)) &&
        (!q ||
          a.path.toLowerCase().includes(q) ||
          (a.symbol || "").toLowerCase().includes(q) ||
          a.ids.some((id) => id.toLowerCase().includes(q)) ||
          (a.text || "").toLowerCase().includes(q)),
    );
    const byFile = new Map();
    for (const a of rows) {
      if (!byFile.has(a.path)) byFile.set(a.path, []);
      byFile.get(a.path).push(a);
    }
    const files = [...byFile.keys()].sort(natural);
    swap(
      results,
      ...(files.length
        ? files.map((f) =>
            h(
              "section",
              { class: "file-group" },
              h(
                "h2",
                { class: "file-head" },
                h("a", { href: `#/code?path=${enc(f)}` }, f),
                h("span", { class: "count" }, String(byFile.get(f).length)),
              ),
              h(
                "div",
                { class: "table-wrap" },
                h(
                  "table",
                  { class: "grid compact" },
                  h(
                    "tbody",
                    null,
                    byFile
                      .get(f)
                      .sort((a, b) => a.line - b.line)
                      .map(annotationRow),
                  ),
                ),
              ),
            ),
          )
        : [empty("No annotations match.")]),
    );
    history.replaceState(
      null,
      "",
      hashOf("code", { q: filter.value.trim(), relation: relation.value, unknown: onlyBad.checked ? "1" : "" }),
    );
  }
  filter.addEventListener("input", draw);
  relation.addEventListener("change", draw);
  onlyBad.addEventListener("change", draw);
  add(
    page,
    h(
      "div",
      { class: "toolbar" },
      filter,
      relation,
      h("label", { class: "check" }, onlyBad, `Only unknown ids (${unknownCount})`),
      h(
        "span",
        { class: "muted" },
        `${plural(new Set(all.map((a) => a.path)).size, "file")}, ${plural(
          all.reduce((n, a) => n + a.ids.length, 0),
          "reference",
        )}`,
      ),
    ),
    results,
  );
  draw();
  return page;
}
