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
from types import MappingProxyType
from typing import Any, Iterable, Iterator, Mapping

from rules_requirements import case_selectors, labels
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
    # Keys this model does not define, kept so that rewriting a note list never
    # loses them (they are also reported under the ``unknown-field`` rule).
    extra: tuple[tuple[str, Any], ...] = ()


NOTE_KINDS = ("comment", "gap", "question", "todo")
NOTE_STATUSES = ("open", "resolved")


@dataclass(frozen=True)
class VerifiedBy:
    """One item of ``verified_by`` (requirements, mitigations) or
    ``validated_by`` (user needs): the cases of one target an entity claims.

    Authored as ``{target, cases: [selector, ...]}`` (selectors:
    :mod:`~rules_requirements.case_selectors`) or ``{target, whole: true,
    reason}`` for a target that reports no per-case results. The legacy forms
    — a bare label, ``{target}`` or ``{target, level}`` — read as a whole
    claim with ``legacy`` set (rule ``bare-target-reference``).

    A claim only *claims*: which entity owns a case is decided by attribution
    alone, and claims of two entities that can select one case are a
    ``shared-case`` error.
    """

    target: str  # normalized (labels.normalize_label); as written if it is not a label
    cases: tuple[str, ...] = ()  # selectors; () iff whole (unless ``problem``)
    whole: bool = False
    level: str = ""  # "" -> config.default_provided_level; applies to cases declaring none
    reason: str = ""  # why a whole-target claim cannot be per-case
    legacy: bool = False  # authored as a bare label, {target} or {target, level}
    extra: tuple[tuple[str, Any], ...] = ()  # unknown keys, kept for round-trips
    # The target as written, kept so that rewriting an item never respells it.
    spelling: str = ""
    # A shape problem (``bad-selector``): both or neither of cases/whole, a
    # non-list ``cases``, ``whole`` not true. Such an item claims the whole
    # target, so it can only add conflicts, never hide one.
    problem: str = ""
    location: Location = field(default_factory=Location, compare=False)
    # The item exactly as authored (plain data), kept when ``problem`` is set:
    # the fields above cannot hold what was wrong with it, and an editor that
    # rewrites the entity must neither lose nor "repair" it.
    authored: Any = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        if isinstance(self.cases, str):
            # VerifiedBy("//p:hw", "hil") is the 0.2 field order (target, level):
            # it would claim the selectors "h", "i" and "l" without a word.
            raise TypeError(
                f"VerifiedBy({self.target!r}, {self.cases!r}): cases is a tuple of selectors, not a string; "
                "pass the fields after target by keyword (level=..., cases=(...,))"
            )

    @property
    def label(self) -> str:
        """The target as written."""
        return self.spelling or self.target

    @property
    def selectors(self) -> tuple[str | None, ...]:
        """The item's selectors; ``(None,)`` for a whole-target claim."""
        if self.whole or self.problem or not self.cases:
            return (None,)
        return self.cases


@dataclass(frozen=True)
class Claim:
    """One selector of one entity: the unit the ``shared-case`` check and
    attribution work on. ``pattern`` is ``None`` for a whole-target claim."""

    entity: str
    kind: str
    target: str  # normalized
    pattern: str | None
    literal: bool  # a selector without an unescaped '*' (False for whole and bad selectors)
    level: str
    index: int  # the item's position in the entity's verified_by / validated_by
    location: Location = field(default_factory=Location, compare=False)
    legacy: bool = False
    relation: str = "verified_by"

    @property
    def whole(self) -> bool:
        return self.pattern is None

    def describe(self) -> str:
        """The selector as a message shows it."""
        if self.pattern is None:
            return "the legacy whole-target reference" if self.legacy else "the whole target"
        return repr(self.pattern)

    def matches(self, path: str) -> bool:
        """Whether this claim selects case ``path`` of its target (a whole
        claim selects every path; a selector never selects ``[target]``)."""
        if self.pattern is None:
            return True
        if path == case_selectors.SYNTHETIC_PATH:
            return False
        try:
            return case_selectors.matches(self.pattern, path)
        except case_selectors.BadSelector:
            return False


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
    # Validation evidence (usability studies, acceptance runs): the same claim
    # namespace as requirements' verified_by.
    validated_by: tuple[VerifiedBy, ...] = ()
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
    # Effectiveness evidence of the control (ISO 14971 §7.2), in the same
    # claim namespace as requirements' verified_by.
    verified_by: tuple[VerifiedBy, ...] = ()
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
    cfg.USER_NEED: _COMMON_FIELDS + ("rationale", "validated_by"),
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
    cfg.MITIGATION: _COMMON_FIELDS + ("type", "mitigates", "implemented_by", "verified_by"),
    cfg.TEST_METHOD: _COMMON_FIELDS + ("level", "procedure"),
}

