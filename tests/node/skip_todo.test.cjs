// SPDX-License-Identifier: AGPL-3.0-or-later
// rr_node_test fixture: skipped and todo tests are "skipped" cases.
const { test } = require("node:test");

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
