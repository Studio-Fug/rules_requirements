// SPDX-License-Identifier: AGPL-3.0-or-later
// rr_node_test fixture: a test below its package's directory, like splanc's
// compiled `dist-test/tests/*.test.js`, with the verifies helper.
"use strict";
const { describe, it } = require("node:test");
const { verifies } = require(process.env.RR_NODE_VERIFIES);

describe("compiled", () => {
  it("keeps the min-RTT sample", (t) => {
    verifies(t, "REQ-8");
  });
});
