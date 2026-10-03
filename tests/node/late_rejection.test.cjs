// SPDX-License-Identifier: AGPL-3.0-or-later
// rr_node_test fixture: an unhandled rejection after the tests passed.
const { test } = require("node:test");

test("passes", () => {});

setTimeout(() => {
  Promise.reject(new Error("late unhandled rejection"));
}, 50);
