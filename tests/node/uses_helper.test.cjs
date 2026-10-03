// SPDX-License-Identifier: AGPL-3.0-or-later
// rr_node_test fixture: a test file whose tests are partly defined in a helper.
const { test } = require("node:test");
const { defineCases } = require("./helper_cases.cjs");

test("own", () => {});
defineCases();
