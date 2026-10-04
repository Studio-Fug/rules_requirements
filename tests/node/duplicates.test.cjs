// SPDX-License-Identifier: AGPL-3.0-or-later
// rr_node_test fixture: two tests with one name stay two cases.
const assert = require("node:assert");
const { test } = require("node:test");

for (const ok of [true, false]) {
  test("same name", () => {
    assert.ok(ok, "the second one fails");
  });
}
