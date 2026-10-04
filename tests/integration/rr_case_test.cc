// SPDX-License-Identifier: AGPL-3.0-or-later
//
// rr_case.h end to end: each case carries at most one requirement, and the
// golden report shows a failing case failing its own requirement only. The
// suite is not named after the target, so the golden case keys prove they
// come from the JUnit classname.
#include "rr_case.h"

RR_CASE(tagged_case_is_traced, "REQ-1") { RR_CHECK(3 * 3 == 9); }

RR_CASE(failing_case_fails_its_own_requirement, "REQ-2") {
  // Deliberately failing: the golden report shows REQ-2 as FAILED.
  RR_CHECK(2 + 2 == 5);
}

RR_CASE(cases_after_a_failure_still_run, "REQ-3") { RR_CHECK(1 + 1 == 2); }

RR_CASE(untagged_case_is_evidence_for_nothing) { RR_CHECK(true); }

int main(int argc, char** argv) { return rr::RunCases(argc, argv, "plain_assert_codec"); }
