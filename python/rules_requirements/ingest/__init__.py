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
    An entity id the case verifies (repeatable; comma-separated lists are
    accepted, as is the plural ``requirements``).
``level``
    The verification rigor the case provides (e.g. ``hil``).
``artifact.<key>``
    Identity of the thing under test (firmware build id, DUT git SHA, ...)
    used to detect stale evidence.
"""

from __future__ import annotations

import glob
import importlib
import os
from dataclasses import dataclass, field
from typing import Iterable, Iterator

from rules_requirements.util import dedupe

PASSED, SKIPPED, FAILED, ERROR = "passed", "skipped", "failed", "error"
# Most severe wins when a target's cases are folded into one status.
STATUS_ORDER = {PASSED: 0, SKIPPED: 1, FAILED: 2, ERROR: 3}

REQUIREMENT_PROPERTY = "requirement"
LEVEL_PROPERTY = "level"
ARTIFACT_PREFIX = "artifact."


@dataclass
class TestCase:
    __test__ = False  # not a pytest test class

    name: str
    status: str  # passed | skipped | failed | error
    classname: str = ""
    requirements: tuple[str, ...] = ()
    level: str = ""
    artifact: dict[str, str] = field(default_factory=dict)
    message: str = ""
    duration: float = 0.0
    source: str = ""  # evidence file it came from
    target: str = ""  # build label (e.g. //pkg:test) if known
    properties: dict[str, str] = field(default_factory=dict)  # everything else

    @property
    def full_name(self) -> str:
        base = f"{self.classname}::{self.name}" if self.classname else self.name
        return f"{self.target} {base}" if self.target else base

    @property
    def is_failure(self) -> bool:
        return self.status in (FAILED, ERROR)


def apply_properties(case: TestCase, props: Iterable[tuple[str, str]]) -> TestCase:
    """Fold raw ``(name, value)`` properties into the typed fields of ``case``."""
    reqs = list(case.requirements)
    for name, value in props:
        value = (value or "").strip()
        if name in (REQUIREMENT_PROPERTY, "requirements"):
            reqs.extend(v.strip() for v in value.split(","))
        elif name == LEVEL_PROPERTY:
            case.level = value.lower()
        elif name.startswith(ARTIFACT_PREFIX):
            case.artifact[name[len(ARTIFACT_PREFIX) :]] = value
        else:
            case.properties[name] = value
    case.requirements = tuple(dedupe(reqs))
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
        group = (
            eps.select(group="rules_requirements.ingestors")  # type: ignore[attr-defined]
            if hasattr(eps, "select")
            else eps.get("rules_requirements.ingestors", [])  # type: ignore[attr-defined,arg-type]
        )
        for ep in group:
            obj = ep.load()
            register(obj() if isinstance(obj, type) else obj)
    except Exception:  # noqa: S110 — a broken third-party plugin must not break ingestion
        pass


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


@dataclass
class Evidence:
    """Every test case from every evidence file, plus per-target rollups."""

    cases: list[TestCase] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    skipped_files: list[str] = field(default_factory=list)  # nothing understood them
    target_status: dict[str, str] = field(default_factory=dict)

    def add(self, case: TestCase) -> None:
        self.cases.append(case)
        if case.target:
            cur = self.target_status.get(case.target)
            if cur is None or STATUS_ORDER[case.status] > STATUS_ORDER[cur]:
                self.target_status[case.target] = case.status

    def for_id(self, entity_id: str) -> list[TestCase]:
        return [c for c in self.cases if entity_id in c.requirements]


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
