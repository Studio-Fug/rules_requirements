# SPDX-License-Identifier: AGPL-3.0-or-later
"""Test-evidence ingestion: turn test reports into :class:`TestCase` records.

JUnit XML is the standard interchange format — every hook in this project
emits it, and most test runners can produce it. Other formats plug in through
the :class:`Ingestor` API:

.. code-block:: python

    from rules_requirements.ingest import Ingestor, TestCase, register

    class TapIngestor(Ingestor):
        name = "tap"
        suffixes = (".tap",)

        def sniff(self, path, head):
            return head.lstrip().startswith(b"TAP version")

        def ingest(self, path):
            ...  # yield TestCase(...)

    register(TapIngestor())

Ingestors can also be published by other packages under the
``rules_requirements.ingestors`` entry-point group, or loaded ad hoc with the
CLI's ``--ingestor module:attr`` flag.

Traceability travels inside the evidence as test-case *properties*:

``requirement``
    The entity id the case declares it verifies — a *tag*, recorded in
    ``TestCase.declared``. Ingest never decides ownership: a tag is a
    cross-check (or, in hybrid mode, a claim for an unclaimed case) that only
    ``rules_requirements.attribution.attribute()`` resolves. A case naming
    more than one distinct id (a repeated property, ``requirement="A,B"``,
    the plural ``requirements``, a list in a Rust trace line, two
    ``[rr:ID]`` name tags) keeps every id, and is quarantined there
    (``multi-tag``): a test case verifies at most one requirement.
``level``
    The verification rigor the case provides (e.g. ``hil``).
``artifact.<key>``
    Identity of the thing under test (firmware build id, DUT git SHA, ...)
    used to detect stale evidence.
``rr.file``
    The test source the case came from (``TestCase.file``).
``rr.scope`` / ``rr.synthetic``
    ``rr.scope=target`` marks a result about the whole target run (an exit
    status, a load error, an unreadable report), never a test case;
    ``rr.synthetic=true`` marks a target's single whole-run result.

Only ``level`` and ``artifact.*`` set on a ``<testsuite>`` (or an enclosing
scope) reach its cases. A suite-level ``requirement`` is not inherited: it is
kept in ``TestCase.suite_declared`` and reported as
``suite-level-requirement`` (``Evidence.issues``).
"""

from __future__ import annotations

import glob
import importlib
import os
import re
import warnings
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Mapping

from rules_requirements.util import dedupe

PASSED, SKIPPED, FAILED, ERROR = "passed", "skipped", "failed", "error"
# Most severe wins when cases are merged — also for a whole target: a target
# with skipped cases does not count as a passing verification artifact (the
# skipped part may be exactly the hardware the `verified_by` level claims).
STATUS_ORDER = {PASSED: 0, SKIPPED: 1, FAILED: 2, ERROR: 3}

REQUIREMENT_PROPERTY = "requirement"
LEVEL_PROPERTY = "level"
ARTIFACT_PREFIX = "artifact."
FILE_PROPERTY = "rr.file"
"""The test source a case came from (our writers; else the ``file`` attribute)."""
SYNTHETIC_PROPERTY = "rr.synthetic"
"""``true``: the target's single whole-run result (no per-case output)."""
SCOPE_PROPERTY = "rr.scope"
"""``target``: a result about the whole target run (exit status, load error), not a test case."""


# One "[rr:ID]" (or "[rr:A,B]") name tag and at most one blank before it, so
# "probe [rr:PR-1] ok" and "probe ok" are the same case (case_keys.case_path).
NAME_TAG = re.compile(r"[ \t]?\[rr:([^\]]*)\]")
_ID_SEPARATORS = re.compile(r"[,\s]+")


def split_ids(value: str) -> list[str]:
    """The ids in one requirement value: split on commas and whitespace, blanks dropped."""
    return [part for part in _ID_SEPARATORS.split(value or "") if part]


def name_tags(name: str) -> list[str]:
    """Ids declared by ``[rr:ID]`` tags in a case name, in order (``[rr:A,B]`` gives two)."""
    return dedupe([rid for tag in NAME_TAG.findall(name or "") for rid in split_ids(tag)])


