# SPDX-License-Identifier: AGPL-3.0-or-later
"""pytest plugin: ``@pytest.mark.rr("REQ-1", level="hil")``.

A test case verifies at most one requirement, so each test declares at most
one id: the plugin writes one ``<property name="requirement">`` (plus
``level``, ``artifact.*`` and ``rr.file``, the test file relative to the
workspace) on its JUnit ``<testcase>``. The id is a *declared tag*: which
requirement the case verifies is decided by attribution, never by the hook.

* Scopes resolve nearest first, and only the nearest scope that names an id
  is written: ``pytest.param(..., marks=...)``, then the test function
  (decorators, a conftest's ``item.add_marker``, ``@rr.verifies``), its class
  (a subclass's own declaration before its base's), then the module or
  package ``pytestmark``. A nearer id replaces a farther one; it is never
  added to it. The nearest marker naming a ``level`` wins, and artifact keys
  resolve nearest-first.
* A declaration naming several ids (several arguments, a list, a comma or
  whitespace inside one argument, or two declarations in one scope) is
  deprecated: it warns with
  :class:`~rules_requirements.hooks.ids.MultipleRequirementsWarning`, and
  every id is still written, so attribution quarantines the case and it
  counts for none of them. The warning is raised once per declaration, when
  the first test it applies to sets up, attributed to the marker's test,
  class or module, so an escalated warning (``-W error``) errors that test
  alone.
* A raw ``record_property("requirement", ...)`` (or ``"requirements"``)
  bypasses these rules: the property is dropped and the test fails with
  RR-E102. Use the marker.
* The marker is also available as ``@pytest.mark.requirements(...)``.
* ``unittest.TestCase`` methods decorated with
  :func:`rules_requirements.rr.verifies` are honoured too.
* ``artifact={"key": "value"}`` stamps artifact identity for staleness checks.

Run pytest with ``--junitxml=... -o junit_family=xunit2`` (the
:mod:`~rules_requirements.hooks.pytest_runner` does this for Bazel).
Installed with pip, the plugin auto-registers through the ``pytest11`` entry
point; under Bazel pass it explicitly (the runner does).
"""

from __future__ import annotations

import warnings
from typing import Any, Callable, Dict, Hashable, Iterator, List, Tuple

try:  # importable without pytest (e.g. a wheel smoke test); hooks need it only under pytest
    import pytest

    _trylast: Callable[[Any], Any] = pytest.hookimpl(trylast=True)
    _tryfirst: Callable[[Any], Any] = pytest.hookimpl(tryfirst=True)
except ImportError:  # pragma: no cover - pytest is always present when the hooks run
    pytest = None  # type: ignore[assignment]

    def _trylast(fn: Any) -> Any:
        return fn

    _tryfirst = _trylast


from rules_requirements.hooks.ids import E_RAW_PROPERTY, MultipleRequirementsWarning, multiple_warning, split_ids
from rules_requirements.hooks.junit_writer import FILE_PROPERTY, source_file
from rules_requirements.util import dedupe

MARKERS = ("rr", "requirements")


def pytest_configure(config: Any) -> None:
    for name in MARKERS:
        config.addinivalue_line(
            "markers",
            f"{name}(id, level=None, artifact=None): the ONE requirement id this test verifies "
            "and the verification level it provides (rules_requirements).",
        )


Declaration = Tuple[List[str], str, Dict[str, str]]  # (ids, level, artifact)


def _declaration(ids: Any, level: Any, artifact: Any) -> Declaration:
    return (
        split_ids(list(ids)),
        str(level or "").strip().lower(),
        {str(k): str(v) for k, v in (artifact or {}).items()},
    )


