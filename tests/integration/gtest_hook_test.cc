// SPDX-License-Identifier: AGPL-3.0-or-later
#include "rr_gtest.h"

TEST(GtestHook, VerifiesIsTraced) {
  RR_VERIFIES("REQ-3");
  RR_LEVEL("sil");
  EXPECT_EQ(3 * 3, 9);
}

TEST(GtestHook, IdsAccumulateAcrossCalls) {
  RR_VERIFIES("REQ-3");
  RR_VERIFIES("REQ-1");
  EXPECT_TRUE(true);
}

TEST(GtestHook, UntracedTestHasNoIds) { EXPECT_TRUE(true); }
