# SPDX-License-Identifier: AGPL-3.0-or-later
"""The requirements model: user needs, requirements, risks, mitigations and
test methods, and the references between them.

The shape follows the design-control and risk-management vocabulary of IEC
62304 (software life cycle), ISO 14971 (risk management) and IEC 60601-1
(programmable electrical medical systems, §14), deliberately kept generic:

* **User need** (``UN``) — what a user must be able to do. *Validated* when the
  requirements that satisfy it are verified (design validation, 21 CFR 820.30(g)).
* **Requirement** (``REQ``) — a verifiable statement the product must meet.
  It ``satisfies`` user needs and/or ``refines`` a parent requirement
  (system -> software decomposition, IEC 62304 §5.2), and names the ``method``
  (test method or verification level) its verification demands.
* **Risk** (``RISK``) — a hazard / hazardous situation / harm chain with an
  estimated ``severity`` and ``likelihood`` (ISO 14971 §5).
* **Mitigation** (``MIT``) — a risk control measure (ISO 14971 §7.1) that
  ``mitigates`` risks and is ``implemented_by`` requirements — so the
  effectiveness of the control is verified exactly like any requirement
  (ISO 14971 §7.2, "verification of risk control measures").
* **Test method** (``TM``) — a named verification procedure at a given rigor
  level (IEC 62304 §5.7.1 "establish tests ... and procedures").

References always point from the more specific object to the more general one
(``REQ.satisfies -> UN``, ``MIT.mitigates -> RISK``, ``MIT.implemented_by ->
REQ``); the reverse views are computed. That keeps a single source of truth
per trace and makes the model safe to edit object-by-object.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Iterator, Mapping

from rules_requirements import config as cfg
from rules_requirements._vendor import yaml
from rules_requirements.config import Config
from rules_requirements.util import natural_key


@dataclass(frozen=True)
class Location:
    path: str = ""
    line: int = 0

    def __str__(self) -> str:
        if not self.path:
            return "<model>"
        return f"{self.path}:{self.line}" if self.line else self.path


@dataclass(frozen=True)
class Note:
    """A free-form annotation on an object: a gap an agent found, a question for
    a reviewer, a TODO that should drive the next implementation cycle."""

    text: str
    kind: str = "comment"  # comment | gap | question | todo
    status: str = "open"  # open | resolved
    author: str = ""
    created: str = ""  # ISO date
    id: str = ""  # stable within its object, e.g. "n1"


NOTE_KINDS = ("comment", "gap", "question", "todo")
NOTE_STATUSES = ("open", "resolved")


@dataclass(frozen=True)
class VerifiedBy:
    """A whole-target verification artifact (e.g. a Bazel test label) and the
    rigor level it provides. Authored as a bare string or ``{target, level}``."""

    target: str
    level: str = ""  # "" -> config.default_provided_level


@dataclass(frozen=True)
class Entity:
    id: str
    title: str
    description: str = ""
    status: str = ""
    owner: str = ""
    tags: tuple[str, ...] = ()
    notes: tuple[Note, ...] = ()
    location: Location = field(default_factory=Location, compare=False)

    kind = ""  # overridden per subclass

    def references(self) -> Iterator[tuple[str, str]]:
        """(relation, target id) for every outgoing reference."""
        return iter(())


@dataclass(frozen=True)
class UserNeed(Entity):
    rationale: str = ""
    kind = cfg.USER_NEED


@dataclass(frozen=True)
class Requirement(Entity):
    rationale: str = ""
    # Free-form category (functional, performance, safety, security, ...).
    category: str = ""
    satisfies: tuple[str, ...] = ()  # UN ids
    refines: tuple[str, ...] = ()  # parent REQ ids
    # Demanded verification: a test method id or a level name ("" -> default).
    method: str = ""
    verified_by: tuple[VerifiedBy, ...] = ()
    # Implementing modules (documentation aid; drives per-module rollups).
    modules: tuple[str, ...] = ()
    kind = cfg.REQUIREMENT

    def references(self) -> Iterator[tuple[str, str]]:
        for un in self.satisfies:
            yield "satisfies", un
        for parent in self.refines:
            yield "refines", parent
        if self.method:
            yield "method", self.method


@dataclass(frozen=True)
class Risk(Entity):
    hazard: str = ""
    hazardous_situation: str = ""
    harm: str = ""
    severity: str = ""
    likelihood: str = ""
    # Estimated after mitigation (ISO 14971 §7.3). Empty -> not yet estimated.
    residual_severity: str = ""
    residual_likelihood: str = ""
    residual: str = ""  # free-form residual-risk note / acceptance rationale
    # Optional back-reference; if present it must agree with MIT.mitigates.
    mitigated_by: tuple[str, ...] = ()
    kind = cfg.RISK

    def references(self) -> Iterator[tuple[str, str]]:
        for mit in self.mitigated_by:
            yield "mitigated_by", mit


@dataclass(frozen=True)
class Mitigation(Entity):
    # ISO 14971 §7.1 option analysis: inherent safety by design, protective
    # measures, or information for safety.
    type: str = ""
    mitigates: tuple[str, ...] = ()  # RISK ids
    implemented_by: tuple[str, ...] = ()  # REQ ids
    kind = cfg.MITIGATION

    def references(self) -> Iterator[tuple[str, str]]:
        for risk in self.mitigates:
            yield "mitigates", risk
        for req in self.implemented_by:
            yield "implemented_by", req


@dataclass(frozen=True)
class TestMethod(Entity):
    level: str = ""  # rigor this method provides/demands
    procedure: str = ""
    kind = cfg.TEST_METHOD


ENTITY_CLASSES: dict[str, type[Entity]] = {
    cfg.USER_NEED: UserNeed,
    cfg.REQUIREMENT: Requirement,
    cfg.RISK: Risk,
    cfg.MITIGATION: Mitigation,
    cfg.TEST_METHOD: TestMethod,
}

# Allowed keys per kind (besides the common ones). Unknown keys are reported by
# the ``unknown-field`` rule because a misspelt ``satisfes:`` silently drops a
# trace otherwise.
_COMMON_FIELDS = ("id", "title", "description", "status", "owner", "tags", "notes")
FIELDS = {
    cfg.USER_NEED: _COMMON_FIELDS + ("rationale",),
    cfg.REQUIREMENT: _COMMON_FIELDS
    + ("rationale", "category", "satisfies", "refines", "method", "verified_by", "modules"),
    cfg.RISK: _COMMON_FIELDS
    + (
        "hazard",
        "hazardous_situation",
        "harm",
        "severity",
        "likelihood",
        "residual_severity",
        "residual_likelihood",
        "residual",
        "mitigated_by",
    ),
    cfg.MITIGATION: _COMMON_FIELDS + ("type", "mitigates", "implemented_by"),
    cfg.TEST_METHOD: _COMMON_FIELDS + ("level", "procedure"),
}


@dataclass(frozen=True)
class Model:
    config: Config = field(default_factory=Config)
    project: Mapping[str, Any] = field(default_factory=dict)
    user_needs: Mapping[str, UserNeed] = field(default_factory=dict)
    requirements: Mapping[str, Requirement] = field(default_factory=dict)
    risks: Mapping[str, Risk] = field(default_factory=dict)
    mitigations: Mapping[str, Mitigation] = field(default_factory=dict)
    test_methods: Mapping[str, TestMethod] = field(default_factory=dict)
    # Problems found while *parsing* (shape); validation adds referential ones.
    parse_errors: tuple[str, ...] = ()
    # Unknown keys, reported by validation under the ``unknown-field`` rule.
    unknown_fields: tuple[str, ...] = ()

    # --- lookup ---------------------------------------------------------------

    def section(self, kind: str) -> Mapping[str, Entity]:
        return getattr(self, cfg.SECTIONS[kind])  # type: ignore[no-any-return]

    def get(self, entity_id: str) -> Entity | None:
        for kind in cfg.KINDS:
            ent = self.section(kind).get(entity_id)
            if ent is not None:
                return ent
        return None

    def entities(self) -> Iterator[Entity]:
        for kind in cfg.KINDS:
            yield from sorted(self.section(kind).values(), key=lambda e: natural_key(e.id))

    def ids(self) -> set[str]:
        return {e.id for e in self.entities()}

    # --- reverse views --------------------------------------------------------

    def requirements_for_need(self, un_id: str) -> list[Requirement]:
        return _sorted(r for r in self.requirements.values() if un_id in r.satisfies)

    def children(self, req_id: str) -> list[Requirement]:
        return _sorted(r for r in self.requirements.values() if req_id in r.refines)

    def mitigations_for_risk(self, risk_id: str) -> list[Mitigation]:
        return _sorted(m for m in self.mitigations.values() if risk_id in m.mitigates)

    def mitigations_implemented_by(self, req_id: str) -> list[Mitigation]:
        return _sorted(m for m in self.mitigations.values() if req_id in m.implemented_by)

    def requirements_for_risk(self, risk_id: str) -> list[Requirement]:
        """Requirements that implement any mitigation of ``risk_id``."""
        ids: set[str] = set()
        for mit in self.mitigations_for_risk(risk_id):
            ids.update(mit.implemented_by)
        return _sorted(self.requirements[i] for i in ids if i in self.requirements)

    def requirements_for_method(self, tm_id: str) -> list[Requirement]:
        return _sorted(r for r in self.requirements.values() if r.method == tm_id)

    def demanded_level(self, req: Requirement) -> str:
        """The level a requirement's verification demands.

        ``method`` may name a test method (whose ``level`` then applies) or a
        level directly; empty falls back to ``config.default_level``.
        """
        method = req.method.strip()
        if not method:
            return self.config.default_level
        tm = self.test_methods.get(method)
        if tm is not None:
            return tm.level or self.config.default_level
        return method.lower()

    def modules(self) -> list[str]:
        mods: set[str] = set()
        for req in self.requirements.values():
            mods.update(req.modules)
        return sorted(mods)

    def with_entity(self, entity: Entity) -> Model:
        """A copy of the model with ``entity`` added or replaced."""
        section = dict(self.section(entity.kind))
        section[entity.id] = entity
        return replace(self, **{cfg.SECTIONS[entity.kind]: section})  # type: ignore[arg-type]

    def without_entity(self, entity_id: str) -> Model:
        ent = self.get(entity_id)
        if ent is None:
            return self
        section = {k: v for k, v in self.section(ent.kind).items() if k != entity_id}
        return replace(self, **{cfg.SECTIONS[ent.kind]: section})  # type: ignore[arg-type]


def _sorted(items: Iterable[Any]) -> list[Any]:
    return sorted(items, key=lambda e: natural_key(e.id))


# --------------------------------------------------------------------------- #
# Loading                                                                     #
# --------------------------------------------------------------------------- #


class _LineDict(dict):  # type: ignore[type-arg]
    """A mapping that remembers the 1-based line it started on."""

    line = 0


class _LineLoader(yaml.SafeLoader):  # type: ignore[misc]
    pass


_MERGE_TAG = "tag:yaml.org,2002:merge"


def _construct_mapping(loader: _LineLoader, node: yaml.MappingNode) -> _LineDict:
    # A repeated key silently replaces the first value in plain YAML loaders,
    # which here would drop whole sections or weaken a demanded method. Check
    # the explicitly written keys — merge-key contributions may legitimately
    # be overridden — before merges are flattened in.
    seen: dict[Any, Any] = {}
    for knode, _ in node.value:
        if knode.tag == _MERGE_TAG:
            continue
        key = loader.construct_object(knode, deep=True)
        try:
            first = seen.get(key)
        except TypeError:  # unhashable key: not a model field anyway
            continue
        if first is not None:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping", first, f"found duplicate key {key!r}", knode.start_mark
            )
        seen[key] = knode.start_mark
    loader.flatten_mapping(node)
    out = _LineDict(loader.construct_pairs(node, deep=True))
    out.line = node.start_mark.line + 1
    return out


_LineLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def _load_documents(path: str) -> list[Any]:
    with open(path, encoding="utf-8") as fh:
        return [d for d in yaml.load_all(fh, Loader=_LineLoader) if d is not None]


def model_files(paths: str | Iterable[str]) -> list[str]:
    """Expand files and directories into the sorted list of model files."""
    if isinstance(paths, str):
        paths = [paths]
    out: list[str] = []
    for path in paths:
        if os.path.isdir(path):
            found = []
            for dirpath, dirnames, filenames in os.walk(path):
                dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
                for name in filenames:
                    if name.endswith((".yaml", ".yml", ".json")):
                        found.append(os.path.join(dirpath, name))
            out.extend(sorted(found))
        else:
            out.append(path)
    return out


def _as_tuple(value: Any) -> tuple[str, ...]:
    if value is None or value == "":
        return ()
    if isinstance(value, str):
        return tuple(v.strip() for v in value.split(",") if v.strip())
    if isinstance(value, (list, tuple)):
        return tuple(str(v).strip() for v in value if str(v).strip())
    return (str(value),)


def _parse_notes(raw: Any, where: str, errors: list[str]) -> tuple[Note, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        errors.append(f"{where}: notes must be a list")
        return ()
    notes = []
    for i, item in enumerate(raw):
        if isinstance(item, str):
            item = {"text": item}
        if not isinstance(item, Mapping) or not item.get("text"):
            errors.append(f"{where}: notes[{i}] needs a 'text'")
            continue
        kind = str(item.get("kind", "comment"))
        status = str(item.get("status", "open"))
        if kind not in NOTE_KINDS:
            errors.append(f"{where}: notes[{i}].kind must be one of {NOTE_KINDS}")
        if status not in NOTE_STATUSES:
            errors.append(f"{where}: notes[{i}].status must be one of {NOTE_STATUSES}")
        notes.append(
            Note(
                text=str(item["text"]),
                kind=kind,
                status=status,
                author=str(item.get("author", "")),
                created=str(item.get("created", "")),
                id=str(item.get("id", "") or f"n{i + 1}"),
            )
        )
    return tuple(notes)


def _parse_verified_by(raw: Any, where: str, errors: list[str]) -> tuple[VerifiedBy, ...]:
    if raw is None:
        return ()
    items = raw if isinstance(raw, list) else [raw]
    out = []
    for item in items:
        if isinstance(item, str):
            out.append(VerifiedBy(target=item))
        elif isinstance(item, Mapping) and item.get("target"):
            out.append(VerifiedBy(target=str(item["target"]), level=str(item.get("level", ""))))
        else:
            errors.append(f"{where}: verified_by items must be a label or {{target, level}}")
    return tuple(out)


def parse_entity(
    kind: str, raw: Mapping[str, Any], location: Location, errors: list[str], unknown: list[str]
) -> Entity | None:
    """Build one entity from its YAML mapping. Shape errors go to ``errors``,
    unknown keys to ``unknown`` (reported under the ``unknown-field`` rule)."""
    where = f"{location}"
    if not isinstance(raw, Mapping):
        errors.append(f"{where}: {cfg.SECTIONS[kind]} entries must be mappings")
        return None
    ident = str(raw.get("id", "") or "").strip()
    where = f"{location}: {ident or '<no id>'}"
    for key in ("id", "title"):
        if not str(raw.get(key, "") or "").strip():
            errors.append(f"{where}: missing required field '{key}'")
    if not ident:
        return None
    for key in raw:
        if key not in FIELDS[kind]:
            unknown.append(f"{where}: unknown field {key!r}")

    common: dict[str, Any] = dict(
        id=ident,
        title=str(raw.get("title", "") or "").strip(),
        description=str(raw.get("description", "") or "").strip(),
        status=str(raw.get("status", "") or "").strip(),
        owner=str(raw.get("owner", "") or "").strip(),
        tags=_as_tuple(raw.get("tags")),
        notes=_parse_notes(raw.get("notes"), where, errors),
        location=location,
    )

    def text(key: str) -> str:
        return str(raw.get(key, "") or "").strip()

    if kind == cfg.USER_NEED:
        return UserNeed(**common, rationale=text("rationale"))
    if kind == cfg.REQUIREMENT:
        return Requirement(
            **common,
            rationale=text("rationale"),
            category=text("category"),
            satisfies=_as_tuple(raw.get("satisfies")),
            refines=_as_tuple(raw.get("refines")),
            method=text("method"),
            verified_by=_parse_verified_by(raw.get("verified_by"), where, errors),
            modules=_as_tuple(raw.get("modules")),
        )
    if kind == cfg.RISK:
        return Risk(
            **common,
            hazard=text("hazard"),
            hazardous_situation=text("hazardous_situation"),
            harm=text("harm"),
            severity=text("severity").lower(),
            likelihood=text("likelihood").lower(),
            residual_severity=text("residual_severity").lower(),
            residual_likelihood=text("residual_likelihood").lower(),
            residual=text("residual"),
            mitigated_by=_as_tuple(raw.get("mitigated_by")),
        )
    if kind == cfg.MITIGATION:
        return Mitigation(
            **common,
            type=text("type").lower(),
            mitigates=_as_tuple(raw.get("mitigates")),
            implemented_by=_as_tuple(raw.get("implemented_by")),
        )
    return TestMethod(**common, level=text("level").lower(), procedure=text("procedure"))


def parse_documents(docs: Iterable[tuple[str, Any]]) -> tuple[Model, list[str]]:
    """Merge ``(path, document)`` pairs into one model.

    Returns the model and a list of extra warnings (currently always empty;
    unknown keys are recorded on ``model.unknown_fields`` for validation).

    A document is either a *section document* (a mapping with any of
    ``config``, ``project``, ``user_needs``, ``requirements``, ``risks``,
    ``mitigations``, ``test_methods``) or a *single-object document* (a mapping
    with ``kind: requirement`` etc. plus the object's fields) — the layout the
    web editor writes, one object per file.
    """
    errors: list[str] = []
    unknown: list[str] = []
    docs = list(docs)

    raw_config = None
    project: dict[str, Any] = {}
    for path, doc in docs:
        if isinstance(doc, Mapping) and "config" in doc:
            if raw_config is not None:
                errors.append(f"{path}: 'config' is defined more than once")
            raw_config = doc["config"]
        if isinstance(doc, Mapping):
            for key in ("project", "meta"):
                if isinstance(doc.get(key), Mapping):
                    project.update(doc[key])
    config = cfg.parse_config(raw_config, errors)

    sections: dict[str, dict[str, Entity]] = {k: {} for k in cfg.KINDS}
    seen: dict[str, Location] = {}

    def add(kind: str, raw: Any, path: str) -> None:
        loc = Location(path, getattr(raw, "line", 0))
        ent = parse_entity(kind, raw, loc, errors, unknown)
        if ent is None:
            return
        if ent.id in seen:
            errors.append(f"{loc}: duplicate id {ent.id} (first defined at {seen[ent.id]})")
            return
        seen[ent.id] = loc
        sections[kind][ent.id] = ent

    top_keys = {"config", "project", "meta", "schema_version", "kind", *cfg.KIND_BY_SECTION}
    for path, doc in docs:
        if not isinstance(doc, Mapping):
            errors.append(f"{path}: top-level document must be a mapping")
            continue
        if "kind" in doc:
            kind = cfg.KIND_BY_SECTION.get(str(doc["kind"]), str(doc["kind"]))
            if kind not in cfg.KINDS:
                errors.append(f"{path}: unknown kind {doc['kind']!r}")
                continue
            body = _LineDict({k: v for k, v in doc.items() if k != "kind"})
            body.line = getattr(doc, "line", 0)
            add(kind, body, path)
            continue
        for key in doc:
            if key not in top_keys:
                unknown.append(f"{path}: unknown top-level key {key!r}")
        for section, kind in cfg.KIND_BY_SECTION.items():
            items = doc.get(section)
            if items is None:
                continue
            if not isinstance(items, list):
                errors.append(f"{path}: '{section}' must be a list")
                continue
            for raw in items:
                add(kind, raw, path)

    return Model(
        config=config,
        project=project,
        user_needs=sections[cfg.USER_NEED],  # type: ignore[arg-type]
        requirements=sections[cfg.REQUIREMENT],  # type: ignore[arg-type]
        risks=sections[cfg.RISK],  # type: ignore[arg-type]
        mitigations=sections[cfg.MITIGATION],  # type: ignore[arg-type]
        test_methods=sections[cfg.TEST_METHOD],  # type: ignore[arg-type]
        parse_errors=tuple(errors),
        unknown_fields=tuple(unknown),
    ), []


def read_model(paths: str | Iterable[str], root: str = "") -> tuple[Model, list[str]]:
    """Read model files without raising; returns (model, extra warnings).

    Unknown fields are kept on ``model.unknown_fields`` and reported by
    :func:`~rules_requirements.validate.validate` like any other issue.

    ``root`` makes recorded source paths relative (stable across machines).
    """
    docs: list[tuple[str, Any]] = []
    load_errors: list[str] = []
    for path in model_files(paths):
        shown = os.path.relpath(path, root) if root else path
        try:
            for doc in _load_documents(path):
                docs.append((shown, doc))
        except (OSError, yaml.YAMLError) as exc:
            load_errors.append(f"{shown}: cannot load: {exc}")
    model, warnings = parse_documents(docs)
    if load_errors:
        model = replace(model, parse_errors=tuple(load_errors) + model.parse_errors)
    return model, warnings


def load_model(paths: str | Iterable[str], root: str = "") -> Model:
    """Load and fully validate the model; raise :class:`ValidationError` on errors."""
    from rules_requirements.validate import ValidationError, validate

    model, _ = read_model(paths, root=root)
    issues = validate(model)
    errors = [i for i in issues if i.severity == "error"]
    if errors:
        raise ValidationError(errors)
    return model
