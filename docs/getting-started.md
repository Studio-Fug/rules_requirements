# Getting started

This page takes a small project from nothing to a traceability report, first
with Bazel and then with plain Python. The {doc}`tutorial` walks through a
complete example.

## Install

### With Bazel (bzlmod)

rules_requirements is consumed as a Bazel module. Until it is published to a
registry, pin a commit with `git_override`:

```starlark
# MODULE.bazel
bazel_dep(name = "rules_requirements", version = "0.2.1")
git_override(
    module_name = "rules_requirements",
    remote = "https://github.com/Studio-Fug/rules_requirements.git",
    commit = "<commit sha>",
)
```

Use `local_path_override(module_name = "rules_requirements", path = "...")`
instead to build against a local checkout.

The module declares `rules_python`, `rules_cc` and `rules_rust` as dependencies
(for the Python toolkit and the C++/Rust hooks); googletest is only a development
dependency — the googletest hook is header-only and uses your own googletest. Their versions are
*floors* — Bazel's minimal version selection keeps whatever newer versions your
workspace already uses — and no toolchain is downloaded unless a target that
needs it is built (rules_rust's own toolchain registration does fetch the
rules_rust sources, but no Rust compiler). The Python library itself uses only the standard library, so it runs on
whichever Python toolchain your workspace registers (3.9 or newer).

Tested with Bazel 7.7.1 and 8.8.1.

### With pip

```console
$ pip install "rules-requirements @ git+https://github.com/Studio-Fug/rules_requirements"
$ rr --help
```

This installs the `rr` command and registers the pytest plugin automatically.

## 1. Write the model

A model is one or more YAML files. Put them in a `requirements/` directory:

```yaml
# requirements/model.yaml
project:
  name: Kettle

user_needs:
  - id: UN-1
    title: Boil water quickly

requirements:
  - id: REQ-1
    title: Heat until the water reaches 100 °C
    satisfies: [UN-1]
  - id: REQ-2
    title: Cut power when the kettle is empty
    method: hil          # this one must be proven on real hardware

risks:
  - id: RISK-1
    title: Dry boil overheats the element
    severity: high
    likelihood: possible

mitigations:
  - id: MIT-1
    title: Dry-boil cutoff
    type: protective
    mitigates: [RISK-1]
    implemented_by: [REQ-2]
```

Check it:

```console
$ rr validate requirements/
model OK: 1 user needs, 2 requirements, 1 risks, 1 mitigations, 0 test methods
```

Validation catches dangling references, requirements that trace to nothing,
needs that nothing satisfies, risks without controls and misspelt field names;
see {doc}`guides/model` for every rule.

## 2. Tag the tests

Tests declare which ids they verify. With pytest:

```python
import pytest


@pytest.mark.rr("REQ-1")
def test_heats_to_boiling(): ...


@pytest.mark.rr("REQ-2", level="simulation")
def test_cutoff_logic_in_simulation(): ...
```

The marker becomes JUnit `<property name="requirement" value="REQ-1"/>` entries
on the test case. googletest, Rust and unittest have equivalent hooks
({doc}`guides/hooks`).

## 3. Build the report

### In Bazel, hermetically

```starlark
# BUILD.bazel
load("@pypi//:requirements.bzl", "requirement")
load("@rules_requirements//rr:defs.bzl", "rr_evidence", "rr_golden_test", "rr_model", "rr_py_test", "rr_report")

rr_model(
    name = "model",
    srcs = glob(["requirements/*.yaml"]),   # also creates :model_test
)

rr_py_test(
    name = "kettle_test",
    srcs = ["test_kettle.py"],
    deps = [":kettle", requirement("pytest")],
)

rr_evidence(
    name = "evidence",
    tests = [":kettle_test"],               # run inside a build action
)

rr_report(
    name = "report",                        # :report.html, :report.json, :report.md
    model = [":model"],
    evidence = [":evidence"],
)

rr_golden_test(
    name = "report_golden_test",
    src = ":report.json",
    golden = "report.golden.json",          # accept changes: bazel run :report_golden_test.update
)
```

```console
$ bazel build //:report && open bazel-bin/report.html
```

### From real test logs

For suites that cannot run inside a build action (for example on a
hardware-in-the-loop bench), run the tests normally and aggregate the logs:

```console
$ bazel test //...
$ bazel run @rules_requirements//python:rr -- report \
    --model requirements/ --evidence "$(bazel info bazel-testlogs)" \
    --html report.html --json report.json --queue-out gaps.json
```

### Without Bazel

```console
$ pytest --junitxml=results.xml
$ rr report --model requirements/ --evidence results.xml --html report.html
```

The pip-installed plugin records each marker as a property of its JUnit test
case; pytest writes those properties in every `junit_family`.

## 4. Read it

For the model above, with both tests passing, the report shows `REQ-1`
**VERIFIED** and `UN-1` **VALIDATED** — but `REQ-2` only **UNDER-VERIFIED**:
it demands `hil` and the only evidence is a simulation. Consequently `MIT-1` is
**PARTIAL** and `RISK-1`, a high-severity risk, is hoisted into a banner at the
top of the report. The gap queue carries two items, both routed to
`human-gate` because closing them needs a bench: `under-verified` for `REQ-2`
and `high-risk-open` for `RISK-1`.

{doc}`concepts` explains every verdict; {doc}`guides/outputs` every output.
