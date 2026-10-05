# Tutorial: a requirements-first thermostat

The repository's [`examples/thermostat`](https://github.com/Studio-Fug/rules_requirements/tree/main/examples/thermostat)
is a deliberately small product developed the way this tool intends: user
needs first, then requirements and a risk analysis, then code and tests that
trace back to them — ending in a report that a golden test pins in review.
Every requirement names the test cases that verify it, and each test case
verifies exactly one requirement ({doc}`one-test-case-one-requirement`). It
is split into three components, one per supported test framework:

| Component | Language | Hook | Requirements |
| --------- | -------- | ---- | ------------ |
| `thermostat/` — controller and panel text | Python | pytest, unittest | REQ-1, 2, 4, 6, 7 |
| `interlock/` — over-temperature cutoff | C++ | googletest | REQ-5 |
| `setpoint/` — setpoint parser | Rust | `rr::verifies!` | REQ-3, 4 |
| panel inspection | — | signed record | REQ-7 |

To follow along, clone the repository and work in `examples/thermostat`, which
is a Bazel module of its own that uses the ruleset from the checkout
(`local_path_override(path = "../..")`).

## 1. User needs

Start from what the occupants of the room need, in their terms:

```{literalinclude} ../examples/thermostat/requirements/user_needs.yaml
:language: yaml
```

## 2. Risks and their controls

A risk analysis asks what could go wrong and how badly (ISO 14971; see
{doc}`standards`). Each risk names its hazard, the situation in which people are
exposed to it, the harm, and an estimate of severity and likelihood — before and
after control. Each **mitigation** is one risk control measure:

```{literalinclude} ../examples/thermostat/requirements/risks.yaml
:language: yaml
```

`project.yaml` sets the risk acceptability threshold used to check the residual
estimates (`high × rare` scores 4 × 1 = 4 and `medium × unlikely` 3 × 2 = 6, both
within 6). It also puts the model in charge of which requirement each test case
verifies (`attribution: model`) and names the lock that pins every
requirement's set of cases (`sets_lock`, step 6):

```{literalinclude} ../examples/thermostat/requirements/project.yaml
:language: yaml
```

## 3. Requirements and test methods

Requirements say what the product must do. Some satisfy user needs; others
exist because a mitigation needs them (REQ-5, REQ-6), and they trace upward
through the mitigation instead. Each one also **claims** the test cases that
verify it (`verified_by`): a target and the case paths it owns, literally or
with `*` as the only wildcard — `test_rejects_setpoints_outside_range[*]`
takes every parameter of that pytest test, `Interlock::*` every googletest
case of the interlock suite. No two requirements may claim one case
({ref}`claims`):

```{literalinclude} ../examples/thermostat/requirements/requirements.yaml
:language: yaml
```

Two requirements demand more than the default `simulation` rigor, through test
methods: the interlock must be verified software-in-the-loop, and the panel
text must be signed off by inspection:

```{literalinclude} ../examples/thermostat/requirements/test_methods.yaml
:language: yaml
```

`bazel test //:model_test` validates all of this — the claims included: had
REQ-4 also claimed `tests::requires_*`, it would fail with

```text
requirements/requirements.yaml:55: error: [shared-case] REQ-4 and REQ-3 both claim cases of //:setpoint_test ('tests::requires_*' vs 'tests::requires_a_unit'), e.g. 'tests::requires_a_unit' (REQ-3 claims it at requirements/requirements.yaml:39). A test case verifies at most one requirement: narrow one selector.
```

The trace graph, coloured by the final verdicts:

```{raw} html
<div class="rr-graph-wrap">
```

```{raw} html
:file: _generated/thermostat-graph.svg
```

```{raw} html
</div>
```

## 4. Implementation, annotated

Code carries `@rr(...)` annotations naming what it implements
({doc}`guides/annotations`):

```{literalinclude} ../examples/thermostat/thermostat/controller.py
:language: python
:start-at: "# @rr(REQ-6)"
```

```{literalinclude} ../examples/thermostat/interlock/interlock.h
:language: cpp
:start-at: "// @rr(REQ-5)"
:end-before: "}  // namespace thermostat"
```

```{literalinclude} ../examples/thermostat/setpoint/src/lib.rs
:language: rust
:start-at: "// @rr(REQ-3, REQ-4)"
:end-before: "#[cfg(test)]"
```

## 5. Tests, one hook per language

The model's claims decide which requirement a test verifies, so a test needs
no tag. Each test here still names its one requirement through its framework's
hook, as a cross-check: a tag that disagrees with the claim owning its case is
a `tag-mismatch` gap in the report.

pytest tests use the `rr` marker:

```{literalinclude} ../examples/thermostat/tests/test_controller.py
:language: python
:start-at: "@pytest.mark.rr(\"REQ-1\")"
:end-before: "@pytest.mark.rr(\"REQ-4\")"
```

The unittest suite uses the decorator and `rr.unittest_main()`:

```{literalinclude} ../examples/thermostat/tests/display_test.py
:language: python
:start-at: "class DisplayTest"
```

