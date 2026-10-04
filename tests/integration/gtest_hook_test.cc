// SPDX-License-Identifier: AGPL-3.0-or-later
#include "rr_gtest.h"

TEST(GtestHook, VerifiesIsTraced) {
  RR_VERIFIES("REQ-3");
  RR_LEVEL("sil");
  EXPECT_EQ(3 * 3, 9);
}

// Quarantine case: two ids in one test are deprecated but still all recorded
// (declared_ids_check asserts both reach the evidence), so attribution
// quarantines the case and it verifies neither requirement.
TEST(GtestHook, IdsAccumulateAcrossCalls) {
  RR_VERIFIES("REQ-3");
  RR_VERIFIES("REQ-1");
  EXPECT_TRUE(true);
}

TEST(GtestHook, UntracedTestHasNoIds) { EXPECT_TRUE(true); }

// Suite-level recording is written on the suite and must not leak into the
// next suite's tests (from 0.3 no test case inherits a suite-level id).
class SuiteLevel : public ::testing::Test {
 protected:
  static void SetUpTestSuite() { RR_VERIFIES("REQ-3"); }
};

TEST_F(SuiteLevel, InheritsSuiteIds) { EXPECT_TRUE(true); }

class NextSuite : public ::testing::Test {
 protected:
  static void SetUpTestSuite() { RR_VERIFIES("REQ-1"); }
};

TEST_F(NextSuite, OnlyItsOwnSuiteIds) { EXPECT_TRUE(true); }