def _scopes(item: Any) -> list[list[Declaration]]:
    """Every scope's declarations for ``item``, nearest scope first.

    The scopes, in order: the ``pytest.param(..., marks=...)`` marks; the
    test function (its markers, ``item.add_marker`` from a conftest, its
    ``@rr.verifies``); its class, the subclass's own declaration before each
    base class's (by MRO), each class's markers together with its
    ``@rr.verifies``; then every enclosing node (an outer class, the module
    ``pytestmark``, a package), nearest first. Both marker names (``rr`` and
    ``requirements``) share one order.
    """
    fn = getattr(item, "obj", None)
    fn = getattr(fn, "__func__", fn)
    callspec = getattr(item, "callspec", None)
    unclaimed = [m for m in (getattr(callspec, "marks", None) or []) if m.name in MARKERS]
    cls_node = item.getparent(pytest.Class) if pytest is not None and hasattr(item, "getparent") else None
    cls_obj = getattr(cls_node, "obj", None) if cls_node is not None else None
    mro = list(getattr(cls_obj, "__mro__", ()) or ())
    mark_owner = _own_class_marks(cls_obj)

    param: list[Declaration] = []
    function: list[Declaration] = []
    classes: dict[int, list[Declaration]] = {}  # MRO index -> that class's own declarations
    others: dict[int, list[Declaration]] = {}  # id(node) -> declarations, nearest node first
    for node, marker in item.iter_markers_with_node():
        if marker.name not in MARKERS:
            continue
        decl = _declaration(marker.args, marker.kwargs.get("level"), marker.kwargs.get("artifact"))
        if node is item:
            if marker in unclaimed:  # the param's marks sit on the function node too
                unclaimed.remove(marker)
                param.append(decl)
            else:
                function.append(decl)
        elif cls_node is not None and node is cls_node:
            owner = mark_owner.get(id(marker), cls_obj)
            classes.setdefault(mro.index(owner) if owner in mro else 0, []).append(decl)
        else:
            others.setdefault(id(node), []).append(decl)
    rr_fn = _own_rr(fn)
    if rr_fn is not None:
        function.append(rr_fn)
    for index, klass in enumerate(mro):
        rr_cls = _own_rr(klass)
        if rr_cls is not None:
            classes.setdefault(index, []).append(rr_cls)
    return [param, function, *(classes[i] for i in sorted(classes)), *others.values()]


def _declarations(item: Any) -> list[Declaration]:
    """Every (ids, level, artifact) declaration for ``item``, nearest scope first."""
    return [decl for scope in _scopes(item) for decl in scope]


