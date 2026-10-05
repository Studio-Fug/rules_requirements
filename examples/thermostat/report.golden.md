# Thermostat

**2/2** user needs validated · **7/7** requirements verified (0 under-verified, 0 failed, 0 invalid, 0 incomplete, 0 unverified) · **2/2** risks mitigated · 18 test cases (18 owned, 0 unowned, 0 quarantined) · 0 gaps

_attribution: model · lock: requirements/verification.rrlock_

## User needs — validation

| ID | Need | Requirements | Status |
|---|---|---|---|
| UN-1 | Keep the room at a comfortable temperature | REQ-1, REQ-2 | ✅ VALIDATED |
| UN-2 | Set the target temperature in Celsius or Fahrenheit | REQ-3, REQ-4, REQ-7 | ✅ VALIDATED |

## Requirements — verification

| ID | Requirement | Traces | Demands | Evidence | Status |
|---|---|---|---|---|---|
| REQ-1 | Heat when the room is below the setpoint band | satisfies UN-1 | simulation | set 1/1 passed<br>✓ tests.test_controller::test_heats_below_band [simulation] | ✅ VERIFIED |
| REQ-2 | Stop heating above the band; hold state inside it | satisfies UN-1 | simulation | set 1/1 passed<br>✓ tests.test_controller::test_stops_above_band_and_holds_inside_it [simulation] | ✅ VERIFIED |
| REQ-3 | Parse setpoints in °C and °F | satisfies UN-2; implements MIT-2 | simulation | set 2/2 passed<br>✓ tests::parses_celsius_and_fahrenheit [simulation]<br>✓ tests::requires_a_unit [simulation] | ✅ VERIFIED |
| REQ-4 | Reject setpoints outside 5-30 °C | satisfies UN-2; implements MIT-2 | simulation | set 6/6 passed<br>✓ tests.test_controller::test_accepts_range_limits [simulation]<br>✓ tests.test_controller::test_rejects_setpoints_outside_range[30.1] [simulation]<br>✓ tests.test_controller::test_rejects_setpoints_outside_range[4.9] [simulation]<br>✓ tests.test_controller::test_rejects_setpoints_outside_range[80.0] [simulation]<br>✓ tests::checks_the_range_after_converting [simulation]<br>✓ tests::rejects_out_of_range [simulation] | ✅ VERIFIED |
| REQ-5 | Independent over-temperature cutoff | implements MIT-1 | sil | set 3/3 passed<br>✓ Interlock::NanReadingTrips [sil]<br>✓ Interlock::StaysTrippedUntilBelowReset [sil]<br>✓ Interlock::TripsAtLimit [sil] | ✅ VERIFIED |
| REQ-6 | Fail safe on an invalid sensor reading | implements MIT-1 | simulation | set 3/3 passed<br>✓ tests.test_controller::test_invalid_reading_turns_heater_off[-41.0] [simulation]<br>✓ tests.test_controller::test_invalid_reading_turns_heater_off[85.1] [simulation]<br>✓ tests.test_controller::test_invalid_reading_turns_heater_off[nan] [simulation] | ✅ VERIFIED |
| REQ-7 | Show the setpoint with its unit | satisfies UN-2 | inspection | set 2/2 passed<br>✓ display_test.DisplayTest::test_setpoint_shows_unit [simulation]<br>✓ inspection.TM-2::panel-shows-setpoint-unit [inspection] | ✅ VERIFIED |

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
| REQ-4 | setpoint/src/lib.rs:24 (fn parse)<br>thermostat/controller.py:20 (def check_setpoint) | setpoint/src/lib.rs:62 (fn rejects_out_of_range)<br>setpoint/src/lib.rs:72 (fn checks_the_range_after_converting)<br>tests/test_controller.py:24 (def test_rejects_setpoints_outside_range)<br>tests/test_controller.py:31 (def test_accepts_range_limits) |
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

## Verification sets

Each entity's set: the cases it owns, the cases it expects (literal selectors, the lock) and every quarantined case that names it. It is verified only when the whole set passed together.

### REQ-1 — ✅ VERIFIED

set 1/1 passed

| Case | State | Level | Via | Selector | Note |
|---|---|---|---|---|---|
| //:controller_test#tests.test_controller::test_heats_below_band | passed | simulation | model | tests.test_controller::test_heats_below_band |  |

