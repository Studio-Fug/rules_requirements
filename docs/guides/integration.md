# Integrating into an existing monorepo

Most projects adopt requirements traceability part-way through their life, with
tests, ids and conventions already in place. This page covers bringing
rules_requirements into such a repository without a flag day.

## Add the dependency

```starlark
# MODULE.bazel
bazel_dep(name = "rules_requirements", version = "0.3.0")
git_override(
    module_name = "rules_requirements",
    remote = "https://github.com/Studio-Fug/rules_requirements.git",
    commit = "<commit sha>",
)
```

For a vendored copy or a sibling checkout use
`local_path_override(module_name = "rules_requirements", path = "third_party/rules_requirements")`.

Nothing in the module registers toolchains or a pip hub for you: the Python
library needs only the standard library, pytest comes from your own pip hub
(`deps = [requirement("pytest")]` on `rr_py_test`), and the C++ and Rust hooks
use your registered toolchains.

## Keep your ids

If requirements are already numbered — `PR-12`, say — keep the numbers and
configure the prefix:

```yaml
# requirements/project.yaml
project:
  name: My product
config:
  prefixes: {requirement: PR}
```

Every other kind keeps its default prefix unless you change it too; prefixes
must be distinct from one another. A different numbering scheme (`SRS-3.2.1`,
say) needs a matching `id_pattern`, for example `'{prefix}-\d+(\.\d+)*'`.

## Model risk controls as mitigations

A common starting point is "derived requirements": requirements that exist
because a risk analysis asked for them, linked straight to the risk. In this
model a risk control is its own entity — a **mitigation** — which says *what
the control is* and is implemented by one or more requirements:

```yaml
# before: a derived requirement pointing at the risk
#   - id: PR-31
#     title: Close the socket when a TLS handshake stalls
#     kind: derived
#     mitigates: [RISK-4]

risks:
  - id: RISK-4
    title: Stalled handshakes exhaust connection slots
    severity: high
    likelihood: likely

mitigations:
  - id: MIT-4
    title: Bound every handshake in time
    type: protective
    mitigates: [RISK-4]
    implemented_by: [PR-31]

requirements:
  - id: PR-31
    title: Close the socket when a TLS handshake stalls
```

The requirement no longer needs to name the risk: it traces upward through the
mitigation, so it is not an orphan. A risk the analysis controlled with several
measures gets one mitigation per measure, or one mitigation implemented by
several requirements — whichever matches how you argue it in the risk
management file.

## Keep your annotations

Existing traceability markers usually carry over unchanged:

- The pytest marker is also registered as `@pytest.mark.requirements(...)`,
  with the same `level=` keyword.
- JUnit written by other tooling works as long as it uses the `requirement`
  property ({ref}`junit-properties`).
- 0.2's whole-target `verified_by` lists still parse — a bare label or
  `{target, level}` is a whole-target claim, with a `bare-target-reference`
  warning — as long as no two entities name the same target. That would let one
  test case verify two requirements, so since 0.3 it is a `shared-case` model
  error: narrow both claims to the cases each one owns
  ({ref}`claims`, {doc}`migrating-to-per-case`).
- A docstring convention such as `Requirements: PR-1, PR-2` is recognised by
  adding a pattern to `config.annotation_patterns`
  ({doc}`annotations`):

  ```yaml
  config:
    annotation_patterns:
      - 'Requirements:\s*([A-Z0-9,\s-]+)'
  ```

## Adopt the rules gradually

Coverage rules default to errors. On a large existing model, start them as
warnings and tighten over time:

```yaml
config:
  rules:
    need-unsatisfied: warning
    requirement-orphan: warning
    risk-unmitigated: warning
    mitigation-unimplemented: warning
```

The shape and reference checks stay errors: a dangling reference is always a
bug.

## One owner per test case

