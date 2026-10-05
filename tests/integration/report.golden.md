# rules_requirements hook integration

**0/1** user needs validated · **1/6** requirements verified (1 under-verified, 1 failed, 3 invalid, 0 incomplete, 0 unverified) · **0/1** risks mitigated · 13 test cases (6 owned, 5 unowned, 2 quarantined) · 11 gaps

_attribution: hybrid · lock: none (sets not pinned)_

> ⛔ **ATTRIBUTION ERROR: 2 quarantined test case(s) count for no requirement** (a test case verifies at most one requirement); every entity they name is INVALID:
>
> - `//tests/integration:gtest_hook_test#GtestHook::IdsAccumulateAcrossCalls` — **multi-tag**: declared REQ-3, REQ-1
> - `//tests/integration:rust_hook_test#rust_hook_test::multiple_ids` — **multi-tag**: declared REQ-4, REQ-1

> **High-severity risks not mitigated:** RISK-1

## User needs — validation

| ID | Need | Requirements | Status |
|---|---|---|---|
| UN-1 | Trace tests in every supported framework | REQ-1, REQ-2, REQ-3, REQ-4, REQ-5, REQ-6 | ❌ FAILED |

## Requirements — verification

| ID | Requirement | Traces | Demands | Evidence | Status |
|---|---|---|---|---|---|
| REQ-1 | pytest markers are traced | satisfies UN-1; implements MIT-1 | simulation | set 1/3 passed · 2 quarantined<br>✓ tests.integration.test_pytest_hook::test_marker_is_traced [simulation]<br>⛔ GtestHook::IdsAccumulateAcrossCalls (multi-tag)<br>⛔ rust_hook_test::multiple_ids (multi-tag) | ❌ INVALID |
| REQ-2 | unittest decorators are traced | satisfies UN-1; implements MIT-1 | simulation | set 1/1 passed<br>✓ unittest_hook_test.UnittestHook::test_decorator_is_traced [simulation] | ✅ VERIFIED |
| REQ-3 | googletest RR_VERIFIES is traced | satisfies UN-1; implements MIT-1 | sil | set 1/2 passed · 1 quarantined<br>✓ GtestHook::VerifiesIsTraced [sil]<br>⛔ GtestHook::IdsAccumulateAcrossCalls (multi-tag) | ❌ INVALID |
| REQ-4 | Rust rr::verifies! is traced | satisfies UN-1; implements MIT-1 | sil | set 1/2 passed · 1 quarantined<br>✓ rust_hook_test::verifies_is_traced [sil]<br>⛔ rust_hook_test::multiple_ids (multi-tag) | ❌ INVALID |
| REQ-5 | A failing test marks its requirement FAILED | satisfies UN-1 | simulation | set 0/1 passed · 1 failed<br>✗ unittest_hook_test.UnittestHook::test_failure_is_recorded [simulation] | ❌ FAILED |
| REQ-6 | Hardware-only requirement stays under-verified without a bench | satisfies UN-1 | hil | set 1/1 passed<br>✓ tests.integration.test_pytest_hook::test_hardware_requirement_in_simulation [simulation] | 🟠 UNDER-VERIFIED |

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

## Verification sets

Each entity's set: the cases it owns, the cases it expects (literal selectors, the lock) and every quarantined case that names it. It is verified only when the whole set passed together.

### REQ-1 — ❌ INVALID

set 1/3 passed · 2 quarantined

| Case | State | Level | Via | Selector | Note |
|---|---|---|---|---|---|
| //tests/integration:gtest_hook_test#GtestHook::IdsAccumulateAcrossCalls | quarantined | simulation | tag | tag | multi-tag |
| //tests/integration:pytest_hook_test#tests.integration.test_pytest_hook::test_marker_is_traced | passed | simulation | tag | tag |  |
| //tests/integration:rust_hook_test#rust_hook_test::multiple_ids | quarantined | simulation | tag | tag | multi-tag |

### REQ-2 — ✅ VERIFIED

set 1/1 passed

| Case | State | Level | Via | Selector | Note |
|---|---|---|---|---|---|
| //tests/integration:unittest_hook_test#unittest_hook_test.UnittestHook::test_decorator_is_traced | passed | simulation | tag | tag |  |

### REQ-3 — ❌ INVALID

set 1/2 passed · 1 quarantined

| Case | State | Level | Via | Selector | Note |
|---|---|---|---|---|---|
| //tests/integration:gtest_hook_test#GtestHook::IdsAccumulateAcrossCalls | quarantined | simulation | tag | tag | multi-tag |
| //tests/integration:gtest_hook_test#GtestHook::VerifiesIsTraced | passed | sil | tag | tag |  |

### REQ-4 — ❌ INVALID

set 1/2 passed · 1 quarantined

| Case | State | Level | Via | Selector | Note |
|---|---|---|---|---|---|
| //tests/integration:rust_hook_test#rust_hook_test::multiple_ids | quarantined | simulation | tag | tag | multi-tag |
| //tests/integration:rust_hook_test#rust_hook_test::verifies_is_traced | passed | sil | tag | tag |  |

### REQ-5 — ❌ FAILED

set 0/1 passed · 1 failed

| Case | State | Level | Via | Selector | Note |
|---|---|---|---|---|---|
| //tests/integration:unittest_hook_test#unittest_hook_test.UnittestHook::test_failure_is_recorded | failed | simulation | tag | tag |  |

### REQ-6 — 🟠 UNDER-VERIFIED

set 1/1 passed

| Case | State | Level | Via | Selector | Note |
|---|---|---|---|---|---|
| //tests/integration:pytest_hook_test#tests.integration.test_pytest_hook::test_hardware_requirement_in_simulation | passed | simulation | tag | tag |  |

## Case attribution

| Target | Cases | Owned | Quarantined | Unowned | Owners |
|---|---|---|---|---|---|
| //tests/integration:gtest_hook_test | 5 | 1 | 1 | 3 | REQ-3 |
| //tests/integration:pytest_hook_test | 2 | 2 | 0 | 0 | REQ-1, REQ-6 |
| //tests/integration:rust_hook_test | 4 | 1 | 1 | 2 | REQ-4 |
| //tests/integration:unittest_hook_test | 2 | 2 | 0 | 0 | REQ-2, REQ-5 |

Unowned cases (the granularity backlog: claim each for one entity, or leave it unowned):

- `//tests/integration:gtest_hook_test`: `GtestHook::UntracedTestHasNoIds` (passed), `NextSuite::OnlyItsOwnSuiteIds` (passed), `SuiteLevel::DoesNotInheritSuiteIds` (passed)
- `//tests/integration:rust_hook_test`: `rust_hook_test::ignored_test` (skipped), `rust_hook_test::trace_line_is_json` (passed)

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
