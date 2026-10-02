// SPDX-License-Identifier: AGPL-3.0-or-later
// rr_node_test fixture: skipped and todo tests are "skipped" cases.
const { describe, it, test } = require("node:test");

test("runs", () => {});
test("skipped with a reason", { skip: "no hardware" }, () => {});
test("skipped", { skip: true }, () => {});
test("todo with a reason", { todo: "not written yet" }, () => {});
test("skipped at run time", (t) => {
  t.skip("decided inside the test");
});
test("todo at run time", (t) => {
  t.todo("marked inside the test");
});

// Node never reports the tests of a skipped describe: the describe is the case.
describe.skip("skipped suite", () => {
  it("never reported", () => {});
  it("never reported either", () => {});
});
describe("outer", () => {
  describe("skipped suite with a reason", { skip: "no bench" }, () => {
    it("never reported", () => {});
  });
});
