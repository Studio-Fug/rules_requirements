# rules_requirements hook integration

**0/1** user needs validated · **1/6** requirements verified (1 under-verified, 1 failed, 3 invalid, 0 incomplete, 0 unverified) · **0/1** risks mitigated · 13 test cases · 11 gaps

> **High-severity risks not mitigated:** RISK-1

## User needs — validation

| ID | Need | Requirements | Status |
|---|---|---|---|
| UN-1 | Trace tests in every supported framework | REQ-1, REQ-2, REQ-3, REQ-4, REQ-5, REQ-6 | ❌ FAILED |

## Requirements — verification

| ID | Requirement | Traces | Demands | Evidence | Status |
|---|---|---|---|---|---|
| REQ-1 | pytest markers are traced | satisfies UN-1; implements MIT-1 | simulation | ✓ tests.integration.test_pytest_hook::test_marker_is_traced [simulation]<br>⛔ GtestHook::IdsAccumulateAcrossCalls (multi-tag)<br>⛔ rust_hook_test::multiple_ids (multi-tag) | ❌ INVALID |
| REQ-2 | unittest decorators are traced | satisfies UN-1; implements MIT-1 | simulation | ✓ unittest_hook_test.UnittestHook::test_decorator_is_traced [simulation] | ✅ VERIFIED |
| REQ-3 | googletest RR_VERIFIES is traced | satisfies UN-1; implements MIT-1 | sil | ✓ GtestHook::VerifiesIsTraced [sil]<br>⛔ GtestHook::IdsAccumulateAcrossCalls (multi-tag) | ❌ INVALID |
| REQ-4 | Rust rr::verifies! is traced | satisfies UN-1; implements MIT-1 | sil | ✓ rust_hook_test::verifies_is_traced [sil]<br>⛔ rust_hook_test::multiple_ids (multi-tag) | ❌ INVALID |
| REQ-5 | A failing test marks its requirement FAILED | satisfies UN-1 | simulation | ✗ unittest_hook_test.UnittestHook::test_failure_is_recorded [simulation] | ❌ FAILED |
| REQ-6 | Hardware-only requirement stays under-verified without a bench | satisfies UN-1 | hil | ✓ tests.integration.test_pytest_hook::test_hardware_requirement_in_simulation [simulation] | 🟠 UNDER-VERIFIED |

## Risks — control

| ID | Risk | Severity × likelihood | Mitigations | Status |
|---|---|---|---|---|
| RISK-1 | A hook silently drops traces, so the report over-claims coverage | high × possible | MIT-1 | ❌ FAILED |

## Mitigations — risk control measures

| ID | Mitigation | Type | Mitigates | Implemented by | Status |
|---|---|---|---|---|---|
| MIT-1 | Golden report over all hooks | protective | RISK-1 | REQ-1, REQ-2, REQ-3, REQ-4 | ❌ FAILED |

## Test methods

| ID | Method | Level | Used by |
|---|---|---|---|
| TM-1 | Bench run on real hardware | hil | REQ-6 |

## Gaps

| Kind | Entity | Route | Detail |
|---|---|---|---|
| multi-tag | //tests/integration:gtest_hook_test#GtestHook::IdsAccumulateAcrossCalls | autonomous | //tests/integration:gtest_hook_test#GtestHook::IdsAccumulateAcrossCalls declares REQ-3, REQ-1; a test case verifies at most one requirement, so it verifies none of them until its evidence names one |
| multi-tag | //tests/integration:rust_hook_test#rust_hook_test::multiple_ids | autonomous | //tests/integration:rust_hook_test#rust_hook_test::multiple_ids declares REQ-4, REQ-1; a test case verifies at most one requirement, so it verifies none of them until its evidence names one |
| invalid | REQ-1 | autonomous | quarantined case(s) name it: //tests/integration:gtest_hook_test#GtestHook::IdsAccumulateAcrossCalls (multi-tag), //tests/integration:rust_hook_test#rust_hook_test::multiple_ids (multi-tag); it verifies nothing through them until each has one owner |
| invalid | REQ-3 | autonomous | quarantined case(s) name it: //tests/integration:gtest_hook_test#GtestHook::IdsAccumulateAcrossCalls (multi-tag); it verifies nothing through them until each has one owner |
| invalid | REQ-4 | autonomous | quarantined case(s) name it: //tests/integration:rust_hook_test#rust_hook_test::multiple_ids (multi-tag); it verifies nothing through them until each has one owner |
| failed | REQ-5 | autonomous | failing evidence: unittest_hook_test.UnittestHook::test_failure_is_recorded |
| under-verified | REQ-6 | human-gate | demands hil, best passing evidence is simulation |
| suite-level-requirement | //tests/integration:gtest_hook_test | autonomous | suite SuiteLevel (or a parent case in it) names REQ-3 above its test cases; suite-level requirements are not inherited by the cases (each test case declares its own one id) |
| suite-level-requirement | //tests/integration:gtest_hook_test | autonomous | suite NextSuite (or a parent case in it) names REQ-1 above its test cases; suite-level requirements are not inherited by the cases (each test case declares its own one id) |
| unpinned-sets |  | autonomous | membership not pinned (no config.sets_lock): a deleted test would go unnoticed in the sets of REQ-1, REQ-2, REQ-3, REQ-4, REQ-5, REQ-6; lock them with `rr sets lock --write` |
| high-risk-open | RISK-1 | human-gate | high risk; mitigation is FAILED |
