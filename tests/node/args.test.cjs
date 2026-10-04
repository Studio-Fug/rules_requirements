// SPDX-License-Identifier: AGPL-3.0-or-later
// rr_node_test fixture: baked `args` reach the test file.
const assert = require("node:assert");
const { test } = require("node:test");

test("sees its arguments", () => {
  assert.deepStrictEqual(process.argv.slice(2), ["--mode", "fast"]);
});
