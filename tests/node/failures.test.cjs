// SPDX-License-Identifier: AGPL-3.0-or-later
// rr_node_test fixture: failing cases, and failures outside any case.
const assert = require("node:assert");
const { before, describe, test } = require("node:test");

test("fails", () => {
  assert.strictEqual(1, 2);
});

test("passes", () => {});

describe("before hook fails", () => {
  before(() => {
    throw new Error("setup broke");
  });
  test("never runs", () => {});
});

test("parent fails after its subtests", async (t) => {
  await t.test("sub passes", () => {});
  throw new Error("parent body broke");
});

test("parent of a failing subtest", async (t) => {
  await t.test("sub fails", () => {
    assert.fail("sub broke");
  });
});
