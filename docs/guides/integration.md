# Integrating into an existing monorepo

Most projects adopt requirements traceability part-way through their life, with
tests, ids and conventions already in place. This page covers bringing
rules_requirements into such a repository without a flag day.

## Add the dependency

```starlark
# MODULE.bazel
bazel_dep(name = "rules_requirements", version = "0.1.0")
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
- Whole-target `verified_by` lists keep working: each Bazel test label maps to
  its `bazel-testlogs/.../test.xml`.
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
      --evidence "$(bazel info bazel-testlogs)/**/test.xml" \
      --current-build dut_git_sha="$GITHUB_SHA" \
      --queue-out "$GITHUB_WORKSPACE/traceability-queue.json" \
      --html "$GITHUB_WORKSPACE/traceability-report.html" \
      --md "$GITHUB_WORKSPACE/traceability-report.md" \
      --fail-on failed
- uses: actions/upload-artifact@v4
  with:
    name: traceability
    path: traceability-*
```

- Keep the test jobs as the pass/fail gate and let the report job describe the
  state; tighten `--fail-on` (`unverified`, `gaps`) once coverage is meant to be
  complete.
- The `**/test.xml` glob keeps the logs of retried flaky attempts out of the
  evidence ({doc}`evidence`).
- Hardware harnesses should stamp their evidence with the same identity keys
  you pass to `--current-build` — `JUnitWriter(..., artifact={"dut_git_sha":
  sha})` ({doc}`hooks`) — so a bench result from an older build is reported as
  stale rather than verified.
- The work queue is the natural input for follow-up automation: `autonomous`
  gaps are candidates for an agent; `human-gate` gaps need a bench session or a
  reviewer.

A repository can use both paths at once: `rr_evidence` + `rr_report` +
`rr_golden_test` for the hermetic suites, pinned in review, and the aggregated
report in CI for the complete picture including hardware.
