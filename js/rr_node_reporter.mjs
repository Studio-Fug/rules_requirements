// SPDX-License-Identifier: AGPL-3.0-or-later
// node:test reporter behind rr_node_test: one JSON line per test case.
//
// The runner (node_test_main.cjs.tpl) passes this module to the test process
// as a second `--test-reporter` next to `spec`. It yields nothing — the human
// output is spec's — and appends one JSON line per event of interest to
// $RR_CASES_OUT, which the runner renders as JUnit once the process exits:
//
//   {"kind":"case", "i":N, "classname", "name", "status", "message", ...}
//       a leaf test (no subtests of its own) passed, failed or was skipped
//       (`skip` and `todo` are both "skipped"); a skipped describe, whose
//       tests node never reports, is one skipped case named after it;
//   {"kind":"diag", "case":N, "key", "value"}
//       an `rr.<key>=<value>` diagnostic of case N (`t.diagnostic(...)`);
//   {"kind":"scope", "classname", "name", "message"}
//       a failure that belongs to no case: a describe or parent test failing
//       outside its subtests (`<hooks>`), or a root-level after() hook (`<file>`);
//   {"kind":"warning", "message"}
//       an rr diagnostic that cannot be tied to a case;
//   {"kind":"start"}, {"kind":"end"}
//       node:test started and finished reporting (the event stream ended). A
//       process that exits mid-run (`process.exit(0)` in a test) never writes
//       the end; a file that registers no test writes neither.
//
// Node guarantees that test:start, test:pass/test:fail and test:diagnostic
// arrive in declaration order, and a test's diagnostics right after its own
// pass/fail with the same nesting, line and column, also under `concurrency`.
// A hook's diagnostics follow a test:plan (a root after()) or the pass of a
// describe, so they never match a case.
import fs from "node:fs";
import path from "node:path";
import { inspect } from "node:util";

// `rr.requirement=PR-13`, `rr.level=hil`, `rr.artifact.board_rev=C`.
const RR_DIAGNOSTIC = /^rr\.(requirement|level|artifact\.[\w.-]+)=(.*)$/s;

// `clocksync.test.js` -> `clocksync`; the runner passes the same stem.
export function fileStem(file) {
  const base = path.basename(file);
  return base.replace(/\.(test|spec)\.[cm]?[jt]s$/, "").replace(/\.[cm]?[jt]s$/, "");
}

function errorText(data) {
  const err = data.details?.error;
  if (err === undefined || err === null) return "";
  // node:test wraps what the test threw in ERR_TEST_FAILURE; report the cause.
  const cause = err.cause ?? err;
  if (cause instanceof Error && typeof cause.stack === "string" && cause.stack) return cause.stack;
  // `throw { code: 1 }` reads `{ code: 1 }`, not `[object Object]`.
  return typeof cause === "string" ? cause : inspect(cause);
}

export default async function* rrCases(source) {
  const out = process.env.RR_CASES_OUT;
  const testFile = process.env.RR_TEST_FILE || "";
  const stem = process.env.RR_TEST_STEM || fileStem(testFile);
  const emit = (row) => {
    if (out) fs.appendFileSync(out, JSON.stringify(row) + "\n");
  };
  const stack = []; // the open test:start chain, by nesting: {name, line, children}
  let index = 0;
  let last = null; // the most recently completed case: diagnostics follow it
  emit({ kind: "start" });
  for await (const event of source) {
    const data = event.data ?? {};
    if (event.type === "test:start" || event.type === "test:plan") {
      // Whatever comes next belongs to another test, or to a hook.
      last = null;
    }
    if (event.type === "test:start") {
      stack.length = data.nesting;
      if (data.nesting > 0 && stack[data.nesting - 1]) stack[data.nesting - 1].children++;
      stack[data.nesting] = { name: data.name, line: data.line, children: 0 };
    } else if (event.type === "test:pass" || event.type === "test:fail") {
      const failed = event.type === "test:fail";
      const me = stack[data.nesting];
      const started = me !== undefined && me.name === data.name && me.line === data.line;
      const chain = stack.slice(0, data.nesting).map((s) => s.name);
      const skipped = data.skip !== undefined || data.todo !== undefined;
      last = null;
      if (!started) {
        // A test:fail without a test:start: a root-level after() hook.
        if (failed) {
          const what = !data.name || data.name === data.file ? "file" : data.name;
          emit({ kind: "scope", classname: stem, name: `<${what}>`, message: errorText(data), file: data.file || testFile });
        }
      } else if ((data.details?.type === "suite" && !(me.children === 0 && skipped)) || me.children > 0) {
        // A describe, or a test with subtests: its leaves are the cases. Its
        // own failure counts only when it is not just "a subtest failed". (A
        // skipped describe has no leaves — node never starts them — so it is
        // a case itself, below.)
        if (failed && data.details?.error?.failureType !== "subtestsFailed") {
          emit({
            kind: "scope",
            classname: [stem, ...chain, data.name].join(" > "),
            name: "<hooks>",
            message: errorText(data),
            file: data.file || testFile,
            line: data.line,
          });
        }
      } else {
        const reason = typeof data.skip === "string" ? data.skip : typeof data.todo === "string" ? data.todo : "";
        last = { i: index++, nesting: data.nesting, line: data.line, column: data.column, file: data.file };
        emit({
          kind: "case",
          i: last.i,
          classname: [stem, ...chain].join(" > "),
          name: data.name,
          status: skipped ? "skipped" : failed ? "failed" : "passed",
          message: skipped ? reason : failed ? errorText(data) : "",
          duration: (data.details?.duration_ms ?? 0) / 1000,
          file: data.file || testFile,
          line: data.line,
          column: data.column,
        });
      }
      stack.length = data.nesting;
    } else if (event.type === "test:diagnostic") {
      const m = RR_DIAGNOSTIC.exec(data.message ?? "");
      if (!m) continue;
      const own =
        last !== null &&
        last.nesting === data.nesting &&
        last.line === data.line &&
        last.column === data.column &&
        data.file === last.file;
      if (own) {
        emit({ kind: "diag", case: last.i, key: m[1], value: m[2].trim() });
      } else {
        // E.g. a diagnostic from a hook or after the test ended: never guessed.
        emit({ kind: "warning", message: `uncorrelated rr diagnostic '${m[0]}' (nesting ${data.nesting}, line ${data.line})` });
      }
    }
  }
  emit({ kind: "end" });
}
