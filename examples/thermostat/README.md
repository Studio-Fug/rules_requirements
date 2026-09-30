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

```sh
bazel test //...        # unit tests + model validation + annotation check + golden report
bazel build //:report   # bazel-bin/report.html / .json / .md
bazel run //:thermostat_cli -- --setpoint 21 19.0 20.8 21.7
```

`//:report` runs every test inside a build action (`rr_evidence`), adds the
inspection record, scans the sources for annotations and renders the report.
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
- Delete the inspection record — REQ-7 is only **UNDER-VERIFIED** by the
  automated text check, because TM-2 demands inspection.
