// SPDX-License-Identifier: AGPL-3.0-or-later
// Optional node:test helper: `verifies(t, "PR-13")` tags the running test.
//
//   const { verifies } = require(process.env.RR_NODE_VERIFIES); // set by rr_node_test
//   test("bestSample keeps the min-RTT sample", (t) => {
//     verifies(t, "PR-13");
//     ...
//   });
//
// It writes the diagnostic `rr.requirement=<id>` (and `rr.level=<level>`),
// which rr_node_test's reporter turns into the case's `requirement` (`level`)
// property — exactly what a hand-written `t.diagnostic(...)` does, plus two
// guards: an id is ONE id, and a test verifies at most one requirement.
"use strict";

const seen = new WeakMap();

exports.verifies = (t, id, level) => {
  if (typeof id !== "string" || !id.trim() || /[\s,]/.test(id)) {
    throw new TypeError(`rr.verifies: '${id}' is not ONE id [RR-E104]`);
  }
  const prev = seen.get(t);
  if (prev && prev !== id) throw new Error(`rr.verifies(${id}): test already verifies ${prev} [RR-E101]`);
  seen.set(t, id);
  t.diagnostic(`rr.requirement=${id}`);
  if (level) t.diagnostic(`rr.level=${level}`);
};
