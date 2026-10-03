// SPDX-License-Identifier: AGPL-3.0-or-later
// rr_node_test fixture: tests defined in a helper module, not the test file
// (their rr.file names this file). Used by uses_helper.test.cjs.
"use strict";
const { test } = require("node:test");

exports.defineCases = () => {
  test("defined in a helper", (t) => {
    t.diagnostic("rr.requirement=REQ-9");
  });
};