_RUNFILES = re.compile(r"^.*?\.runfiles/[^/]+/")
_BAZEL_OUT = re.compile(r"^(?:.*/)?bazel-out/[^/]+/bin/")


def workspace_relative(path: str) -> str:
    """Strip a ``*.runfiles/<workspace>/`` or ``bazel-out/<cfg>/bin/`` prefix."""
    norm = (path or "").replace("\\", "/")
    for rx in (_RUNFILES, _BAZEL_OUT):
        stripped = rx.sub("", norm, count=1)
        if stripped != norm:
            return stripped
    return norm


def _ids(value: Any) -> tuple[str, ...]:
    """Normalize declared ids, however they were given: a string or any
    iterable of them, each split on commas and whitespace (:func:`split_ids`),
    blanks dropped, duplicates removed, order kept. The one place ids enter a
    :class:`TestCase` (constructor, ``declared``/``suite_declared``
    assignment, the ``requirements`` alias), so ``"PR-1, PR-2"`` is always
    two ids, never one id that happens to contain a comma."""
    if value is None:
        return ()
    items = [value] if isinstance(value, str) else list(value)
    return tuple(dedupe([rid for item in items for rid in split_ids(str(item))]))


_ID_FIELDS = frozenset(("declared", "suite_declared"))

_ALIAS_WARNING = (
    "TestCase.requirements is deprecated: use TestCase.declared. A case's declared ids are tags, "
    "not owners; rules_requirements.attribution decides which requirement a case verifies"
)


@dataclass(init=False)
class TestCase:
    """One raw result, as an ingestor read it.

    ``declared`` holds the requirement ids the evidence *names* for this case
    — tags, in order, without duplicates. It is plain data: nothing here
    says which requirement the case verifies (see :mod:`rules_requirements.ingest`).
    ``requirements`` is a deprecated read/write alias of it (and a
    deprecated keyword of the constructor).
    """

    __test__ = False  # not a pytest test class

    name: str
    status: str  # passed | skipped | failed | error
    classname: str = ""
    declared: tuple[str, ...] = ()  # tag ids, never owners (was ``requirements``)
    level: str = ""
    artifact: dict[str, str] = field(default_factory=dict)
    message: str = ""
    duration: float = 0.0
    source: str = ""  # evidence file it came from
    target: str = ""  # build label (e.g. //pkg:test) if known
    properties: dict[str, str] = field(default_factory=dict)  # everything else
    # Added in v0.2: after ``properties``, so positional construction keeps working.
    suite: str = ""  # name of the enclosing <testsuite>, if any
    # Added in v0.3.
    file: str = ""  # test source, workspace-relative (rr.file, else the file attribute)
    line: int = 0  # line in ``file`` (the line attribute), 0 if unknown
    suite_declared: tuple[str, ...] = ()  # ids an enclosing scope named: NOT inherited

    def __init__(
        self,
        name: str,
        status: str,
        classname: str = "",
        declared: Iterable[str] = (),
        level: str = "",
        artifact: Mapping[str, str] | None = None,
        message: str = "",
        duration: float = 0.0,
        source: str = "",
        target: str = "",
        properties: Mapping[str, str] | None = None,
        suite: str = "",
        file: str = "",
        line: int = 0,
        suite_declared: Iterable[str] = (),
        *,
        requirements: Iterable[str] | None = None,
    ) -> None:
        if requirements is not None:
            warnings.warn(_ALIAS_WARNING, DeprecationWarning, stacklevel=2)
            if _ids(declared):
                raise TypeError("TestCase: pass declared= or the deprecated requirements=, not both")
            declared = requirements
        self.name = name
        self.status = status
        self.classname = classname
        self.declared = _ids(declared)
        self.level = level
        self.artifact = dict(artifact or {})
        self.message = message
        self.duration = duration
        self.source = source
        self.target = target
        self.properties = dict(properties or {})
        self.suite = suite
        self.file = file
        self.line = line
        self.suite_declared = _ids(suite_declared)

    def __setattr__(self, name: str, value: Any) -> None:
        # Every way of setting the ids (a hand-built case, a third-party
        # ingestor assigning ``case.declared = [...]``) is normalized, so a
        # case naming two ids always reads as two (multi-tag downstream).
        if name in _ID_FIELDS:
            value = _ids(value)
        object.__setattr__(self, name, value)

    @property
    def requirements(self) -> tuple[str, ...]:
        """Deprecated alias of ``declared`` (tags, not owners)."""
        warnings.warn(_ALIAS_WARNING, DeprecationWarning, stacklevel=2)
        return self.declared

    @requirements.setter
    def requirements(self, value: Iterable[str]) -> None:
        warnings.warn(_ALIAS_WARNING, DeprecationWarning, stacklevel=2)
        self.declared = _ids(value)

    @property
    def full_name(self) -> str:
        base = f"{self.classname}::{self.name}" if self.classname else self.name
        return f"{self.target} {base}" if self.target else base

    @property
    def is_failure(self) -> bool:
        return self.status in (FAILED, ERROR)

    @property
    def scope(self) -> str:
        """``target`` for a result about the whole target run, else ``case``."""
        return "target" if self.properties.get(SCOPE_PROPERTY, "").lower() == "target" else "case"

    @property
    def synthetic(self) -> bool:
        """The target's single whole-run result (Bazel's generated report, or ``rr.synthetic``)."""
        return self.properties.get(SYNTHETIC_PROPERTY, "").lower() == "true"


