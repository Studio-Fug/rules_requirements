// SPDX-License-Identifier: AGPL-3.0-or-later
// rr_node_test fixture: nesting, subtests and concurrency.
const { describe, it, test } = require("node:test");

test("top level", () => {});

describe("outer", () => {
  it("in a describe", () => {});
  describe("inner", () => {
    it("two levels down", () => {});
  });
});

test("parent with subtests", async (t) => {
  await t.test("sub a", () => {});
  await t.test("sub b", async (t2) => {
    await t2.test("sub sub", () => {});
  });
});

describe("concurrent", { concurrency: 3 }, () => {
  for (const [name, ms] of [
    ["slow", 40],
    ["fast", 1],
    ["medium", 15],
  ]) {
    it(name, async (t) => {
      t.diagnostic(`rr.requirement=REQ-${name}`);
      await new Promise((resolve) => setTimeout(resolve, ms));
    });
  }
});
