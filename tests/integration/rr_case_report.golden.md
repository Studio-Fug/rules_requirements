# rules_requirements rr_case.h integration

**0/1** user needs validated · **2/3** requirements verified (0 under-verified, 1 failed, 0 unverified) · **0/0** risks mitigated · 4 test cases · 1 gaps

## User needs — validation

| ID | Need | Requirements | Status |
|---|---|---|---|
| UN-1 | Trace plain-assert C++ tests case by case | REQ-1, REQ-2, REQ-3 | ❌ FAILED |

## Requirements — verification

| ID | Requirement | Traces | Demands | Evidence | Status |
|---|---|---|---|---|---|
| REQ-1 | A passing case verifies the one requirement it names | satisfies UN-1 | simulation | ✓ rr_case_test::tagged_case_is_traced [simulation] | ✅ VERIFIED |
| REQ-2 | A failing case fails its own requirement | satisfies UN-1 | simulation | ✗ rr_case_test::failing_case_fails_its_own_requirement [simulation] | ❌ FAILED |
| REQ-3 | The cases after a failing one still run and count | satisfies UN-1 | simulation | ✓ rr_case_test::cases_after_a_failure_still_run [simulation] | ✅ VERIFIED |

## Gaps

| Kind | Entity | Route | Detail |
|---|---|---|---|
| failed | REQ-2 | autonomous | failing evidence: rr_case_test::failing_case_fails_its_own_requirement |