**A test case verifies at most one requirement**
({doc}`../one-test-case-one-requirement`). A project coming from 0.2, where a
test counted toward every id it was tagged with and every requirement listing
its target, moves there in steps that each keep CI green:
{doc}`migrating-to-per-case` walks through them. The end state, which
[`examples/thermostat`](https://github.com/Studio-Fug/rules_requirements/tree/main/examples/thermostat)
shows, is:

```yaml
# requirements/project.yaml
config:
  prefixes: {requirement: PR}
  attribution: model                 # the claims decide; tags only cross-check
  sets_lock: verification.rrlock     # every set's members, pinned
  main_repo: my_product              # '@my_product//x:y' reads as '//x:y'
```

with `verified_by` claims per case (`{target: //web:clocksync_test, cases:
["clocksync::*"]}`), single-id tags (or none), and a lock written by `rr sets
lock --write` and reviewed like a golden file. 0.3 defaults to
`attribution: hybrid`, so a single-id tag still owns a case no claim covers
while you get there.

In Bazel, for the hermetic suites:

```starlark
rr_model(
    name = "model",
    srcs = glob(["requirements/*.yaml"]),
    lock = "requirements/verification.rrlock",  # :model_test checks it against the claims
)

rr_report(
    name = "report",
    model = [":model"],
    evidence = [":evidence"],
    # check = True is the default with a JSON report: :report_check_test runs
    # `rr check-report` on report.json. A quarantined case fails the build.
)

rr_sets_lock_test(  # `bazel run :sets_lock_test.update` re-locks
    name = "sets_lock_test",
    model = ":model",
    evidence = [":evidence"],
)
```

## CI: aggregate real test logs

Suites that run only in CI or on hardware cannot run inside `rr_evidence`.
Run them normally, then build the report from the test logs, stamping the
identity of what was tested so that older evidence is marked stale:

```yaml
# .github/workflows/traceability.yaml (excerpt)
- name: Tests
  run: bazel test //... || true          # a failing test must still produce a report
- name: Traceability report
  run: |
    bazel run @rules_requirements//python:rr -- report \
      --model "$GITHUB_WORKSPACE/requirements" \
      --evidence "$(bazel info bazel-testlogs)" \
      --current-build dut_git_sha="$GITHUB_SHA" \
      --queue-out "$GITHUB_WORKSPACE/traceability-queue.json" \
      --html "$GITHUB_WORKSPACE/traceability-report.html" \
      --json "$GITHUB_WORKSPACE/traceability-report.json" \
      --md "$GITHUB_WORKSPACE/traceability-report.md" \
      --fail-on failed
- uses: actions/upload-artifact@v4
  with:
    name: traceability
    path: traceability-*
```

`rr report` exits 3 when a test case is quarantined — its evidence names two
ids, or two entities claim it — after writing the reports, so the artifact is
there to read; every entity it names reads INVALID. Add the one-owner checks
to the same job:

```yaml
- name: Attribution checks (one test case, one requirement)
  run: |
    bazel query 'tests(//...)' > "$RUNNER_TEMP/targets.txt"
    bazel run @rules_requirements//python:rr -- validate requirements \
      --known-targets "$RUNNER_TEMP/targets.txt"
    bazel run @rules_requirements//python:rr -- sets check \
      --model requirements --evidence "$(bazel info bazel-testlogs)"
    bazel run @rules_requirements//python:rr -- check-report \
      "$GITHUB_WORKSPACE/traceability-report.json"
```

`--known-targets` turns a claim on a label that does not exist (a typo would
otherwise read as not-run forever) into an `unknown-target` error; `rr sets
check` fails when a locked case is missing or an owned one is not locked;
`rr check-report` re-proves from the published JSON (written with `--json`)
that no case has two owners.

- Keep the test jobs as the pass/fail gate and let the report job describe the
  state; tighten `--fail-on` (`unverified`, `gaps`) once coverage is meant to be
  complete.
- Pass the whole `bazel-testlogs` directory rather than a `**/test.xml` glob
  if you retry tests: the earlier attempts under `test_attempts/` are merged
  with the final result, and a pass that needed a retry then reads
  UNDER-VERIFIED (`config.flaky`) instead of a plain pass ({doc}`evidence`).
- Hardware harnesses should stamp their evidence with the same identity keys
  you pass to `--current-build` — `JUnitWriter(..., artifact={"dut_git_sha":
  sha})` ({doc}`hooks`) — so a bench result from an older build is reported as
  stale rather than verified.
- The work queue is the natural input for follow-up automation: `autonomous`
  gaps are candidates for an agent; `human-gate` gaps need a bench session or a
  reviewer.
- **Lanes.** When hardware runs in another pipeline, stamp each report with its
  lane: `--lane software --lane-targets targets.txt` (the targets this lane
  runs). Members in other lanes' targets read "out of lane" and stay out of the
  queue, but verdicts never change: a requirement whose set spans both lanes
  reads INCOMPLETE in each lane's report, and only a report over both lanes'
  evidence can read VERIFIED. In Bazel: `rr_report(lane = ..., lane_targets =
  ...)`.

A repository can use both paths at once: `rr_evidence` + `rr_report` +
`rr_golden_test` for the hermetic suites, pinned in review, and the aggregated
report in CI for the complete picture including hardware.