def apply_properties(case: TestCase, props: Iterable[tuple[str, str]]) -> TestCase:
    """Fold raw ``(name, value)`` properties into the typed fields of ``case``.

    Every ``requirement``/``requirements`` value is split on commas and
    whitespace, and the distinct ids — together with the case name's
    ``[rr:ID]`` tags — are recorded in ``TestCase.declared``, in order.
    Nothing is decided about ownership: two ids stay two ids.
    """
    reqs = list(case.declared)
    for name, value in props:
        value = (value or "").strip()
        if name in (REQUIREMENT_PROPERTY, "requirements"):
            reqs.extend(split_ids(value))
        elif name == LEVEL_PROPERTY:
            case.level = value.lower()
        elif name.startswith(ARTIFACT_PREFIX):
            case.artifact[name[len(ARTIFACT_PREFIX) :]] = value
        else:
            case.properties[name] = value
    reqs.extend(name_tags(case.name))
    case.declared = tuple(dedupe(reqs))
    if not case.file and case.properties.get(FILE_PROPERTY):
        case.file = workspace_relative(case.properties[FILE_PROPERTY])
    return case


class Ingestor:
    """Base class for evidence readers. Subclasses set ``name`` and implement
    :meth:`sniff` and :meth:`ingest`."""

    name = ""
    suffixes: tuple[str, ...] = ()

    def sniff(self, path: str, head: bytes) -> bool:
        """Whether this ingestor understands ``path`` (``head`` = first 4 KiB)."""
        return path.endswith(self.suffixes) if self.suffixes else False

    def ingest(self, path: str) -> Iterable[TestCase]:
        raise NotImplementedError


_REGISTRY: dict[str, Ingestor] = {}
PLUGIN_ERRORS: list[str] = []  # entry-point ingestors that failed to load
_BUILTINS_LOADED = False


def register(ingestor: Ingestor) -> Ingestor:
    """Register (or replace) an ingestor by its ``name``."""
    if not ingestor.name:
        raise ValueError("an ingestor needs a name")
    _REGISTRY[ingestor.name] = ingestor
    return ingestor


def _load_builtins() -> None:
    global _BUILTINS_LOADED
    if _BUILTINS_LOADED:
        return
    _BUILTINS_LOADED = True
    from rules_requirements.ingest import junit, libtest, records

    for ing in (junit.JUnitIngestor(), records.RecordsIngestor(), libtest.LibtestIngestor()):
        _REGISTRY.setdefault(ing.name, ing)
    try:
        from importlib.metadata import entry_points

        eps = entry_points()
        group: Any = (
            eps.select(group="rules_requirements.ingestors")  # type: ignore[attr-defined]
            if hasattr(eps, "select")
            else eps.get("rules_requirements.ingestors", [])  # type: ignore[attr-defined,arg-type]
        )
    except Exception:  # metadata problems must not break ingestion
        group = []
    for ep in group:
        try:
            obj = ep.load()
            register(obj() if isinstance(obj, type) else obj)
        except Exception as exc:
            PLUGIN_ERRORS.append(f"{ep.name}: {type(exc).__name__}: {exc}")


