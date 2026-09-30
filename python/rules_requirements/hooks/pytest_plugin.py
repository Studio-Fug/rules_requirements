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


def _declarations(item: Any) -> list[tuple[list[str], str, dict[str, str]]]:
    """Every (ids, level, artifact) declaration, nearest scope first.

    Order: the test function's markers, its ``@rr.verifies`` decorator, the
    class's markers, the class decorator, then module/package markers. Both
    marker names (``rr`` and ``requirements``) share one order.
    """
    import pytest

    fn = getattr(item, "obj", None)
    cls = getattr(item, "cls", None)
    by_scope: dict[str, list[tuple[list[str], str, dict[str, str]]]] = {"function": [], "class": [], "other": []}
    for node, marker in item.iter_markers_with_node():
        if marker.name not in MARKERS:
            continue
        scope = "function" if node is item else "class" if isinstance(node, pytest.Class) else "other"
        ids = [i for arg in marker.args for i in _split(arg)]
        level = str(marker.kwargs.get("level") or "").strip().lower()
        artifact = {str(k): str(v) for k, v in (marker.kwargs.get("artifact") or {}).items()}
        by_scope[scope].append((ids, level, artifact))
    for scope, holder in (("function", getattr(fn, "__func__", fn)), ("class", cls)):
        rr = getattr(holder, "__rr__", None) if holder is not None else None
        if rr:
            by_scope[scope].append((list(rr.get("ids", ())), str(rr.get("level", "")), dict(rr.get("artifact") or {})))
    return by_scope["function"] + by_scope["class"] + by_scope["other"]


def trace_of(item: Any) -> tuple[list[str], str, dict[str, str]]:
    """(ids, level, artifact) declared for a collected test item.

    Ids from every scope accumulate; the nearest declaration naming a level
    wins, and artifact keys resolve nearest-first.
    """
    ids: list[str] = []
    level = ""
    artifact: dict[str, str] = {}
    for decl_ids, decl_level, decl_artifact in _declarations(item):
        ids.extend(decl_ids)
        level = level or decl_level
        for k, v in decl_artifact.items():
            artifact.setdefault(k, v)
    return dedupe(ids), level, artifact


def pytest_itemcollected(item: Any) -> None:
    # Recorded at collection time so the properties reach the JUnit testcase
    # however the test ends — including tests skipped by @pytest.mark.skip /
    # skipif, whose setup hooks never run.
    ids, level, artifact = trace_of(item)
    for rid in ids:
        item.user_properties.append(("requirement", rid))
    if level:
        item.user_properties.append(("level", level))
    for k, v in sorted(artifact.items()):
        item.user_properties.append((f"artifact.{k}", v))
