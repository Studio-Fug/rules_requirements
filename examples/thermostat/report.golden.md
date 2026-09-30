# Thermostat

**2/2** user needs validated · **7/7** requirements verified (0 under-verified, 0 failed, 0 unverified) · **2/2** risks mitigated · 17 test cases · 0 gaps

## User needs — validation

| ID | Need | Requirements | Status |
|---|---|---|---|
| UN-1 | Keep the room at a comfortable temperature | REQ-1, REQ-2 | ✅ VALIDATED |
| UN-2 | Set the target temperature in Celsius or Fahrenheit | REQ-3, REQ-4, REQ-7 | ✅ VALIDATED |

## Requirements — verification

| ID | Requirement | Traces | Demands | Evidence | Status |
|---|---|---|---|---|---|
| REQ-1 | Heat when the room is below the setpoint band | satisfies UN-1 | simulation | ✓ tests.test_controller::test_heats_below_band [simulation] | ✅ VERIFIED |
| REQ-2 | Stop heating above the band; hold state inside it | satisfies UN-1 | simulation | ✓ tests.test_controller::test_stops_above_band_and_holds_inside_it [simulation] | ✅ VERIFIED |
| REQ-3 | Parse setpoints in °C and °F | satisfies UN-2; implements MIT-2 | simulation | ✓ tests::parses_celsius_and_fahrenheit [simulation]<br>✓ tests::requires_a_unit [simulation] | ✅ VERIFIED |
| REQ-4 | Reject setpoints outside 5-30 °C | satisfies UN-2; implements MIT-2 | simulation | ✓ tests.test_controller::test_accepts_range_limits [simulation]<br>✓ tests.test_controller::test_rejects_setpoints_outside_range[30.1] [simulation]<br>✓ tests.test_controller::test_rejects_setpoints_outside_range[4.9] [simulation]<br>✓ tests.test_controller::test_rejects_setpoints_outside_range[80.0] [simulation]<br>✓ tests::rejects_out_of_range [simulation]<br>✓ tests::requires_a_unit [simulation] | ✅ VERIFIED |
| REQ-5 | Independent over-temperature cutoff | implements MIT-1 | sil | ✓ Interlock::NanReadingTrips [sil]<br>✓ Interlock::StaysTrippedUntilBelowReset [sil]<br>✓ Interlock::TripsAtLimit [sil] | ✅ VERIFIED |
| REQ-6 | Fail safe on an invalid sensor reading | implements MIT-1 | simulation | ✓ tests.test_controller::test_invalid_reading_turns_heater_off[-41.0] [simulation]<br>✓ tests.test_controller::test_invalid_reading_turns_heater_off[85.1] [simulation]<br>✓ tests.test_controller::test_invalid_reading_turns_heater_off[nan] [simulation] | ✅ VERIFIED |
| REQ-7 | Show the setpoint with its unit | satisfies UN-2 | inspection | ✓ inspection.TM-2::panel-shows-setpoint-unit [inspection]<br>✓ display_test.DisplayTest::test_setpoint_shows_unit [simulation] | ✅ VERIFIED |

## Risks — control

| ID | Risk | Severity × likelihood | Mitigations | Status |
|---|---|---|---|---|
| RISK-1 | Room overheats | high × possible → high × rare | MIT-1 | ✅ MITIGATED |
| RISK-2 | Implausible setpoint accepted | medium × likely → medium × unlikely | MIT-2 | ✅ MITIGATED |

## Mitigations — risk control measures

| ID | Mitigation | Type | Mitigates | Implemented by | Status |
|---|---|---|---|---|---|
| MIT-1 | Independent over-temperature protection | protective | RISK-1 | REQ-5, REQ-6 | ✅ VERIFIED |
| MIT-2 | Validate setpoints at entry | inherent | RISK-2 | REQ-3, REQ-4 | ✅ VERIFIED |

## Test methods

| ID | Method | Level | Used by |
|---|---|---|---|
| TM-1 | Interlock software-in-the-loop test | sil | REQ-5 |
| TM-2 | Visual inspection of the panel | inspection | REQ-7 |

## Implementation — source annotations

| ID | Implemented in | Verified in |
|---|---|---|
| REQ-1 | thermostat/controller.py:27 (class Controller) | tests/test_controller.py:9 (def test_heats_below_band) |
| REQ-2 | thermostat/controller.py:27 (class Controller) | tests/test_controller.py:15 (def test_stops_above_band_and_holds_inside_it) |
| REQ-3 | setpoint/src/lib.rs:24 (fn parse) | setpoint/src/lib.rs:46 (fn parses_celsius_and_fahrenheit)<br>setpoint/src/lib.rs:54 (fn requires_a_unit) |
| REQ-4 | setpoint/src/lib.rs:24 (fn parse)<br>thermostat/controller.py:20 (def check_setpoint) | setpoint/src/lib.rs:54 (fn requires_a_unit)<br>setpoint/src/lib.rs:62 (fn rejects_out_of_range)<br>tests/test_controller.py:24 (def test_rejects_setpoints_outside_range)<br>tests/test_controller.py:31 (def test_accepts_range_limits) |
| REQ-5 | interlock/interlock.h:7 (class Interlock) | interlock/interlock_test.cc:13 (Interlock.TripsAtLimit)<br>interlock/interlock_test.cc:22 (Interlock.StaysTrippedUntilBelowReset)<br>interlock/interlock_test.cc:33 (Interlock.NanReadingTrips) |
| REQ-6 | thermostat/controller.py:14 (def plausible) | tests/test_controller.py:37 (def test_invalid_reading_turns_heater_off) |
| REQ-7 | thermostat/display.py:5 (def format_setpoint) | tests/display_test.py:11 (def test_setpoint_shows_unit) |
| MIT-1 | — | — |
| MIT-2 | — | — |

## Modules

| Module | Status |
|---|---|
| interlock | ✅ VERIFIED |
| setpoint | ✅ VERIFIED |
| thermostat | ✅ VERIFIED |
