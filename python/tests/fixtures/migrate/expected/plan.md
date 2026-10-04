# Attribution worksheet

**16** of 18 attributed evidence units count toward two or more entities · **1** target(s) shared between requirements · 0 decided, 16 open

A test case verifies at most one requirement. Decide each case's owner in the `.rrplan` file.

## Targets named in verified_by

| Target | Claimed by | Cases | Per-case output | Uncontested cases |
|---|---|---|---|---|
| `//app/tests:smoke_test` | REQ-6 | 2 | yes | REQ-6: 1 |
| `//web:clock_test` | REQ-4, REQ-5 (shared) | 1 | no — whole target only | — |

## Contested evidence

### `//app/tests:smoke_test` — `app.tests.test_smoke`

Counts toward **REQ-3, REQ-6** (tagged REQ-3; verified_by of REQ-6) · owner: **?** · proposed: REQ-3 (the case's own tag is more specific than the whole-target verified_by of REQ-6)

| Case | Status | Counts toward | Proposed | Owner |
|---|---|---|---|---|
| `app.tests.test_smoke::test_boots` | passed | (group) | — | (group) |

### `//app/tests:unit_test` — `app.tests.test_config`

Counts toward **REQ-1, REQ-2** (tagged REQ-1, REQ-2) · owner: **?** · proposed: REQ-2 (REQ-2 refines REQ-1, whose verdict rolls up from it)

| Case | Status | Counts toward | Proposed | Owner |
|---|---|---|---|---|
| `app.tests.test_config::test_parses_minimal_file` | passed | (group) | — | (group) |
| `app.tests.test_config::test_rejects_garbage[]` | passed | (group) | — | (group) |
| `app.tests.test_config::test_rejects_garbage[]]` | passed | (group) | — | (group) |
| `app.tests.test_config::test_rejects_garbage[{]` | passed | (group) | — | (group) |
| `app.tests.test_config::test_rejects_missing_key` | passed | (group) | — | (group) |

### `//app/tests:unit_test` — `app.tests.test_config.TestRoundTrip`

Counts toward **REQ-1, REQ-2** (tagged REQ-1, REQ-2) · owner: **?** · proposed: REQ-2 (REQ-2 refines REQ-1, whose verdict rolls up from it)

| Case | Status | Counts toward | Proposed | Owner |
|---|---|---|---|---|
| `app.tests.test_config.TestRoundTrip::test_dump_then_load` | passed | (group) | — | (group) |

### `//app/tests:unit_test` — `app.tests.test_legacy.LegacyTest`

Counts toward **REQ-1, REQ-2, REQ-3** (tagged REQ-1, REQ-2, REQ-3) · owner: **?**

| Case | Status | Counts toward | Proposed | Owner |
|---|---|---|---|---|
| `app.tests.test_legacy.LegacyTest::test_reads_old_format` | passed | REQ-1, REQ-3 | — | (group) |
| `app.tests.test_legacy.LegacyTest::test_rejects_old_garbage` | passed | (group) | — | (group) |

### `//app/tests:unit_test` — `app.tests.test_link`

Counts toward **REQ-1, REQ-3** (tagged REQ-1, REQ-3) · owner: **?**

| Case | Status | Counts toward | Proposed | Owner |
|---|---|---|---|---|
| `app.tests.test_link::test_backs_off_exponentially` | passed | (group) | — | (group) |
| `app.tests.test_link::test_reconnects_after_drop` | passed | (group) | — | (group) |

### `//app/tests:unit_test` — `app.tests.test_modes`

Counts toward **REQ-1, REQ-3** (tagged REQ-1, REQ-3) · owner: **?**

| Case | Status | Counts toward | Proposed | Owner |
|---|---|---|---|---|
| `app.tests.test_modes::test_modes[fast]` | passed | (group) | — | (group) |
| `app.tests.test_modes::test_modes[slow]` | passed | (group) | — | (group) |

### `//app/tests:unit_test` — `app.tests.test_obsolete`

Counts toward **REQ-4, REQ-5** (tagged REQ-4, REQ-5) · owner: **?**

| Case | Status | Counts toward | Proposed | Owner |
|---|---|---|---|---|
| `app.tests.test_obsolete::test_helper_shape` | passed | (group) | — | (group) |

### `//cc:codec_test` — `Codec`

Counts toward **REQ-1, REQ-2** (tagged REQ-1, REQ-2) · owner: **?** · proposed: REQ-2 (REQ-2 refines REQ-1, whose verdict rolls up from it)

| Case | Status | Counts toward | Proposed | Owner |
|---|---|---|---|---|
| `Codec::RoundTrip` | passed | (group) | — | (group) |

### `//web:clock_test`

Counts toward **REQ-4, REQ-5** (verified_by of REQ-4, REQ-5) · owner: **?**

| Case | Status | Counts toward | Proposed | Owner |
|---|---|---|---|---|
| `[target]` | passed | (group) | — | (group) |
