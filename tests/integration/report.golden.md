# rules_requirements hook integration

**0/1** user needs validated · **4/6** requirements verified (1 under-verified, 1 failed, 0 unverified) · **1/1** risks mitigated · 13 test cases · 2 gaps

## User needs — validation

| ID | Need | Requirements | Status |
|---|---|---|---|
| UN-1 | Trace tests in every supported framework | REQ-1, REQ-2, REQ-3, REQ-4, REQ-5, REQ-6 | ❌ FAILED |

## Requirements — verification

| ID | Requirement | Traces | Demands | Evidence | Status |
|---|---|---|---|---|---|
| REQ-1 | pytest markers are traced | satisfies UN-1; implements MIT-1 | simulation | ✓ GtestHook::IdsAccumulateAcrossCalls [simulation]<br>✓ tests.integration.test_pytest_hook::test_marker_is_traced [simulation]<br>✓ rust_hook_test::multiple_ids [simulation] | ✅ VERIFIED |
| REQ-2 | unittest decorators are traced | satisfies UN-1; implements MIT-1 | simulation | ✓ unittest_hook_test.UnittestHook::test_decorator_is_traced [simulation] | ✅ VERIFIED |
| REQ-3 | googletest RR_VERIFIES is traced | satisfies UN-1; implements MIT-1 | sil | ✓ GtestHook::IdsAccumulateAcrossCalls [simulation]<br>✓ GtestHook::VerifiesIsTraced [sil] | ✅ VERIFIED |
| REQ-4 | Rust rr::verifies! is traced | satisfies UN-1; implements MIT-1 | sil | ✓ rust_hook_test::multiple_ids [simulation]<br>✓ rust_hook_test::verifies_is_traced [sil] | ✅ VERIFIED |
| REQ-5 | A failing test marks its requirement FAILED | satisfies UN-1 | simulation | ✗ unittest_hook_test.UnittestHook::test_failure_is_recorded [simulation] | ❌ FAILED |
| REQ-6 | Hardware-only requirement stays under-verified without a bench | satisfies UN-1 | hil | ✓ tests.integration.test_pytest_hook::test_hardware_requirement_in_simulation [simulation] | 🟠 UNDER-VERIFIED |

## Risks — control

| ID | Risk | Severity × likelihood | Mitigations | Status |
|---|---|---|---|---|
| RISK-1 | A hook silently drops traces, so the report over-claims coverage | high × possible | MIT-1 | ✅ MITIGATED |

## Mitigations — risk control measures

| ID | Mitigation | Type | Mitigates | Implemented by | Status |
|---|---|---|---|---|---|
| MIT-1 | Golden report over all hooks | protective | RISK-1 | REQ-1, REQ-2, REQ-3, REQ-4 | ✅ VERIFIED |

## Test methods

| ID | Method | Level | Used by |
|---|---|---|---|
| TM-1 | Bench run on real hardware | hil | REQ-6 |

## Gaps

| Kind | Entity | Route | Detail |
|---|---|---|---|
| failed | REQ-5 | autonomous | failing evidence: unittest_hook_test.UnittestHook::test_failure_is_recorded |
| under-verified | REQ-6 | human-gate | demands hil, best passing evidence is simulation |
