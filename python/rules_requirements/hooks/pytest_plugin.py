# SPDX-License-Identifier: AGPL-3.0-or-later
"""pytest plugin: ``@pytest.mark.rr("REQ-1", level="hil")``.

Each marked test gets one ``<property name="requirement">`` per id (and a
``level`` property) on its JUnit ``<testcase>``. A test case verifies at most
one requirement: a marker naming several ids (several arguments, or a comma or
whitespace inside one) is deprecated and warns with
:class:`~rules_requirements.hooks.ids.MultipleRequirementsWarning`, though
every id is still recorded. The warning is raised when the first test the
marker applies to sets up, attributed to the marker's test, class or module,
so an escalated warning (``-W error``) errors that test alone. Run pytest with
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

import warnings
from typing import Any, Callable, Hashable, Iterator

try:  # importable without pytest (e.g. a wheel smoke test); hooks need it only under pytest
    import pytest

    _trylast: Callable[[Any], Any] = pytest.hookimpl(trylast=True)
    _tryfirst: Callable[[Any], Any] = pytest.hookimpl(tryfirst=True)
except ImportError:  # pragma: no cover - pytest is always present when the hooks run
    pytest = None  # type: ignore[assignment]

    def _trylast(fn: Any) -> Any:
        return fn

    _tryfirst = _trylast


from rules_requirements.hooks.ids import MultipleRequirementsWarning, multiple_warning, split_ids
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
    for item in items:
        _record(item)


_WARNED = "_rules_requirements_multi_id_warned"


@_tryfirst
def pytest_runtest_setup(item: Any) -> None:
    # Warned here, inside the test's own warning capture, and not from a
    # collection hook: escalated (-W error, filterwarnings = error) it errors
    # this test instead of aborting the whole session (an internal error).
    warned: set[Hashable] = getattr(item.config, _WARNED, None) or set()
    setattr(item.config, _WARNED, warned)
    for key, subject, ids, (filename, lineno, module) in _multi_id_declarations(item):
        if key in warned:
            continue
        warned.add(key)
        warnings.warn_explicit(
            multiple_warning(subject, ids), MultipleRequirementsWarning, filename, lineno, module=module
        )


def _multi_id_declarations(item: Any) -> Iterator[tuple[Hashable, str, list[str], tuple[str, int, Any]]]:
    """(key, subject, ids, location) of each node whose ``rr`` /
    ``requirements`` markers name several ids for ``item``.

    The key identifies the declaration, so it warns once however many tests
    it applies to (every test of a module, every parameter of a test). Only
    markers are checked here: ``@rr.verifies`` warns when it decorates. Ids
    accumulated across scopes (a module marker plus a function marker) are
    not a multi-id declaration.
    """
    by_node: dict[int, tuple[Any, list[str]]] = {}
    for node, marker in item.iter_markers_with_node():
        if marker.name in MARKERS:
            by_node.setdefault(id(node), (node, []))[1].extend(split_ids(list(marker.args)))
    for node, ids in by_node.values():
        distinct = dedupe(ids)
        if len(distinct) < 2:
            continue
        nodeid = node.nodeid or item.nodeid
        callspec = getattr(node, "callspec", None)
        if callspec is not None and nodeid.endswith(f"[{callspec.id}]"):
            nodeid = nodeid[: -len(callspec.id) - 2]  # one declaration for every parameter
        yield (nodeid, tuple(distinct)), f"{nodeid}: marker", distinct, _location(node, item)


def _location(node: Any, item: Any) -> tuple[str, int, Any]:
    """(filename, 1-based line, module name) to attribute a warning about ``node`` to."""
    try:
        path, lineno, _ = node.reportinfo()
    except Exception:  # a directory or package node
        path, lineno = getattr(node, "path", None) or item.path, 0
    try:
        module = getattr(getattr(node, "module", None), "__name__", None)
    except Exception:
        module = None
    return str(path), (lineno or 0) + 1, module


def _record(item: Any) -> None:
    ids, level, artifact = trace_of(item)
    for rid in ids:
        item.user_properties.append(("requirement", rid))
    if level:
        item.user_properties.append(("level", level))
    for k, v in sorted(artifact.items()):
        item.user_properties.append((f"artifact.{k}", v))
