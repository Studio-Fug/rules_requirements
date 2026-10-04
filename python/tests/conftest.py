# SPDX-License-Identifier: AGPL-3.0-or-later
import os
import subprocess
import sys
import textwrap

import pytest

# Allow running from a plain checkout (`pytest python/tests`) as well as Bazel.
_HERE = os.path.dirname(os.path.abspath(__file__))
_PKG_ROOT = os.path.dirname(_HERE)
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)

from rules_requirements.model import read_model  # noqa: E402

# Source trees that tests copy and rewrite (e.g. `rr migrate apply`); their
# test_*.py files are data, not tests of this package.
collect_ignore = ["fixtures"]


@pytest.fixture(scope="session", autouse=True)
def _child_interpreter_sees_the_suite_deps():
    """`rr migrate apply`'s collection check runs `sys.executable -m pytest`.
    Under Bazel 7, rules_python's bootstrap builds no venv: the deps are on
    this process's sys.path only, and a child interpreter cannot import
    pytest. Hand them over through PYTHONPATH, only when that is the case."""
    probe = subprocess.run([sys.executable, "-c", "import pytest"], capture_output=True)
    if probe.returncode == 0:
        yield
        return
    old = os.environ.get("PYTHONPATH")
    os.environ["PYTHONPATH"] = os.pathsep.join([p for p in sys.path if p] + ([old] if old else []))
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("PYTHONPATH", None)
        else:
            os.environ["PYTHONPATH"] = old


MODEL = """
project:
  name: Thermostat
user_needs:
  - id: UN-1
    title: Keep the room comfortable
  - id: UN-2
    title: Choose the target temperature
requirements:
  - id: REQ-1
    title: Heat below setpoint
    satisfies: [UN-1]
    modules: [controller]
  - id: REQ-2
    title: Accept setpoints between 5 and 30 C
    satisfies: [UN-2]
    modules: [controller]
  - id: REQ-3
    title: Cut the heater at 35 C
    method: TM-1
    modules: [interlock]
risks:
  - id: RISK-1
    title: Room overheats
    hazard: Heater stuck on
    harm: Burns, heat stress
    severity: high
    likelihood: possible
    residual_likelihood: rare
mitigations:
  - id: MIT-1
    title: Independent over-temperature cutoff
    type: protective
    mitigates: [RISK-1]
    implemented_by: [REQ-3]
test_methods:
  - id: TM-1
    title: Interlock bench test
    level: hil
"""


def write(tmp_path, name, text):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")
    return str(path)


@pytest.fixture
def model_path(tmp_path):
    return write(tmp_path, "model.yaml", MODEL)


@pytest.fixture
def model(model_path):
    m, warnings = read_model(model_path)
    assert not m.parse_errors, m.parse_errors
    assert not warnings
    return m


def junit(tmp_path, name, cases):
    """Write a JUnit file. cases: list of (name, status, reqs, level, extra_props)."""
    rows = []
    for case in cases:
        cname, status, reqs, level = case[:4]
        props = list(case[4]) if len(case) > 4 else []
        props = [("requirement", r) for r in reqs] + ([("level", level)] if level else []) + props
        prop_xml = "".join(f'<property name="{k}" value="{v}"/>' for k, v in props)
        body = f"<properties>{prop_xml}</properties>" if props else ""
        if status == "failed":
            body += '<failure message="boom">trace</failure>'
        elif status == "error":
            body += '<error message="err"/>'
        elif status == "skipped":
            body += '<skipped message="later"/>'
        rows.append(f'<testcase classname="suite" name="{cname}" time="0.01">{body}</testcase>')
    xml = f'<?xml version="1.0"?><testsuites><testsuite name="s">{"".join(rows)}</testsuite></testsuites>'
    return write(tmp_path, name, xml)
