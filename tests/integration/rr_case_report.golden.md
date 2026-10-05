# rules_requirements rr_case.h integration

**0/1** user needs validated · **2/3** requirements verified (0 under-verified, 1 failed, 0 invalid, 0 incomplete, 0 unverified) · **0/0** risks mitigated · 4 test cases (3 owned, 1 unowned, 0 quarantined) · 1 gaps

_attribution: hybrid · lock: tests/integration/rr_case.rrlock_

## User needs — validation

| ID | Need | Requirements | Status |
|---|---|---|---|
| UN-1 | Trace plain-assert C++ tests case by case | REQ-1, REQ-2, REQ-3 | ❌ FAILED |

## Requirements — verification

| ID | Requirement | Traces | Demands | Evidence | Status |
|---|---|---|---|---|---|
| REQ-1 | A passing case verifies the one requirement it names | satisfies UN-1 | simulation | set 1/1 passed<br>✓ plain_assert_codec::tagged_case_is_traced [simulation] | ✅ VERIFIED |
| REQ-2 | A failing case fails its own requirement | satisfies UN-1 | simulation | set 0/1 passed · 1 failed<br>✗ plain_assert_codec::failing_case_fails_its_own_requirement [simulation] | ❌ FAILED |
| REQ-3 | The cases after a failing one still run and count | satisfies UN-1 | simulation | set 1/1 passed<br>✓ plain_assert_codec::cases_after_a_failure_still_run [simulation] | ✅ VERIFIED |

## Verification sets

Each entity's set: the cases it owns, the cases it expects (literal selectors, the lock) and every quarantined case that names it. It is verified only when the whole set passed together.

### REQ-1 — ✅ VERIFIED

set 1/1 passed

| Case | State | Level | Via | Selector | Note |
|---|---|---|---|---|---|
| //tests/integration:rr_case_test#plain_assert_codec::tagged_case_is_traced | passed | simulation | tag | tag |  |

### REQ-2 — ❌ FAILED

set 0/1 passed · 1 failed

| Case | State | Level | Via | Selector | Note |
|---|---|---|---|---|---|
| //tests/integration:rr_case_test#plain_assert_codec::failing_case_fails_its_own_requirement | failed | simulation | tag | tag |  |

### REQ-3 — ✅ VERIFIED

set 1/1 passed

| Case | State | Level | Via | Selector | Note |
|---|---|---|---|---|---|
| //tests/integration:rr_case_test#plain_assert_codec::cases_after_a_failure_still_run | passed | simulation | tag | tag |  |

## Case attribution

| Target | Cases | Owned | Quarantined | Unowned | Owners |
|---|---|---|---|---|---|
| //tests/integration:rr_case_test | 4 | 3 | 0 | 1 | REQ-1, REQ-2, REQ-3 |

Unowned cases (the granularity backlog: claim each for one entity, or leave it unowned):

- `//tests/integration:rr_case_test`: `plain_assert_codec::untagged_case_is_evidence_for_nothing` (passed)

## Gaps

| Kind | Entity | Route | Detail |
|---|---|---|---|
| failed | REQ-2 | autonomous | failing evidence: plain_assert_codec::failing_case_fails_its_own_requirement |