# The field holding each verifiable kind's claims. Risks and test methods
# hold none (the keys are unknown fields there).
CLAIM_FIELDS = {
    cfg.USER_NEED: "validated_by",
    cfg.REQUIREMENT: "verified_by",
    cfg.MITIGATION: "verified_by",
}
VERIFIABLE_KINDS = tuple(CLAIM_FIELDS)


def claim_items(ent: Entity) -> tuple[VerifiedBy, ...]:
    """The ``verified_by`` / ``validated_by`` items of ``ent`` (() for kinds
    that cannot claim cases)."""
    name = CLAIM_FIELDS.get(ent.kind)
    return getattr(ent, name) if name else ()  # type: ignore[no-any-return]


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
    # The file holding the ``config:`` section ("" if none), as locations show
    # it (relative to ``root`` when the model was read with one): the
    # ``sets_lock`` path is relative to it.
    config_file: str = field(default="", compare=False)
    root: str = field(default="", compare=False)

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

    def is_verifiable(self, entity_id: str) -> bool:
        """Whether ``entity_id`` is a user need, requirement or mitigation —
        an entity that can own test cases."""
        ent = self.get(entity_id)
        return ent is not None and ent.kind in CLAIM_FIELDS

    def claims(self) -> list[Claim]:
        """Every claim of every entity, in a stable order: user needs,
        requirements, mitigations (by id), then item and selector order."""
        out: list[Claim] = []
        for kind in VERIFIABLE_KINDS:
            relation = CLAIM_FIELDS[kind]
            for ent in _sorted(self.section(kind).values()):
                for index, vb in enumerate(claim_items(ent)):
                    for pattern in vb.selectors:
                        literal = False
                        if pattern is not None:
                            try:
                                literal = case_selectors.is_literal(pattern)
                            except case_selectors.BadSelector:
                                literal = False
                        out.append(
                            Claim(
                                entity=ent.id,
                                kind=kind,
                                target=vb.target,
                                pattern=pattern,
                                literal=literal,
                                level=vb.level,
                                index=index,
                                location=vb.location if vb.location.path else ent.location,
                                legacy=vb.legacy,
                                relation=relation,
                            )
                        )
        return out

    def lock_path(self, shown: bool = False) -> str:
        """Where ``config.sets_lock`` points ("" without one): a path to open,
        or with ``shown`` the path as locations show it."""
        lock = self.config.sets_lock
        if not lock:
            return ""
        if not os.path.isabs(lock):
            lock = os.path.normpath(os.path.join(os.path.dirname(self.config_file), lock))
        elif shown and self.root and lock.startswith(self.root.rstrip(os.sep) + os.sep):
            # An absolute lock below the root (`--sets-lock` makes it absolute)
            # shows relative to it, as model locations do: stable across machines.
            return os.path.relpath(lock, self.root)
        if shown or os.path.isabs(lock):
            return lock
        return os.path.join(self.root, lock)

    def modules(self) -> list[str]:
        mods: set[str] = set()
        for req in self.requirements.values():
            mods.update(req.modules)
        return sorted(mods)

    def with_entity(self, entity: Entity) -> Model:
        """A copy of the model with ``entity`` added or replaced (in its own
        section). Raises ValueError when its id names an entity of another
        kind: one id names exactly one entity."""
        other = self.get(entity.id)
        if other is not None and other.kind != entity.kind:
            raise ValueError(
                f"{entity.id} is already a {other.kind.replace('_', ' ')}; it cannot also be a "
                f"{entity.kind.replace('_', ' ')} (remove it first: without_entity)"
            )
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
    """A mapping that remembers the 1-based line it started on, and the line
    of each key written in it (``key_lines``; merged-in keys are absent)."""

    line = 0
    key_lines: Mapping[Any, int] = MappingProxyType({})


