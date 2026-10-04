#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# A "test binary" whose only test passes, and which then exits non-zero (a
# crash in teardown, a leak checker): `rr wrap` adds a target-scope
# exit-status case, which must declare NO requirement (it is no test case).
if [ -n "$RR_TRACE_FILE" ]; then
  echo '{"test": "leaky::passes", "requirement": "REQ-1"}' >>"$RR_TRACE_FILE"
fi
echo "running 1 test"
echo "test leaky::passes ... ok"
echo
echo "test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out"
exit 1
