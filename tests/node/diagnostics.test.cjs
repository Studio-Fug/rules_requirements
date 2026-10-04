// SPDX-License-Identifier: AGPL-3.0-or-later
// rr_node_test fixture: rr diagnostics become properties of their own case.
const { after, before, describe, test } = require("node:test");
// rr_node_test sets RR_NODE_VERIFIES; elsewhere (`node --test`, an IDE) the
// file still loads, without the helper's guards.
const { verifies } = process.env.RR_NODE_VERIFIES
  ? require(process.env.RR_NODE_VERIFIES)
  : { verifies: (t, id) => t.diagnostic(`rr.requirement=${id}`) };

before((t) => {
  t.diagnostic("rr.requirement=REQ-from-a-hook"); // belongs to no case: a warning
});

after((t) => {
  t.diagnostic("rr.requirement=REQ-from-a-root-after-hook"); // after the last case: a warning too
});

test("raw diagnostics", (t) => {
  t.diagnostic("rr.requirement=REQ-1");
  t.diagnostic("rr.level=hil");
  t.diagnostic("rr.artifact.board_rev=C");
  t.diagnostic("not an rr diagnostic");
});

test("the verifies helper", (t) => {
  verifies(t, "REQ-2", "sil");
  verifies(t, "REQ-2"); // the same id again is fine
});

test("verifies refuses a second id", (t) => {
  verifies(t, "REQ-3");
  verifies(t, "REQ-4"); // throws RR-E101: this case fails
});

test("verifies refuses an id list", (t) => {
  verifies(t, "REQ-5, REQ-6"); // throws RR-E104: this case fails
});

test("no diagnostics", () => {});

describe("after a sibling's diagnostics", () => {
  test("first", (t) => {
    t.diagnostic("rr.requirement=REQ-7");
  });
  test("second", () => {});
});