class _LineLoader(yaml.SafeLoader):  # type: ignore[misc]
    # Aliases are fine, but not a document that expands into an absurd number
    # of values through them ("billion laughs"): everything downstream walks
    # the expanded values.
    MAX_EXPANDED_VALUES = 1_000_000

    def construct_document(self, node: Any) -> Any:
        if _expanded_size(node, {}) > self.MAX_EXPANDED_VALUES:
            raise yaml.constructor.ConstructorError(
                None,
                None,
                f"the document expands to over {self.MAX_EXPANDED_VALUES:,} values through aliases",
                node.start_mark,
            )
        return super().construct_document(node)


def _expanded_size(node: Any, memo: dict[int, int]) -> int:
    """How many nodes ``node`` stands for once aliases are expanded (linear:
    each distinct node is sized once; a recursive alias counts once)."""
    key = id(node)
    if key in memo:
        return memo[key]
    memo[key] = 1
    size = 1
    if isinstance(node, yaml.SequenceNode):
        size += sum(_expanded_size(child, memo) for child in node.value)
    elif isinstance(node, yaml.MappingNode):
        size += sum(_expanded_size(k, memo) + _expanded_size(v, memo) for k, v in node.value)
    memo[key] = size
    return size


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
        except TypeError:  # a complex key (`? [a, b]`) cannot be a dict key
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping", node.start_mark, "found unhashable key", knode.start_mark
            ) from None
        if first is not None:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping", first, f"found duplicate key {key!r}", knode.start_mark
            )
        seen[key] = knode.start_mark
    loader.flatten_mapping(node)
    out = _LineDict(loader.construct_pairs(node, deep=True))
    out.line = node.start_mark.line + 1
    out.key_lines = {key: mark.line + 1 for key, mark in seen.items()}
    return out


_LineLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def _load_documents(path: str) -> list[Any]:
    with open(path, encoding="utf-8") as fh:
        return load_text(fh.read())


def load_text(text: str) -> list[Any]:
    """The documents of one model file's text, loaded the way model files are
    (line numbers, duplicate-key check, alias expansion limit)."""
    return [d for d in yaml.load_all(text, Loader=_LineLoader) if d is not None]


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


_NOTE_KEYS = ("id", "text", "kind", "status", "author", "created")