### REQ-2 — ✅ VERIFIED

set 1/1 passed

| Case | State | Level | Via | Selector | Note |
|---|---|---|---|---|---|
| //:controller_test#tests.test_controller::test_stops_above_band_and_holds_inside_it | passed | simulation | model | tests.test_controller::test_stops_above_band_and_holds_inside_it |  |

### REQ-3 — ✅ VERIFIED

set 2/2 passed

| Case | State | Level | Via | Selector | Note |
|---|---|---|---|---|---|
| //:setpoint_test#tests::parses_celsius_and_fahrenheit | passed | simulation | model | tests::parses_celsius_and_fahrenheit |  |
| //:setpoint_test#tests::requires_a_unit | passed | simulation | model | tests::requires_a_unit |  |

### REQ-4 — ✅ VERIFIED

set 6/6 passed

| Case | State | Level | Via | Selector | Note |
|---|---|---|---|---|---|
| //:controller_test#tests.test_controller::test_accepts_range_limits | passed | simulation | model | tests.test_controller::test_accepts_range_limits |  |
| //:controller_test#tests.test_controller::test_rejects_setpoints_outside_range[4.9] | passed | simulation | model | tests.test_controller::test_rejects_setpoints_outside_range[*] |  |
| //:controller_test#tests.test_controller::test_rejects_setpoints_outside_range[30.1] | passed | simulation | model | tests.test_controller::test_rejects_setpoints_outside_range[*] |  |
| //:controller_test#tests.test_controller::test_rejects_setpoints_outside_range[80.0] | passed | simulation | model | tests.test_controller::test_rejects_setpoints_outside_range[*] |  |
| //:setpoint_test#tests::checks_the_range_after_converting | passed | simulation | model | tests::checks_the_range_after_converting |  |
| //:setpoint_test#tests::rejects_out_of_range | passed | simulation | model | tests::rejects_out_of_range |  |

### REQ-5 — ✅ VERIFIED

set 3/3 passed

| Case | State | Level | Via | Selector | Note |
|---|---|---|---|---|---|
| //:interlock_test#Interlock::NanReadingTrips | passed | sil | model | Interlock::* |  |
| //:interlock_test#Interlock::StaysTrippedUntilBelowReset | passed | sil | model | Interlock::* |  |
| //:interlock_test#Interlock::TripsAtLimit | passed | sil | model | Interlock::* |  |

### REQ-6 — ✅ VERIFIED

set 3/3 passed

| Case | State | Level | Via | Selector | Note |
|---|---|---|---|---|---|
| //:controller_test#tests.test_controller::test_invalid_reading_turns_heater_off[85.1] | passed | simulation | model | tests.test_controller::test_invalid_reading_turns_heater_off[*] |  |
| //:controller_test#tests.test_controller::test_invalid_reading_turns_heater_off[-41.0] | passed | simulation | model | tests.test_controller::test_invalid_reading_turns_heater_off[*] |  |
| //:controller_test#tests.test_controller::test_invalid_reading_turns_heater_off[nan] | passed | simulation | model | tests.test_controller::test_invalid_reading_turns_heater_off[*] |  |

### REQ-7 — ✅ VERIFIED

set 2/2 passed

| Case | State | Level | Via | Selector | Note |
|---|---|---|---|---|---|
| //:display_test#display_test.DisplayTest::test_setpoint_shows_unit | passed | simulation | model | display_test.DisplayTest::test_setpoint_shows_unit |  |
| record:panel_inspection#inspection.TM-2::panel-shows-setpoint-unit | passed | inspection | model | inspection.TM-2::panel-shows-setpoint-unit |  |

## Case attribution

| Target | Cases | Owned | Quarantined | Unowned | Owners |
|---|---|---|---|---|---|
| //:controller_test | 9 | 9 | 0 | 0 | REQ-1, REQ-2, REQ-4, REQ-6 |
| //:display_test | 1 | 1 | 0 | 0 | REQ-7 |
| //:interlock_test | 3 | 3 | 0 | 0 | REQ-5 |
| //:setpoint_test | 4 | 4 | 0 | 0 | REQ-3, REQ-4 |
| record:panel_inspection | 1 | 1 | 0 | 0 | REQ-7 |
