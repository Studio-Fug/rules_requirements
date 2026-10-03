// SPDX-License-Identifier: AGPL-3.0-or-later
// rr_node_test fixture: the file throws while it is loaded.
const { test } = require("node:test");

test("registered before the throw", () => {});

throw new Error("boom at load");
