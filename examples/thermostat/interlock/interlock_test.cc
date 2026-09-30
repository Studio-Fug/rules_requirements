// SPDX-License-Identifier: AGPL-3.0-or-later
#include "interlock/interlock.h"

#include <cmath>

#include "gtest/gtest.h"
#include "rr_gtest.h"

namespace thermostat {
namespace {

TEST(Interlock, TripsAtLimit) {
  RR_VERIFIES("REQ-5");
  RR_LEVEL("sil");
  Interlock lock;
  EXPECT_TRUE(lock.HeaterAllowed(34.9));
  EXPECT_FALSE(lock.HeaterAllowed(35.0));
  EXPECT_TRUE(lock.tripped());
}

TEST(Interlock, StaysTrippedUntilBelowReset) {
  RR_VERIFIES("REQ-5");
  RR_LEVEL("sil");
  Interlock lock;
  lock.HeaterAllowed(40.0);
  for (double t : {34.0, 32.0, 30.0}) {
    EXPECT_FALSE(lock.HeaterAllowed(t)) << t;
  }
  EXPECT_TRUE(lock.HeaterAllowed(29.9));
}

TEST(Interlock, NanReadingTrips) {
  RR_VERIFIES("REQ-5");
  RR_LEVEL("sil");
  Interlock lock;
  EXPECT_FALSE(lock.HeaterAllowed(std::nan("")));
}

}  // namespace
}  // namespace thermostat