googletest tests call `RR_VERIFIES` and declare the level they provide — `sil`,
which is what TM-1 demands of REQ-5:

```{literalinclude} ../examples/thermostat/interlock/interlock_test.cc
:language: cpp
:start-at: "TEST(Interlock, TripsAtLimit)"
:end-before: "TEST(Interlock, StaysTrippedUntilBelowReset)"
```

Rust tests call `rr::verifies!`. Until 0.3, `requires_a_unit` called
`rr::verifies!("REQ-3", "REQ-4")`; a test case verifies at most one
requirement, so that case was quarantined and both requirements read INVALID.
Its assertions are about syntax, so it now verifies REQ-3, and the range check
after converting °F, which is REQ-4's, is a test of its own:

```{literalinclude} ../examples/thermostat/setpoint/src/lib.rs
:language: rust
:lines: 52-58,70-77
:dedent: 4
```

Finally, the inspection TM-2 demands is recorded as evidence in its own right
({doc}`guides/evidence`):

```{literalinclude} ../examples/thermostat/evidence/panel_inspection.rr.yaml
:language: yaml
```

## 6. The build

The `BUILD.bazel` file wires it together — the model with its lock, one test
per hook, an annotation check, and the evidence → report → golden chain, with
the lock checked against the same evidence:

```{literalinclude} ../examples/thermostat/BUILD.bazel
:language: starlark
:start-at: "rr_py_test("
```

```console
$ bazel test //...
//:annotations_test                                                      PASSED
//:controller_test                                                       PASSED
//:display_test                                                          PASSED
//:interlock_test                                                        PASSED
//:model_test                                                            PASSED
//:report_check_test                                                     PASSED
//:report_json_golden_test                                               PASSED
//:report_md_golden_test                                                 PASSED
//:setpoint_test                                                         PASSED
//:sets_lock_test                                                        PASSED
$ bazel build //:report
evidence: 5 file(s), 18 test case(s) | validation: 2/2 needs | verification: 7/7 requirements (0 failed, 0 unverified, 0 under-verified, 0 invalid, 0 incomplete) | risks: 2/2 mitigated | gaps: 0
```

`//:report` runs the four test targets inside a build action (`rr_evidence`),
adds the inspection record, scans the sources for annotations and renders
`bazel-bin/report.html`, `report.json` and `report.md`. A quarantined test
case would fail this build. `//:report_check_test` re-proves from
`report.json` alone that no test case is owned by two requirements
(`rr check-report`).

**The lock.** `requirements/verification.rrlock` records, for every test
case, the one requirement whose set holds it:

```{literalinclude} ../examples/thermostat/requirements/verification.rrlock
:language: yaml
:lines: 1-8
```

`//:sets_lock_test` fails when the evidence and the lock disagree — a locked
test that no longer runs, a new test no one locked, a case that changed
owner — so a deleted or renamed test cannot silently shrink a requirement's
set. After an intended change, `bazel run //:sets_lock_test.update` rewrites
the lock; its diff is reviewed with the change, like a golden file.

## 7. The report

This is the example's golden Markdown report, exactly as checked in. Every
requirement is verified, by a set of test cases it alone owns: each member
lists the selector that claims it (`via model`), and the "Case attribution"
table shows every case of every target owned once, none unowned, none
quarantined:

```{include} ../examples/thermostat/report.golden.md
:heading-offset: 2
```

## 8. Things to try

Change something and watch the golden test tell you what it did to the
traceability argument:

- **Demand more rigor.** Point REQ-5's `method` at a new test method with
  `level: hil`. REQ-5 becomes **UNDER-VERIFIED** (its evidence is `sil`),
  MIT-1 and RISK-1 become **PARTIAL**, and RISK-1 — a high-severity risk — is
  hoisted into the "not mitigated" banner. The gap queue gains an
  `under-verified` item routed to `human-gate`, a `high-risk-open` item, and a
  `pyramid` item.
- **Break the code.** Remove the hysteresis in
  `thermostat/controller.py`: REQ-2 turns **FAILED**, and with it UN-1's
  validation and the golden test.
- **Lose the sign-off.** Delete `evidence/panel_inspection.rr.yaml` from the
  report's evidence: REQ-7's claimed inspection record did not run, so its set
  is not whole — REQ-7 reads **INCOMPLETE** and UN-2 PARTIAL — and the
  `incomplete` gap is routed to `human-gate`, because the missing member is an
  inspection (the claim's `level`).
- **Name two requirements in one test.** Put `rr::verifies!("REQ-3",
  "REQ-4")` back into `requires_a_unit`: the case is quarantined, REQ-3 and
  REQ-4 read **INVALID**, and `bazel build //:report` fails (`rr report` exits
  3) with an `ATTRIBUTION ERROR: multi-tag` line naming the case.
- **Claim one case twice.** Add `tests::requires_*` to REQ-4's claims on
  `//:setpoint_test`: `//:model_test` fails with the `shared-case` error shown
  in step 3, before any test runs.

Accept an intended change with `bazel run //:report_md_golden_test.update`
(and its `json` twin); the diff of the golden file is the change to the
traceability argument, reviewed alongside the code.
