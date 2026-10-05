# Example: a requirements-first thermostat

A deliberately small product, developed against user needs, requirements and a
risk analysis, with every requirement traced to tests — one component per
supported test framework:

| Component | Language | Hook | Requirements |
| --------- | -------- | ---- | ------------ |
| `thermostat/` controller + panel text | Python | pytest `@pytest.mark.rr`, unittest `@rr.verifies` | REQ-1, 2, 4, 6, 7 |
| `interlock/` over-temperature cutoff | C++ | googletest `RR_VERIFIES` | REQ-5 |
| `setpoint/` setpoint parser | Rust | `rr::verifies!` | REQ-3, 4 |
| panel inspection | — | signed record `evidence/panel_inspection.rr.yaml` | REQ-7 |

```text
UN-1 comfortable room ◀── REQ-1 heat below band, REQ-2 hysteresis
UN-2 °C / °F setpoint  ◀── REQ-3 parse units, REQ-4 range 5–30 °C, REQ-7 show unit
RISK-1 room overheats  ◀── MIT-1 independent protection ──▶ REQ-5 interlock, REQ-6 sensor plausibility
RISK-2 bad setpoint    ◀── MIT-2 validate at entry      ──▶ REQ-3, REQ-4
```

The model lives in [`requirements/`](requirements) (one file per entity kind);
source files carry `@rr(...)` annotations tying code to requirements.

**A test case verifies at most one requirement.** Each requirement claims its
test cases in the model (`verified_by`, with `config.attribution: model`), no
two requirements may claim one case, and every test names its one requirement
as a cross-check. [`requirements/verification.rrlock`](requirements/verification.rrlock)
pins each requirement's set of cases; review its diff like a golden.

```sh
bazel test //...        # unit tests + model and lock validation + annotation check + golden report
bazel build //:report   # bazel-bin/report.html / .json / .md
bazel run //:sets_lock_test.update   # re-lock the verification sets after an intended change
bazel run //:thermostat_cli -- --setpoint 21 19.0 20.8 21.7
bazel run //:editor     # the web editor on this model (http://localhost:8080/)
```

`//:report` runs every test inside a build action (`rr_evidence`), adds the
inspection record, scans the sources for annotations and renders the report;
a quarantined test case (one naming two requirements, or claimed by two)
fails it, and `//:report_check_test` re-proves from `report.json` alone that
no test case is owned twice. `//:sets_lock_test` fails when the evidence and
the lock disagree.
[`report.golden.md`](report.golden.md) is the checked-in result; change a test
or a requirement and `//:report_md_golden_test` shows you exactly how the
traceability changed. Accept an intended change with
`bazel run //:report_md_golden_test.update` (and the `json` twin).

Things to try:

- Change `REQ-5`'s method to `TM-3` with `level: hil` — it turns
  **UNDER-VERIFIED** (sil evidence, hil demanded), MIT-1 becomes PARTIAL and
  RISK-1, a high-severity risk, is hoisted to the top of the report.
- Break the hysteresis in `thermostat/controller.py` — REQ-2 goes **FAILED**,
  and so do UN-1's validation and the golden test.
- Delete the inspection record from the report's evidence — REQ-7's set is
  no longer whole, so it reads **INCOMPLETE**, routed to a human.
- Put `rr::verifies!("REQ-3", "REQ-4")` into `requires_a_unit` — the case is
  quarantined, REQ-3 and REQ-4 read **INVALID**, and `//:report` fails.
- Add `tests::requires_*` to REQ-4's claims — `//:model_test` fails with
  `shared-case`, naming `tests::requires_a_unit` as the case both would own.
