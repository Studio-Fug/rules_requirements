# SPDX-License-Identifier: AGPL-3.0-or-later
"""pytest plugin: ``@pytest.mark.rr("REQ-1", level="hil")``.

Each marked test gets one ``<property name="requirement">`` per id (and a
``level`` property) on its JUnit ``<testcase>``. A test case verifies at most
one requirement: a marker naming several ids (several arguments, or a comma or
whitespace inside one) is deprecated and warns with
:class:`~rules_requirements.hooks.ids.MultipleRequirementsWarning`, though
every id is still recorded. Run pytest with
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

from typing import Any, Callable

try:  # importable without pytest (e.g. a wheel smoke test); hooks need it only under pytest
    import pytest

    _trylast: Callable[[Any], Any] = pytest.hookimpl(trylast=True)
except ImportError:  # pragma: no cover - pytest is always present when the hooks run
    pytest = None  # type: ignore[assignment]

    def _trylast(fn: Any) -> Any:
        return fn


from rules_requirements.hooks.ids import split_ids, warn_multiple
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


@_trylast
def pytest_collection_modifyitems(items: list[Any]) -> None:
    # Recorded once collection is final — after every conftest's
    # collection_modifyitems has added its markers — and before any test
    # runs, so the properties reach the JUnit testcase however the test ends,
    # including tests skipped by @pytest.mark.skip / skipif.
    warned: set[str] = set()
    for item in items:
        _warn_multiple(item, warned)
        _record(item)


def _warn_multiple(item: Any, warned: set[str]) -> None:
    """Warn once per node whose ``rr`` / ``requirements`` markers name several ids.

    Only markers are checked here: ``@rr.verifies`` warns when it decorates.
    Ids accumulated across scopes (a module marker plus a function marker) are
    not a multi-id declaration.
    """
    by_node: dict[str, tuple[Any, list[str]]] = {}
    for node, marker in item.iter_markers_with_node():
        if marker.name in MARKERS:
            by_node.setdefault(node.nodeid, (node, []))[1].extend(split_ids(list(marker.args)))
    for nodeid, (_node, ids) in by_node.items():
        distinct = dedupe(ids)
        if len(distinct) > 1 and nodeid not in warned:
            warned.add(nodeid)
            warn_multiple(f"{nodeid or item.nodeid}: marker", distinct)


def _record(item: Any) -> None:
    ids, level, artifact = trace_of(item)
    for rid in ids:
        item.user_properties.append(("requirement", rid))
    if level:
        item.user_properties.append(("level", level))
    for k, v in sorted(artifact.items()):
        item.user_properties.append((f"artifact.{k}", v))