def _parse_notes(raw: Any, where: str, errors: list[str], unknown: list[str] | None = None) -> tuple[Note, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        errors.append(f"{where}: notes must be a list")
        return ()
    # Explicit ids are kept; a note without one gets the first free "n<k>",
    # never one another note of the same entity already uses.
    explicit = [str(item.get("id", "")) for item in raw if isinstance(item, Mapping) and item.get("id")]
    for dup in sorted({i for i in explicit if explicit.count(i) > 1}):
        errors.append(f"{where}: note id {dup!r} is used more than once")
    used = set(explicit)
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
        extra = tuple((str(k), v) for k, v in item.items() if k not in _NOTE_KEYS)
        for key, _ in extra:
            if unknown is not None:
                unknown.append(f"{where}: notes[{i}]: unknown field {key!r}")
        nid = str(item.get("id", "") or "")
        if not nid:
            k = i + 1
            while f"n{k}" in used:
                k += 1
            nid = f"n{k}"
            used.add(nid)
        notes.append(
            Note(
                text=str(item["text"]),
                kind=kind,
                status=status,
                author=str(item.get("author", "")),
                created=str(item.get("created", "")),
                id=nid,
                extra=extra,
            )
        )
    return tuple(notes)


_ITEM_KEYS = ("target", "cases", "whole", "level", "reason")


def _parse_verified_by(
    raw: Any,
    where: str,
    errors: list[str],
    unknown: list[str] | None = None,
    main_repo: str = "",
    relation: str = "verified_by",
    location: Location | None = None,
) -> tuple[VerifiedBy, ...]:
    """The items of a ``verified_by`` / ``validated_by`` list.

    Shape problems of one item (``bad-selector``) and labels that do not
    normalize (``bad-target``) are left to validation, which reports them with
    their own codes; only an item that names no target at all is a parse
    error.
    """
    if raw is None:
        return ()
    base = location or Location()
    items = raw if isinstance(raw, list) else [raw]
    out = []
    for i, item in enumerate(items):
        if isinstance(item, str) and item.strip():
            out.append(_item(item, main_repo, location=base))
        elif isinstance(item, Mapping) and isinstance(item.get("target"), str) and item["target"].strip():
            extra = tuple((str(k), v) for k, v in item.items() if k not in _ITEM_KEYS)
            for key, _ in extra:
                if unknown is not None:
                    unknown.append(f"{where}: {relation}[{i}]: unknown field {key!r}")
            loc = Location(base.path, getattr(item, "line", 0) or base.line)
            out.append(_item(item["target"], main_repo, item=item, extra=extra, location=loc))
        else:
            errors.append(
                f"{where}: {relation} items must be a label or a mapping with a 'target' "
                "and either 'cases' (a list of case selectors) or 'whole: true'"
            )
    return tuple(out)


def _item(
    target: str,
    main_repo: str,
    item: Mapping[str, Any] | None = None,
    extra: tuple[tuple[str, Any], ...] = (),
    location: Location | None = None,
) -> VerifiedBy:
    spelling = target.strip()
    norm = labels.try_normalize(spelling, main_repo) or spelling
    if item is None:
        return VerifiedBy(target=norm, whole=True, legacy=True, spelling=spelling, location=location or Location())
    level = str(item.get("level", "") or "").strip()
    reason = str(item.get("reason", "") or "").strip()
    has_cases, has_whole = "cases" in item, "whole" in item
    legacy = False
    cases: tuple[str, ...] = ()
    whole = False
    problem = ""
    if has_whole:
        whole = item["whole"] is True
        if not whole:
            problem = "whole must be true (or leave it out and list cases)"
    if has_cases:
        raw_cases = item["cases"]
        if not isinstance(raw_cases, list) or not all(isinstance(c, str) for c in raw_cases):
            problem = problem or "cases must be a list of case selectors (strings)"
        elif not raw_cases:
            problem = problem or "cases is empty (use ['*'] for every case of the target)"
        else:
            cases = tuple(raw_cases)
        if has_whole:
            problem = "an item has either cases or whole: true, not both"
    if not has_cases and not has_whole:
        if "reason" in item:
            problem = "a reason belongs to a whole: true claim (add whole: true, or list cases)"
        else:
            legacy = True
    authored: Any = None
    if problem:  # keep what was written (rewrites stay faithful); it claims the whole target
        raw_cases = item.get("cases")
        if isinstance(raw_cases, list) and all(isinstance(c, str) for c in raw_cases):
            cases = tuple(raw_cases)
        whole = item.get("whole") is True
        authored = _plain(item)
    return VerifiedBy(
        target=norm,
        cases=cases,
        whole=whole or legacy,
        level=level,
        reason=reason,
        legacy=legacy,
        extra=extra,
        spelling=spelling,
        problem=problem,
        location=location or Location(),
        authored=authored,
    )


def _plain(value: Any) -> Any:
    """``value`` as plain dicts and lists (no loader subclasses), deep-copied."""
    if isinstance(value, Mapping):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def parse_entity(
    kind: str,
    raw: Mapping[str, Any],
    location: Location,
    errors: list[str],
    unknown: list[str],
    nested: list[str] | None = None,
    main_repo: str = "",
) -> Entity | None:
    """Build one entity from its YAML mapping. Shape errors go to ``errors``,
    unknown keys to ``unknown`` (reported under the ``unknown-field`` rule);
    unknown keys inside notes and ``verified_by`` / ``validated_by`` items go
    to ``nested`` if given, else to ``unknown`` (they are kept on the item
    either way). Claim targets are normalized with ``main_repo``."""
    nested = unknown if nested is None else nested
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
        notes=_parse_notes(raw.get("notes"), where, errors, nested),
        location=location,
    )

    def text(key: str) -> str:
        return str(raw.get(key, "") or "").strip()

    def claims(name: str) -> tuple[VerifiedBy, ...]:
        return _parse_verified_by(raw.get(name), where, errors, nested, main_repo, name, location)

    if kind == cfg.USER_NEED:
        return UserNeed(**common, rationale=text("rationale"), validated_by=claims("validated_by"))
    if kind == cfg.REQUIREMENT:
        return Requirement(
            **common,
            rationale=text("rationale"),
            category=text("category"),
            satisfies=_as_tuple(raw.get("satisfies")),
            refines=_as_tuple(raw.get("refines")),
            method=text("method"),
            verified_by=claims("verified_by"),
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
            verified_by=claims("verified_by"),
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
    config_file = ""
    project: dict[str, Any] = {}
    for path, doc in docs:
        if isinstance(doc, Mapping) and "config" in doc:
            if raw_config is not None:
                errors.append(f"{path}: 'config' is defined more than once")
            else:
                config_file = path
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
        ent = parse_entity(kind, raw, loc, errors, unknown, main_repo=config.main_repo)
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
        config_file=config_file,
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
    if root:
        model = replace(model, root=root)
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
