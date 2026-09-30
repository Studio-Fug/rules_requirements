// SPDX-License-Identifier: AGPL-3.0-or-later
#ifndef THERMOSTAT_INTERLOCK_H_
#define THERMOSTAT_INTERLOCK_H_

namespace thermostat {

// @rr(REQ-5): Over-temperature interlock, independent of the controller.
// Trips at kTripC and stays tripped until the temperature drops below kResetC.
class Interlock {
 public:
  static constexpr double kTripC = 35.0;
  static constexpr double kResetC = 30.0;

  // Feeds a reading; returns whether the heater may be energised.
  bool HeaterAllowed(double reading_c);
  bool tripped() const { return tripped_; }

 private:
  bool tripped_ = false;
};

}  // namespace thermostat

#endif  // THERMOSTAT_INTERLOCK_H_
