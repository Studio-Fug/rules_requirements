// SPDX-License-Identifier: AGPL-3.0-or-later
// rr_node_test fixture: a failing root after() hook (node still exits 0).
const { after, test } = require("node:test");

test("passes", () => {});

after(() => {
  throw new Error("after hook fails");
});
