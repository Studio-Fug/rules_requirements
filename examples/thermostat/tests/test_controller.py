# SPDX-License-Identifier: AGPL-3.0-or-later
import math

import pytest

from thermostat.controller import Controller, check_setpoint, plausible


@pytest.mark.rr("REQ-1")
def test_heats_below_band():
    ctl = Controller(21.0)
    assert ctl.update(20.4) is True


@pytest.mark.rr("REQ-2")
def test_stops_above_band_and_holds_inside_it():
    ctl = Controller(21.0)
    ctl.update(19.0)
    assert ctl.update(21.4) is True  # inside the band: keep heating
    assert ctl.update(21.6) is False
    assert ctl.update(20.6) is False  # inside the band: stay off


@pytest.mark.rr("REQ-4")
@pytest.mark.parametrize("setpoint", [4.9, 30.1, 80.0])
def test_rejects_setpoints_outside_range(setpoint):
    with pytest.raises(ValueError):
        Controller(setpoint)


@pytest.mark.rr("REQ-4")
def test_accepts_range_limits():
    assert check_setpoint(5.0) == 5.0
    assert check_setpoint(30.0) == 30.0


@pytest.mark.rr("REQ-6")
@pytest.mark.parametrize("reading", [math.nan, -41.0, 85.1])
def test_invalid_reading_turns_heater_off(reading):
    ctl = Controller(21.0)
    ctl.update(15.0)
    assert not plausible(reading)
    assert ctl.update(reading) is False
