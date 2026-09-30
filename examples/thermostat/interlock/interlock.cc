// SPDX-License-Identifier: AGPL-3.0-or-later
#include "interlock/interlock.h"

namespace thermostat {

bool Interlock::HeaterAllowed(double reading_c) {
  // A NaN compares false everywhere, so treat "not below the reset point" as hot.
  if (!(reading_c < kTripC)) {
    tripped_ = true;
  } else if (tripped_ && reading_c < kResetC) {
    tripped_ = false;
  }
  return !tripped_;
}

}  // namespace thermostat
