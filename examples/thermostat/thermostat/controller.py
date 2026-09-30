# SPDX-License-Identifier: AGPL-3.0-or-later
"""Heater control with hysteresis and a sensor plausibility guard."""

from __future__ import annotations

import math

BAND_C = 0.5
MIN_SETPOINT_C = 5.0
MAX_SETPOINT_C = 30.0
SENSOR_RANGE_C = (-40.0, 85.0)


# @rr(REQ-6): A reading the sensor cannot produce means the sensor is broken.
def plausible(reading_c: float) -> bool:
    lo, hi = SENSOR_RANGE_C
    return not math.isnan(reading_c) and lo <= reading_c <= hi


# @rr(REQ-4): Setpoints outside the comfort range are refused, whatever their source.
def check_setpoint(setpoint_c: float) -> float:
    if not MIN_SETPOINT_C <= setpoint_c <= MAX_SETPOINT_C:
        raise ValueError(f"setpoint {setpoint_c:.1f} °C is outside {MIN_SETPOINT_C:g}-{MAX_SETPOINT_C:g} °C")
    return setpoint_c


# @rr(REQ-1, REQ-2): Bang-bang control with a ±0.5 °C hysteresis band.
class Controller:
    def __init__(self, setpoint_c: float) -> None:
        self.setpoint_c = check_setpoint(setpoint_c)
        self.heater_on = False

    def update(self, reading_c: float) -> bool:
        """Feed one temperature reading; returns whether the heater should run."""
        if not plausible(reading_c):
            self.heater_on = False
        elif reading_c < self.setpoint_c - BAND_C:
            self.heater_on = True
        elif reading_c > self.setpoint_c + BAND_C:
            self.heater_on = False
        return self.heater_on