def load_ingestor(spec: str) -> Ingestor:
    """Import ``module:attr`` (a class or instance) and register it."""
    module, _, attr = spec.partition(":")
    obj = getattr(importlib.import_module(module), attr or "INGESTOR")
    return register(obj() if isinstance(obj, type) else obj)


def ingestors() -> dict[str, Ingestor]:
    _load_builtins()
    return dict(_REGISTRY)


def ingestor_for(path: str, only: Iterable[str] | None = None) -> Ingestor | None:
    """The first registered ingestor that recognises ``path``."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(4096)
    except OSError:
        return None
    allowed = set(only) if only else None
    for name, ing in ingestors().items():
        if allowed is not None and name not in allowed:
            continue
        if ing.sniff(path, head):
            return ing
    return None


@dataclass(frozen=True)
class IngestIssue:
    """Something about the evidence worth a warning (never a verdict).

    ``suite-level-requirement``: a ``<testsuite>`` (or a ``<testcase>``
    holding nested cases) names a requirement. It used to reach every case
    below; since v0.3 it reaches none of them.
    """

    code: str
    source: str
    target: str
    scope: str  # the suite (or parent case) that named the ids
    ids: tuple[str, ...]

    def __str__(self) -> str:
        where = f"{self.source}: " if self.source else ""
        return (
            f"{where}warning: [{self.code}] suite {self.scope or '(unnamed)'} (or a parent case in it) names "
            f"{', '.join(self.ids)} above its test cases; suite-level requirements are not inherited by the "
            "cases (each test case declares its own one id)"
        )


@dataclass
class Evidence:
    """Every test case from every evidence file, plus per-target rollups."""

    cases: list[TestCase] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    skipped_files: list[str] = field(default_factory=list)  # nothing understood them
    target_status: dict[str, str] = field(default_factory=dict)
    issues: list[IngestIssue] = field(default_factory=list)

    def add(self, case: TestCase) -> None:
        self.cases.append(case)
        if case.target:
            cur = self.target_status.get(case.target)
            if cur is None or STATUS_ORDER[case.status] > STATUS_ORDER[cur]:
                self.target_status[case.target] = case.status
        if case.suite_declared:
            issue = IngestIssue(
                "suite-level-requirement",
                case.source,
                case.target,
                case.suite or case.classname,
                case.suite_declared,
            )
            if issue not in self.issues:
                self.issues.append(issue)

    def for_id(self, entity_id: str) -> list[TestCase]:
        """Cases whose evidence *declares* ``entity_id`` — tags, not ownership."""
        return [c for c in self.cases if entity_id in c.declared]


def expand(paths: Iterable[str]) -> list[str]:
    """Files, directories (recursive) and globs -> sorted unique file list."""
    out: list[str] = []
    for path in paths:
        if not path:
            continue
        if os.path.isdir(path):
            for dirpath, dirnames, filenames in os.walk(path, followlinks=True):
                dirnames.sort()
                out.extend(os.path.join(dirpath, n) for n in sorted(filenames))
        elif any(ch in path for ch in "*?["):
            out.extend(sorted(glob.glob(path, recursive=True)))
        elif os.path.isfile(path):
            out.append(path)
    return dedupe(out)


def collect(paths: Iterable[str], only: Iterable[str] | None = None) -> Evidence:
    """Ingest every recognisable evidence file under ``paths``.

    Directories are walked; files no ingestor recognises (e.g. ``test.log``
    next to ``test.xml`` in ``bazel-testlogs``) are skipped silently.
    """
    ev = Evidence()
    only = list(only) if only else None
    for path in expand(paths):
        ing = ingestor_for(path, only)
        if ing is None:
            ev.skipped_files.append(path)
            continue
        ev.files.append(path)
        for case in ing.ingest(path):
            ev.add(case)
    return ev


def iter_cases(evidence: Evidence) -> Iterator[TestCase]:
    yield from evidence.cases