def trace_of(item: Any) -> tuple[list[str], str, dict[str, str]]:
    """(ids, level, artifact) declared for a collected test item.

    ``ids`` are those of the NEAREST scope that names any (param marks, then
    the function, its class, the module, a package): a nearer declaration
    replaces a farther one, it does not add to it. That is one id, or none;
    several only for a deprecated multi-id declaration (several ids in one
    marker, or two declarations in one scope), which the hook records in
    full so attribution quarantines the case. The nearest declaration naming
    a level wins, and artifact keys resolve nearest-first.
    """
    ids: list[str] = []
    level = ""
    artifact: dict[str, str] = {}
    for scope in _scopes(item):
        scope_ids = dedupe([i for decl_ids, _, _ in scope for i in decl_ids])
        if scope_ids and not ids:
            ids = scope_ids
        for _, decl_level, decl_artifact in scope:
            level = level or decl_level
            for k, v in decl_artifact.items():
                artifact.setdefault(k, v)
    return ids, level, artifact


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
    Ids declared at several scopes (a module marker plus a function marker, a
    param mark plus a function marker, a subclass's declaration plus its
    base's) are not a multi-id declaration: the nearest one wins.
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
    pytest merges onto the subclass's Class node.

    ``pytestmark`` holds ``Mark`` objects when set by a decorator, but
    ``MarkDecorator`` objects when assigned in the class body, as a list or a
    bare one. pytest unpacks a decorator to its ``.mark``, the very object
    ``iter_markers_with_node`` yields, so key by that."""
    owner: dict[int, Any] = {}
    for klass in getattr(cls_obj, "__mro__", ()) or ():
        try:
            marks = vars(klass).get("pytestmark")
        except TypeError:  # pragma: no cover - a class always has __dict__
            continue
        if marks is None:
            continue
        if not isinstance(marks, (list, tuple)):
            marks = [marks]  # `pytestmark = pytest.mark.rr(...)`
        for m in marks:
            m = getattr(m, "mark", m)
            if getattr(m, "name", None) in MARKERS:
                owner.setdefault(id(m), klass)
    return owner


def _own_rr(obj: Any) -> Declaration | None:
    """The ``@rr.verifies`` declaration on ``obj`` itself (not inherited from a base class)."""
    if obj is None:
        return None
    try:
        own = vars(obj).get("__rr__")
    except TypeError:  # no __dict__
        return None
    if not isinstance(own, dict):
        return None
    return _declaration(own.get("own_ids", own.get("ids")) or [], own.get("level"), own.get("artifact"))


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


_WRITTEN = "_rules_requirements_properties"  # item attribute: the property tuples this plugin wrote
_TRACE_PROPERTIES = ("requirement", "requirements")


def _record(item: Any) -> None:
    ids, level, artifact = trace_of(item)
    props: list[tuple[str, str]] = [("requirement", rid) for rid in ids]
    if level:
        props.append(("level", level))
    props.extend((f"artifact.{k}", v) for k, v in sorted(artifact.items()))
    path = getattr(item, "path", None) or getattr(item, "fspath", None)
    if path is not None:
        props.append((FILE_PROPERTY, source_file(str(path))))
    item.user_properties.extend(props)
    setattr(item, _WRITTEN, tuple(props))


def _pair(prop: Any) -> tuple[Any, Any] | None:
    """``prop`` unpacked to ``(name, value)`` the way junitxml does
    (``for name, value in user_properties``), or None if it does not unpack.

    Any two-item iterable is a property to junitxml (a tuple, a list, a
    two-key dict), so the guard must not care what shape the entry has.
    """
    try:
        name, value = prop
    except Exception:
        return None
    return name, value


def _materialize(item: Any, report: Any) -> None:
    """Replace one-shot iterator entries with tuples, in the item and the report.

    The guard unpacks every entry to read its name; an iterator would be
    consumed by that, and junitxml would then see an empty one. The item and
    the report hold the SAME entry objects, so each is materialized once.
    """
    done: dict[int, tuple[Any, ...]] = {}
    for props in (item.user_properties, getattr(report, "user_properties", None)):
        if not isinstance(props, list):
            continue
        for i, prop in enumerate(props):
            try:
                one_shot = iter(prop) is prop
            except TypeError:
                continue
            if one_shot:
                if id(prop) not in done:
                    done[id(prop)] = tuple(prop)
                props[i] = done[id(prop)]


def _raw_trace_properties(item: Any, properties: list[Any]) -> list[Any]:
    """The ``requirement`` / ``requirements`` entries of ``properties`` this
    plugin did not write (a raw ``record_property``), by identity.

    An entry is a trace property if junitxml would write it as one: any
    two-item entry (a tuple, a list, ...) whose name, as junitxml writes it
    (``str(name)``), is ``requirement`` or ``requirements``.
    """
    written = getattr(item, _WRITTEN, ())
    raw = []
    for p in properties:
        if any(p is w for w in written):
            continue
        pair = _pair(p)
        if pair is not None and str(pair[0]) in _TRACE_PROPERTIES:
            raw.append(p)
    return raw


def _guard_raw_properties(item: Any, report: Any) -> None:
    """RR-E102: drop raw ``requirement`` properties and fail the phase that recorded them.

    ``record_property("requirement", ...)`` would declare an id the marker
    rules never saw (a second id next to the marker's, or one that a nearer
    scope should have replaced), so the property is removed from the item
    and the report, and the test fails: use ``@pytest.mark.rr``.
    """
    _materialize(item, report)
    raw = _raw_trace_properties(item, list(item.user_properties))
    if raw:
        item.user_properties[:] = [p for p in item.user_properties if not any(p is r for r in raw)]
    stale = _raw_trace_properties(item, list(getattr(report, "user_properties", None) or []))
    if stale:
        report.user_properties = [p for p in report.user_properties if not any(p is r for r in stale)]
    if not raw:
        return
    named = ", ".join(f"record_property({str(name)!r}, {value!r})" for name, value in filter(None, map(_pair, raw)))
    message = (
        f"rr: {item.nodeid}: {named} bypasses @pytest.mark.rr and was dropped [{E_RAW_PROPERTY}]: "
        'declare the ONE requirement a test verifies with @pytest.mark.rr("<id>")'
    )
    if report.outcome == "failed":
        report.sections.append(("rules_requirements", message))
    else:
        report.outcome = "failed"
        report.longrepr = message
    if hasattr(report, "wasxfail"):
        # An xfail marker turns a failure into "xfailed" (junitxml writes it
        # as <skipped>): the guard's failure is not the expected one.
        del report.wasxfail


if pytest is not None:

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_makereport(item: Any, call: Any) -> Iterator[None]:
        outcome: Any = yield
        report = outcome.get_result()
        if report is not None:
            _guard_raw_properties(item, report)
