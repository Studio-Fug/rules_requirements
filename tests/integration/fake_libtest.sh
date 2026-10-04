#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# A checked-in "test binary" printing libtest output: rr_wrapped_test's `test`
# may be a plain source file (or a genrule output), as in 0.1.0.
echo "running 1 test"
echo "test fake::passes ... ok"
echo
echo "test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out"
