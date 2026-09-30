# SPDX-License-Identifier: AGPL-3.0-or-later
"""pytest plugin: ``@pytest.mark.rr("REQ-1", level="hil")``.

Each marked test gets one ``<property name="requirement">`` per id (and a
``level`` property) on its JUnit ``<testcase>``; run pytest with
``--junitxml=... -o junit_family=xunit2`` (the
:mod:`~rules_requirements.hooks.pytest_runner` does this for Bazel).

* The marker is also available as ``@pytest.mark.requirements(...)``.
* Module- or class-level ``pytestmark`` sets a default; the nearest marker
  naming a ``level`` wins, and ids from every level accumulate.
* ``unittest.TestCase`` methods decorated with
  :func:`rules_requirements.rr.verifies` are honoured too.
* ``artifact={"key": "value"}`` stamps artifact identity for staleness checks.

Installed with pip, the plugin auto-registers through the ``pytest11`` entry
point; under Bazel pass it explicitly (the runner does).
"""

from __future__ import annotations

from typing import Any

from rules_requirements.util import dedupe

MARKERS = ("rr", "requirements")


def pytest_configure(config: Any) -> None:
    for name in MARKERS:
        config.addinivalue_line(
            "markers",
            f"{name}(*ids, level=None, artifact=None): requirement ids this test verifies "
            "and the verification level it provides (rules_requirements).",
        )


def _split(arg: Any) -> list[str]:
    if isinstance(arg, (list, tuple, set)):
        return [str(a).strip() for a in arg]
    return [p.strip() for p in str(arg).split(",")]


def trace_of(item: Any) -> tuple[list[str], str, dict[str, str]]:
    """(ids, level, artifact) declared for a collected test item."""
    ids: list[str] = []
    level = ""
    artifact: dict[str, str] = {}
    for name in MARKERS:
        for marker in item.iter_markers(name=name):
            for arg in marker.args:
                ids.extend(_split(arg))
            if not level and marker.kwargs.get("level"):
                level = str(marker.kwargs["level"]).strip().lower()
            for k, v in (marker.kwargs.get("artifact") or {}).items():
                artifact.setdefault(str(k), str(v))
    # unittest-style decorator (rules_requirements.rr.verifies)
    fn = getattr(item, "obj", None)
    for holder in (fn, getattr(item, "cls", None)):
        rr = getattr(holder, "__rr__", None)
        if rr:
            ids.extend(rr.get("ids", ()))
            level = level or rr.get("level", "")
            for k, v in (rr.get("artifact") or {}).items():
                artifact.setdefault(k, v)
    return dedupe(ids), level, artifact


def pytest_runtest_setup(item: Any) -> None:
    # user_properties recorded before the body runs land on the testcase
    # whether it passes, fails or is skipped.
    ids, level, artifact = trace_of(item)
    for rid in ids:
        item.user_properties.append(("requirement", rid))
    if level:
        item.user_properties.append(("level", level))
    for k, v in sorted(artifact.items()):
        item.user_properties.append((f"artifact.{k}", v))
