# SPDX-License-Identifier: AGPL-3.0-or-later
"""pytest plugin: ``@pytest.mark.rr("REQ-1", level="hil")``.

Each marked test gets one ``<property name="requirement">`` per id (and a
``level`` property) on its JUnit ``<testcase>``. A test case verifies at most
one requirement: a marker naming several ids (several arguments, a list, or a
comma-separated string, which is split on its commas) is deprecated and warns
with :class:`~rules_requirements.hooks.ids.MultipleRequirementsWarning`, though
every id is still recorded. Whitespace does not separate ids:
``rr("REQ-1 REQ-2")`` records the one id ``"REQ-1 REQ-2"``, as it always has,
which matches no requirement (the report lists it as an undefined id). The
warning is raised once per declaration, when the first test the marker applies
to sets up, attributed to the marker's test, class or module, so an escalated
warning (``-W error``) errors that test alone. Run pytest with
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
    """(key, subject, ids, location) of each declaration naming several ids for ``item``.

    A declaration is one scope's own: a node's ``rr`` / ``requirements``
    markers, a ``pytest.param(..., marks=...)`` (a scope of its own, nearer
    than the function), and, together with the markers of the same function
    or class, that function's or class's own ``@rr.verifies``. The key
    identifies the declaration, so it warns once however many tests it
    applies to (every test of a module, every parameter of a test).
    ``@rr.verifies`` naming several ids by itself warns when it decorates.
    Ids accumulated across scopes (a module marker plus a function marker, a
    param mark plus a function marker, a subclass's declaration plus its
    base's) are not a multi-id declaration: the nearest one wins from 0.3.
    """
    callspec = getattr(item, "callspec", None)
    param_marks = [m for m in (getattr(callspec, "marks", None) or []) if m.name in MARKERS]
    param_ids = dedupe([i for m in param_marks for i in split_ids(list(m.args))])
    if len(param_ids) > 1:
        yield (item.nodeid, tuple(param_ids)), f"{item.nodeid}: pytest.param marks", param_ids, _location(item, item)

    fn = getattr(item, "obj", None)
    fn = getattr(fn, "__func__", fn)
    cls_node = item.getparent(pytest.Class) if pytest is not None else None
    cls_obj = getattr(cls_node, "obj", None) if cls_node is not None else None
    # pytest (>=7.2) merges a base class's pytestmark into the subclass's Class
    # node, so its markers are yielded against the subclass node. Group each by
    # the class that actually declares it, so a base's own declaration is a
    # separate site the nearest-scope rule resolves, not a multi-id declaration.
    mark_owner = _own_class_marks(cls_obj)

    by_node: dict[Any, tuple[Any, list[str]]] = {}
    unclaimed = list(param_marks)  # the param's marks sit on the function node too
    for node, marker in item.iter_markers_with_node():
        if marker.name not in MARKERS:
            continue
        if node is item and marker in unclaimed:
            unclaimed.remove(marker)
            continue
        key: Any = id(node)
        if cls_node is not None and node is cls_node:
            owner = mark_owner.get(id(marker))
            if owner is not None and owner is not cls_obj:
                key = (id(node), id(owner))  # a base class's own declaration site
        by_node.setdefault(key, (node, []))[1].extend(split_ids(list(marker.args)))
    own_rr = {
        id(item): _own_rr_ids(fn),
        **({id(cls_node): _own_rr_ids(getattr(cls_node, "obj", None))} if cls_node is not None else {}),
    }
    for node_id, node in ((id(item), item), *(((id(cls_node), cls_node),) if cls_node is not None else ())):
        if own_rr.get(node_id):
            by_node.setdefault(node_id, (node, []))
    for node_id, (node, ids) in by_node.items():
        distinct = dedupe(ids)
        nodeid = node.nodeid or item.nodeid
        node_callspec = getattr(node, "callspec", None)
        if node_callspec is not None and nodeid.endswith(f"[{node_callspec.id}]"):
            nodeid = nodeid[: -len(node_callspec.id) - 2]  # one declaration for every parameter
        if len(distinct) > 1:
            yield (nodeid, tuple(distinct)), f"{nodeid}: marker", distinct, _location(node, item)
            continue
        verified = own_rr.get(node_id, [])
        both = dedupe(distinct + verified)
        if distinct and verified and len(verified) < 2 and len(both) > 1:
            # One scope, two declarations: a marker and @rr.verifies on the
            # same function (or class) name different ids.
            yield (nodeid, "rr.verifies", tuple(both)), f"{nodeid}: marker and rr.verifies", both, _location(node, item)


def _own_class_marks(cls_obj: Any) -> dict[int, Any]:
    """Map ``id(Mark) -> owning class`` for the ``rr`` / ``requirements``
    markers each class in the MRO declares in its OWN ``pytestmark`` (not
    inherited). Lets a subclass tell its own markers from a base class's, which
    pytest merges onto the subclass's Class node."""
    owner: dict[int, Any] = {}
    for klass in getattr(cls_obj, "__mro__", ()) or ():
        try:
            marks = vars(klass).get("pytestmark")
        except TypeError:  # pragma: no cover - a class always has __dict__
            continue
        if isinstance(marks, (list, tuple)):
            for m in marks:
                if getattr(m, "name", None) in MARKERS:
                    owner.setdefault(id(m), klass)
    return owner


def _own_rr_ids(obj: Any) -> list[str]:
    """The ids of ``@rr.verifies`` on ``obj`` itself (not inherited from a base class)."""
    if obj is None:
        return []
    try:
        own = vars(obj).get("__rr__")
    except TypeError:  # no __dict__
        return []
    if not isinstance(own, dict):
        return []
    ids: Any = own.get("own_ids", own.get("ids")) or []
    return split_ids(list(ids))


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
