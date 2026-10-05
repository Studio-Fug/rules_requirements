# SPDX-License-Identifier: AGPL-3.0-or-later
"""Attribution: the one place that decides which entity a test case verifies.

**A test case verifies at most one requirement**; a set of test cases may
together verify one. Everything else in rules_requirements only produces the
inputs of this module:

* the model produces **claims** — the ``verified_by`` / ``validated_by``
  selectors of requirements, user needs and mitigations
  (:meth:`~rules_requirements.model.Model.claims`);
* evidence produces **declared ids** — the tags a case names
  (``TestCase.declared``), never an owner.

:func:`attribute` turns both into an :class:`Attribution` whose ``owner`` maps
each :class:`~rules_requirements.case_keys.CaseKey` to one entity id. A
mapping is a function: no case can have two owners. Ambiguity fails closed —
a case whose evidence names more than one id (``multi-tag``), that claims of
two entities select (``attribution-conflict``), or whose test code is owned by
two entities in two targets (``same-code-multiple-owners``) is
**quarantined**: it owns nothing, and every entity it names reads INVALID.

The verdicts (:func:`rules_requirements.trace.build_matrix`), the reports,
the editor and the agents read an entity's cases from
:meth:`Attribution.members_of` and from nothing else, and every
:class:`Attribution` is checked by :meth:`Attribution.check_invariant`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping, Sequence

from rules_requirements import case_selectors
from rules_requirements.case_keys import (
    SYNTHETIC_PATH,
    CaseKey,
    CaseRow,
    case_path,
    index_cases,
    is_unscoped,
    normalize_target,
    run_dims_from_path,
)
from rules_requirements.config import Config
from rules_requirements.ingest import ERROR, FAILED, PASSED, SKIPPED, Evidence, TestCase
from rules_requirements.ingest.junit import target_from_path
from rules_requirements.lock import Lock
from rules_requirements.model import VERIFIABLE_KINDS, Claim, Model
from rules_requirements.util import dedupe, natural_key

__all__ = [
    "ATTRIBUTION_CONFLICT",
    "MEMBER_STATES",
    "MULTI_TAG",
    "OWNED_STATES",
    "SAME_CODE",
    "Attribution",
    "AttributionInvariantError",
    "AttributionIssue",
    "CaseResult",
    "Member",
    "Quarantine",
    "TargetRun",
    "attribute",
    "is_stale",
    "resolve_cases",
]

# Quarantine codes (report time; not configurable rules).
MULTI_TAG = "multi-tag"
ATTRIBUTION_CONFLICT = "attribution-conflict"
SAME_CODE = "same-code-multiple-owners"

# Member states.
MISSING = "missing"
NOT_RUN = "not-run"
MOVED = "moved"
QUARANTINED = "quarantined"
OWNED_STATES = (PASSED, FAILED, ERROR, SKIPPED)
"""States of a member the entity owns: the merged result of its case (``error`` also for a taint)."""
MEMBER_STATES = (*OWNED_STATES, MISSING, NOT_RUN, MOVED, QUARANTINED)

# How a member came to be expected.
VIA_MODEL = "model"
VIA_TAG = "tag"
VIA_LOCK = "lock"
WHOLE_SELECTOR = "*whole*"
TAG_SELECTOR = "tag"
LOCK_SELECTOR = "lock"

# Report-time findings that are no configurable rule, and their severity.
_FIXED_SEVERITY = {
    "lock-owner-changed": "error",  # also a hard error of the static lock check
    "lock-invalid": "error",
    "unlocked-member": "warning",
    "unscoped-evidence": "warning",
    "misdirected-evidence": "warning",
    "unknown-id": "warning",
}


class AttributionInvariantError(AssertionError):
    """Raised only by :meth:`Attribution.check_invariant`: a bug in this module
    (or a hand-built :class:`Attribution`), never a property of the input."""

    def __init__(self, problems: Sequence[str]):
        self.problems = list(problems)
        super().__init__("attribution invariant violated:\n  - " + "\n  - ".join(self.problems))


def is_stale(artifact: Mapping[str, str], current: Mapping[str, str] | None) -> bool:
    """Evidence is stale when it recorded an artifact identity that differs from
    the current build on any shared key. No identity (or no reference) -> fresh."""
    if not artifact or not current:
        return False
    return any(k in current and str(current[k]) != str(v) for k, v in artifact.items())


# --------------------------------------------------------------------------- #
# Data                                                                        #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CaseResult:
    """One test case after every observation of its key was merged
    (:func:`resolve_cases`): the unit attribution gives at most one owner.

    ``artifact`` is the identity it was stamped with (``artifacts`` lists
    every distinct stamp its observations carry: two evidence roots from two
    builds give two). ``declared`` is the union of the ids its observations
    name — tags, never an owner.
    """

    key: CaseKey
    status: str  # passed | failed | error | skipped, after merging attempts, runs, shards and roots
    level: str = ""
    artifact: Mapping[str, str] = field(default_factory=dict, compare=False)
    declared: tuple[str, ...] = ()
    synthetic: bool = False
    flaky: bool = False  # an earlier attempt failed, the final one passed
    attempts: int = 1
    duplicate: bool = False
    file: str = ""
    line: int = 0
    sources: tuple[str, ...] = ()
    message: str = ""
    artifacts: tuple[Mapping[str, str], ...] = field(default=(), compare=False)
    cases: tuple[TestCase, ...] = field(default=(), repr=False, compare=False)  # the raw observations

    @property
    def is_failure(self) -> bool:
        return self.status in (FAILED, ERROR)

    def stale(self, current: Mapping[str, str] | None) -> bool:
        """Whether any of its stamps differs from ``current`` on a shared key."""
        stamps = self.artifacts or ((self.artifact,) if self.artifact else ())
        return any(is_stale(stamp, current) for stamp in stamps)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "case": str(self.key),
            "target": self.key.target,
            "path": self.key.path,
            "status": self.status,
            "declared": list(self.declared),
        }
        for name in ("level", "file", "line"):
            if getattr(self, name):
                out[name] = getattr(self, name)
        for flag in ("synthetic", "flaky", "duplicate"):
            if getattr(self, flag):
                out[flag] = True
        if self.attempts > 1:
            out["attempts"] = self.attempts
        return out


@dataclass(frozen=True)
class TargetRun:
    """How one target ran in this evidence.

    ``taint`` holds its failing target-scope results (``rr.scope=target``: an
    exit status, a load error, an unreadable report, a failing root hook),
    and the failed synthetic result of a shard or run that crashed while
    other repetitions reported per-case results. They are never members and
    never count as passing; every member claimed on the target reads
    ``error`` instead.
    """

    target: str
    ran: bool = True
    synthetic_only: bool = False  # its only case results are its single whole-target result
    taint: tuple[TestCase, ...] = field(default=(), compare=False)

    @property
    def tainted(self) -> bool:
        return bool(self.taint)

    @property
    def taint_message(self) -> str:
        """``<name> (<shard/run>): <first line of its message>`` of each
        target-scope failure; the slot (``shard_2_of_4``, ``run_3_of_5``) says
        which repetition failed, when the target was sharded or repeated."""
        parts = []
        for case in self.taint:
            first = ((case.message or case.status).splitlines() or [case.status])[0][:200]
            dims = run_dims_from_path(case.source)
            slot = "_".join(
                [f"shard_{dims.shard}_of_{dims.shards}"] * bool(dims.shards)
                + [f"run_{dims.run}_of_{dims.runs}"] * bool(dims.runs)
            )
            where = f" ({slot})" if slot else ""
            parts.append(f"{case_path(case.classname, case.name)}{where}: {first}")
        return "; ".join(parts)


@dataclass(frozen=True)
class Member:
    """One member of an entity's verification set.

    * owned (``passed`` / ``failed`` / ``error`` / ``skipped``): a case the
      entity owns, with its ``result``;
    * ``missing`` / ``not-run``: a selector that matched nothing, or a lock
      entry, whose case is absent (``key`` is the case a literal selector or
      a lock entry names; None for a glob or whole claim);
    * ``moved``: a lock entry whose case now has another owner or none;
    * ``quarantined``: a quarantined case that names the entity.
    """

    entity: str
    key: CaseKey | None
    selector: str  # the claim's pattern | "*whole*" | "tag" | "lock"
    via: str  # model | tag | lock
    state: str
    level: str = ""
    stale: bool = False
    flaky: bool = False
    result: CaseResult | None = field(default=None, compare=False)
    target: str = ""  # where the member is expected (the key's target, or the claim's)
    origin: str = ""  # where the claim or lock entry is written ("" for a tag)
    reason: str = ""  # why the state: a taint, a quarantine code, the new owner

    @property
    def owned(self) -> bool:
        """Whether this is a case the entity owns (it counts as its evidence)."""
        return self.result is not None and self.state in OWNED_STATES

    @property
    def name(self) -> str:
        """The case key, or ``<target> <selector>`` for a member without one."""
        return str(self.key) if self.key is not None else f"{self.target} {self.selector}"

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "case": str(self.key) if self.key is not None else None,
            "target": self.target,
            "selector": self.selector,
            "via": self.via,
            "state": self.state,
        }
        if self.level:
            out["level"] = self.level
        if self.stale:
            out["stale"] = True
        if self.flaky:
            out["flaky"] = True
        if self.reason:
            out["reason"] = self.reason
        return out


@dataclass(frozen=True)
class Quarantine:
    """A case that owns nothing because its attribution is ambiguous."""

    key: CaseKey
    code: str  # multi-tag | attribution-conflict | same-code-multiple-owners
    entities: tuple[str, ...]  # every entity it names: each reads INVALID
    detail: str
    declared: tuple[str, ...] = ()
    claims: tuple[Claim, ...] = field(default=(), compare=False)  # the claims selecting the key

    def to_dict(self) -> dict[str, Any]:
        return {
            "case": str(self.key),
            "code": self.code,
            "entities": list(self.entities),
            "declared": list(self.declared),
            "claims": [
                {"entity": c.entity, "selector": c.pattern if c.pattern is not None else WHOLE_SELECTOR}
                | ({"location": str(c.location)} if c.location.path else {})
                for c in self.claims
            ],
            "detail": self.detail,
        }


@dataclass(frozen=True)
class AttributionIssue:
    """A finding of attribution that is not a quarantine: a tag that
    disagrees with the model, a missing lock entry, a duplicate case, ..."""

    code: str
    message: str
    severity: str = "warning"
    key: CaseKey | None = None
    entities: tuple[str, ...] = ()
    declared: tuple[str, ...] = ()
    target: str = ""

    def __str__(self) -> str:
        return f"{self.severity}: [{self.code}] {self.message}"

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "severity": self.severity, "message": self.message}
        if self.key is not None:
            out["case"] = str(self.key)
        if self.entities:
            out["entities"] = list(self.entities)
        if self.declared:
            out["declared"] = list(self.declared)
        if self.target:
            out["target"] = self.target
        return out


@dataclass(frozen=True)
class Attribution:
    """The result of :func:`attribute`: read-only, and checked.

    ``owner`` is THE function from case keys to entity ids; ``members`` are
    each verifiable entity's verification set; the entities a quarantined case
    names each hold it as a ``quarantined`` member.
    """

    mode: str
    cases: Mapping[CaseKey, CaseResult]
    owner: Mapping[CaseKey, str]
    via: Mapping[CaseKey, str]  # model | tag
    members: Mapping[str, tuple[Member, ...]]
    quarantined: tuple[Quarantine, ...]
    targets: Mapping[str, TargetRun]
    issues: tuple[AttributionIssue, ...] = ()
    lock: Lock | None = None
    claimed_by: Mapping[CaseKey, tuple[Claim, ...]] = field(default_factory=dict, compare=False)
    _quarantine: Mapping[CaseKey, Quarantine] = field(default_factory=dict, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        index: dict[CaseKey, Quarantine] = {}
        for q in self.quarantined:
            index.setdefault(q.key, q)
        object.__setattr__(self, "_quarantine", MappingProxyType(index))

    @property
    def entities(self) -> tuple[str, ...]:
        """The verifiable entities (user needs, requirements, mitigations)."""
        return tuple(self.members)

    def members_of(self, entity: str) -> tuple[Member, ...]:
        """The verification set of ``entity`` (empty for anything else)."""
        return self.members.get(entity, ())

    def owner_of(self, key: CaseKey) -> str | None:
        return self.owner.get(key)

    def quarantine_of(self, key: CaseKey) -> Quarantine | None:
        return self._quarantine.get(key)

    def check_invariant(self) -> None:
        """Assert the one-owner invariant; raises :class:`AttributionInvariantError`.

        * ``owner`` is a function from case keys to one entity id each;
        * the owned members (states passed/failed/error/skipped) partition
          the owned keys: each owned key is a member of exactly its owner;
        * no key is both owned and quarantined;
        * every entity a quarantine names holds that key as a
          ``quarantined`` member.
        """
        problems: list[str] = []
        for key, ent in self.owner.items():
            if not isinstance(key, CaseKey):
                problems.append(f"owner key {key!r} is not a case key")
            if not isinstance(ent, str) or not ent or ent not in self.members:
                problems.append(f"{key}: owner {ent!r} is not one verifiable entity id")
            if key not in self.cases:
                problems.append(f"{key}: owned, but no such case")
            if self.via.get(key) not in (VIA_MODEL, VIA_TAG):
                problems.append(f"{key}: owned via {self.via.get(key)!r}")
        if set(self.via) != set(self.owner):
            problems.append("via and owner name different keys")
        seen: dict[CaseKey, str] = {}
        quarantined_members: set[tuple[str, CaseKey]] = set()
        for ent, members in self.members.items():
            for m in members:
                if m.entity != ent:
                    problems.append(f"a member of {ent} names {m.entity}")
                if m.state not in MEMBER_STATES:
                    problems.append(f"{m.name}: unknown member state {m.state!r}")
                if m.owned:
                    if m.key is None:
                        problems.append(f"{ent}: an owned member without a case key")
                        continue
                    if self.owner.get(m.key) != ent:
                        problems.append(f"{m.key} is a member of {ent} but owned by {self.owner.get(m.key)}")
                    if m.key in seen:
                        problems.append(f"{m.key} is an owned member of {seen[m.key]} and of {ent}")
                    seen.setdefault(m.key, ent)
                elif m.state == QUARANTINED:
                    q = self._quarantine.get(m.key) if m.key is not None else None
                    if q is None or ent not in q.entities:
                        problems.append(f"{m.name} is a quarantined member of {ent}, which no quarantine names")
                    elif m.key is not None:
                        quarantined_members.add((ent, m.key))
        for key in self.owner:
            if key not in seen:
                problems.append(f"{key} is owned by {self.owner[key]} but no member of it")
        keys = [q.key for q in self.quarantined]
        if len(set(keys)) != len(keys):
            problems.append("a case is quarantined twice")
        for q in self.quarantined:
            if q.key in self.owner:
                problems.append(f"{q.key} is quarantined ({q.code}) and owned by {self.owner[q.key]}")
            for ent in q.entities:
                if (ent, q.key) not in quarantined_members:
                    problems.append(f"{q.key} ({q.code}) names {ent}, which has no quarantined member for it")
        if problems:
            raise AttributionInvariantError(problems)


# --------------------------------------------------------------------------- #
# Step 1: one result per case key                                             #
# --------------------------------------------------------------------------- #


def _issue(config: Config, code: str, message: str, **kw: Any) -> AttributionIssue | None:
    """An issue with its configured severity; None when its rule is off."""
    severity = _FIXED_SEVERITY.get(code) or config.rule(code)
    if severity == "off":
        return None
    return AttributionIssue(code, message, severity, **kw)


def _representative(row: CaseRow) -> TestCase:
    return next((c for c in row.cases if c.status == row.status), row.cases[0])


def _result(row: CaseRow) -> CaseResult:
    stamps: list[dict[str, str]] = []
    for case in row.cases:
        stamp = dict(case.artifact)
        if stamp and stamp not in stamps:
            stamps.append(stamp)
    merged: dict[str, str] = {}
    for stamp in stamps:
        for k, v in stamp.items():
            merged.setdefault(k, v)
    return CaseResult(
        key=row.key,
        status=row.status,
        level=row.level,
        artifact=MappingProxyType(merged),
        declared=row.declared,
        synthetic=row.synthetic,
        flaky=row.flaky,
        attempts=row.attempts,
        duplicate=row.duplicate,
        file=row.file,
        line=row.line,
        sources=row.sources,
        message=row.message,
        artifacts=tuple(MappingProxyType(s) for s in stamps),
        cases=row.cases,
    )


def resolve_cases(
    evidence: Evidence, config: Config
) -> tuple[dict[CaseKey, CaseResult], dict[str, TargetRun], list[AttributionIssue]]:
    """Merge raw test cases into one :class:`CaseResult` per key.

    Targets are normalized (``config.main_repo``) exactly as claims are. The
    final attempt wins (an earlier failure under a final pass is ``flaky``),
    the worst run wins, shards are unioned and evidence roots merged
    (:func:`~rules_requirements.case_keys.index_cases`). Target-scope results
    are not cases: a failing one taints its target (:class:`TargetRun`). So
    does a failed synthetic ``[target]`` result of a target that also
    reported per-case results: a shard or run that crashed before writing
    its report, beside the repetitions that did (the worst run wins). A
    target also counts as run when it left a report without any case (an
    empty suite), so its claimed cases read ``missing``, not ``not-run``.
    """
    rows = index_cases(evidence, main_repo=config.main_repo)
    cases: dict[CaseKey, CaseResult] = {}
    taint: dict[str, list[TestCase]] = {}
    present: dict[str, None] = {}
    found: list[AttributionIssue | None] = []
    # Targets that reported per-case results. Their failed synthetic result
    # is a crashed shard or run (Bazel's generated test.xml for the
    # repetition that wrote no JUnit): a failure of the whole target run.
    with_cases = {key.target for key, row in rows.items() if not row.target_scope and not row.synthetic}
    for key, row in rows.items():
        present.setdefault(key.target)
        crashed = row.synthetic and row.status in (FAILED, ERROR) and key.target in with_cases
        if row.target_scope or crashed:
            if row.status in (FAILED, ERROR):
                taint.setdefault(key.target, []).append(_representative(row))
            continue
        cases[key] = _result(row)
        if row.duplicate:
            found.append(
                _issue(
                    config,
                    "duplicate-case",
                    f"{key} is reported more than once in one run; its results are folded into one "
                    f"(worst status: {row.status}); rename one of the tests",
                    key=key,
                    target=key.target,
                )
            )
    for path in evidence.files:
        target = target_from_path(path)
        if target:
            present.setdefault(normalize_target(target, config.main_repo))
    by_target: dict[str, list[CaseResult]] = {}
    for key, res in cases.items():
        by_target.setdefault(key.target, []).append(res)
    targets: dict[str, TargetRun] = {}
    for target in sorted(present, key=natural_key):
        results = by_target.get(target, [])
        targets[target] = TargetRun(
            target,
            ran=True,
            synthetic_only=bool(results) and all(r.synthetic for r in results),
            taint=tuple(taint.get(target, ())),
        )
        if is_unscoped(target):
            found.append(
                _issue(
                    config,
                    "unscoped-evidence",
                    f"{target}: JUnit outside a testlogs tree cannot be pinned to a build target; claim its "
                    f"cases as {target} or pass the report under its target's bazel-testlogs path",
                    target=target,
                )
            )
    for ingest_issue in evidence.issues:
        found.append(
            _issue(
                config,
                ingest_issue.code,
                str(ingest_issue),
                target=normalize_target(ingest_issue.target, config.main_repo) if ingest_issue.target else "",
                declared=tuple(ingest_issue.ids),
            )
        )
    return cases, targets, [i for i in found if i is not None]


# --------------------------------------------------------------------------- #
# Steps 2-6: claims, owners, quarantine, members                              #
# --------------------------------------------------------------------------- #


def attribute(
    model: Model,
    evidence: Evidence,
    *,
    current_build: Mapping[str, str] | None = None,
    lock: Lock | None = None,
) -> Attribution:
    """Decide the owner of every test case in ``evidence``, and each verifiable
    entity's verification set. The only function that assigns ownership.

    1. :func:`resolve_cases` merges the evidence into one result per key.
    2. Every claim selects keys: a whole claim every case result of its
       target (its synthetic ``[target]`` result only if the target reported
       nothing else); a selector the non-synthetic results whose path it
       matches.
    3. Per key, the first rule that applies: more than one declared id →
       quarantine ``multi-tag``; claims of more than one entity →
       quarantine ``attribution-conflict``; one claimant → it owns the key
       (``tag-mismatch`` if the case declares another id); hybrid mode and
       one declared requirement, user need or mitigation → it owns the key;
       model mode and one such id → ``unclaimed-tag``. A declared risk or
       test-method id is ``misdirected-evidence`` and an undefined one
       ``unknown-id``: neither owns anything.
    4. Owned keys of one test code (the same source file and path, or one
       ``config.variants`` group and path) in different targets with
       different owners are quarantined ``same-code-multiple-owners``;
       equal paths with different owners and no source file to compare are
       ``same-path-multiple-owners``.
    5. Members: each entity's owned keys; a pseudo-member per selector that
       matched nothing (``missing`` if the target ran, ``not-run`` if not,
       ``error`` if it is tainted or its only result is a failed synthetic
       one); its lock entries (``lock`` adds expected members only, never an
       owner); the quarantined keys that name it.
    6. :meth:`Attribution.check_invariant`.

    ``lock`` is the verification-set lock to expect members from (None: no
    lock; this function reads no files). ``current_build`` marks members
    stamped with another build ``stale``.
    """
    attribution = _Attributor(model, evidence, current_build, lock).run()
    attribution.check_invariant()
    return attribution


class _Attributor:
    def __init__(
        self,
        model: Model,
        evidence: Evidence,
        current_build: Mapping[str, str] | None,
        lock: Lock | None,
    ) -> None:
        self.m = model
        self.c = model.config
        self.evidence = evidence
        self.mode = "model" if self.c.attribution == "model" else "hybrid"
        self.current = dict(current_build or {})
        self.lock = lock
        self.entities = [
            e.id
            for kind in VERIFIABLE_KINDS
            for e in sorted(model.section(kind).values(), key=lambda e: natural_key(e.id))
        ]
        self.verifiable = set(self.entities)
        self.issues: list[AttributionIssue] = []
        self.cases: dict[CaseKey, CaseResult] = {}
        self.targets: dict[str, TargetRun] = {}
        self.by_target: dict[str, list[CaseKey]] = {}
        self.claims: list[Claim] = []
        self.matched: list[list[CaseKey]] = []
        self.claimed_by: dict[CaseKey, list[int]] = {}
        self.owner: dict[CaseKey, str] = {}
        self.via: dict[CaseKey, str] = {}
        self.quarantine: dict[CaseKey, Quarantine] = {}

    def issue(self, code: str, message: str, **kw: Any) -> None:
        found = _issue(self.c, code, message, **kw)
        if found is not None:
            self.issues.append(found)

    # --- the run ------------------------------------------------------------

    def run(self) -> Attribution:
        self.cases, targets, issues = resolve_cases(self.evidence, self.c)
        self.issues.extend(issues)
        self.targets = dict(targets)
        for key in self.cases:
            self.by_target.setdefault(key.target, []).append(key)
        self.claims = self.m.claims()
        self.select()
        self.decide()
        self.same_code()
        members = self.build_members()
        for target in sorted({c.target for c in self.claims} | self._lock_targets(), key=natural_key):
            self.targets.setdefault(target, TargetRun(target, ran=False))
        claimed_by = {
            key: tuple(self.claims[i] for i in idxs) for key, idxs in sorted(self.claimed_by.items(), key=_by_key)
        }
        return Attribution(
            mode=self.mode,
            cases=MappingProxyType(self.cases),
            owner=MappingProxyType(dict(sorted(self.owner.items(), key=_by_key))),
            via=MappingProxyType(dict(sorted(self.via.items(), key=_by_key))),
            members=MappingProxyType(members),
            quarantined=tuple(sorted(self.quarantine.values(), key=lambda q: _key_order(q.key))),
            targets=MappingProxyType(dict(sorted(self.targets.items(), key=lambda kv: natural_key(kv[0])))),
            issues=tuple(self.issues),
            lock=self.lock,
            claimed_by=MappingProxyType(claimed_by),
        )

    def _lock_targets(self) -> set[str]:
        return {e.target for e in self.lock.entries} if self.lock is not None else set()

    # --- step 2: what each claim selects -----------------------------------

    def select(self) -> None:
        for i, claim in enumerate(self.claims):
            keys = self.selected(claim)
            self.matched.append(keys)
            for key in keys:
                self.claimed_by.setdefault(key, []).append(i)
            if claim.pattern is None and self.has_cases(claim.target):
                self.issue(
                    "coarse-claim",
                    f"{claim.entity}: {claim.describe()} of {claim.target} claims {len(keys)} per-case result(s) at "
                    f"once ({claim.location}); claim them as {{target: {claim.target}, cases: ['*']}} (or narrower "
                    "selectors) so they are enumerated and locked",
                    entities=(claim.entity,),
                    target=claim.target,
                )

    def has_cases(self, target: str) -> bool:
        """Whether ``target`` reported per-case results (anything but its synthetic one)."""
        return any(not self.cases[k].synthetic for k in self.by_target.get(target, ()))

    def selected(self, claim: Claim) -> list[CaseKey]:
        keys = self.by_target.get(claim.target, [])
        if not keys:
            return []
        if claim.pattern is None:
            run = self.targets.get(claim.target)
            if run is not None and run.synthetic_only:
                return list(keys)
            return [k for k in keys if not self.cases[k].synthetic]
        try:
            if claim.literal:
                key = CaseKey(claim.target, case_selectors.literal_path(claim.pattern))
                return [key] if key in self.cases and not self.cases[key].synthetic else []
            return [k for k in keys if not self.cases[k].synthetic and case_selectors.matches(claim.pattern, k.path)]
        except ValueError:  # a bad selector selects nothing (validation reports it: bad-selector)
            return []

    # --- step 3: one owner, or a quarantine ---------------------------------

    def claimants(self, key: CaseKey) -> list[str]:
        return dedupe([self.claims[i].entity for i in self.claimed_by.get(key, ())])

    def claims_of(self, key: CaseKey) -> list[Claim]:
        return [self.claims[i] for i in self.claimed_by.get(key, ())]

    def decide(self) -> None:
        for key, res in self.cases.items():
            declared = res.declared
            who = self.claimants(key)
            for rid in declared:
                if rid not in self.verifiable:
                    self.misdirected(key, rid)
            if len(declared) > 1:
                named = dedupe([d for d in declared if d in self.verifiable] + who)
                self.quarantine[key] = Quarantine(
                    key,
                    MULTI_TAG,
                    tuple(sorted(named, key=natural_key)),
                    self.multi_tag_detail(key, res, who),
                    declared,
                    tuple(self.claims_of(key)),
                )
            elif len(who) > 1:
                self.quarantine[key] = Quarantine(
                    key,
                    ATTRIBUTION_CONFLICT,
                    tuple(sorted(who, key=natural_key)),
                    f"{key} is claimed by {_origins(self.claims_of(key))}; it verifies "
                    f"{'neither' if len(who) == 2 else 'none of them'} until it has one owner",
                    declared,
                    tuple(self.claims_of(key)),
                )
            elif who:
                (owner,) = who
                self.owner[key], self.via[key] = owner, VIA_MODEL
                if declared and declared[0] != owner:
                    self.issue(
                        "tag-mismatch",
                        f"{key} declares {declared[0]} but is owned by {owner} through its claim "
                        f"({_origins(self.claims_of(key))}); the model decides, the tag is only a cross-check",
                        key=key,
                        entities=(owner,),
                        declared=declared,
                    )
            elif declared and declared[0] in self.verifiable:
                if self.mode == "hybrid":
                    if res.synthetic and self.has_cases(key.target):
                        # A synthetic result is a member only when its target
                        # reported nothing else (as for a whole claim).
                        continue
                    self.owner[key], self.via[key] = declared[0], VIA_TAG
                else:
                    self.issue(
                        "unclaimed-tag",
                        f"{key} declares {declared[0]}, but no claim selects it (attribution: model); add a "
                        f"selector for it to {declared[0]}, or drop the tag",
                        key=key,
                        entities=(declared[0],),
                        declared=declared,
                    )

    def misdirected(self, key: CaseKey, rid: str) -> None:
        ent = self.m.get(rid)
        if ent is not None:
            self.issue(
                "misdirected-evidence",
                f"{key} declares {rid}, a {ent.kind.replace('_', ' ')}: only requirements, user needs and "
                "mitigations are verified by test cases (tag the requirement instead)",
                key=key,
                entities=(rid,),
                declared=(rid,),
            )
        else:
            self.issue(
                "unknown-id",
                f"{key} declares {rid}, which the model does not define",
                key=key,
                entities=(rid,),
                declared=(rid,),
            )

    def multi_tag_detail(self, key: CaseKey, res: CaseResult, who: list[str]) -> str:
        # No evidence file paths: reports are deterministic across machines.
        text = (
            f"{key} declares {', '.join(res.declared)}; a test case verifies at most one requirement, "
            "so it verifies none of them until its evidence names one"
        )
        if who:
            text += f" (it is claimed by {_origins(self.claims_of(key))})"
        return text

    # --- step 4: one test code, one owner -----------------------------------

    def same_code(self) -> None:
        groups: list[tuple[list[CaseKey], str]] = []
        by_code: dict[tuple[str, str], list[CaseKey]] = {}
        for key in self.owner:
            source = self.cases[key].file
            if source:
                by_code.setdefault((source, key.path), []).append(key)
        for (source, _), keys in by_code.items():
            if len({k.target for k in keys}) > 1 and len({self.owner[k] for k in keys}) > 1:
                groups.append((keys, f"the same source file {source}"))
        for group in self.c.variant_groups():
            by_path: dict[str, list[CaseKey]] = {}
            for target in group:
                for key in self.by_target.get(target, ()):
                    if key in self.owner:
                        by_path.setdefault(key.path, []).append(key)
            for keys in by_path.values():
                if len({self.owner[k] for k in keys}) > 1:
                    groups.append((keys, f"targets declared as variants of one test ({', '.join(group)})"))
        conflicted: dict[CaseKey, list[tuple[list[CaseKey], str]]] = {}
        for keys, why in groups:
            for key in keys:
                conflicted.setdefault(key, []).append((keys, why))
        before = dict(self.owner)
        for key, found in conflicted.items():
            others = sorted({k for keys, _ in found for k in keys if k != key}, key=_key_order)
            owners = ", ".join(f"{k} (owned by {before[k]})" for k in others)
            whys = "; ".join(dedupe([why for _, why in found]))
            self.quarantine[key] = Quarantine(
                key,
                SAME_CODE,
                (before[key],),
                f"{key} (owned by {before[key]}, {self.owner_origin(key)}) runs the same test code as {owners}: "
                f"{whys}; a test case verifies at most one requirement, so none of them verifies anything "
                "until one entity owns them all (or the variants get distinct case names)",
                self.cases[key].declared,
                tuple(self.claims_of(key)),
            )
        for key in conflicted:
            del self.owner[key]
            del self.via[key]
        owned_paths: dict[str, list[CaseKey]] = {}
        for key in self.owner:
            if key.path != SYNTHETIC_PATH:
                owned_paths.setdefault(key.path, []).append(key)
        for path, keys in owned_paths.items():
            owners_ = sorted({self.owner[k] for k in keys}, key=natural_key)
            if len(owners_) > 1 and any(not self.cases[k].file for k in keys):
                listed = ", ".join(f"{k} ({self.owner[k]})" for k in sorted(keys, key=_key_order))
                self.issue(
                    "same-path-multiple-owners",
                    f"case path {path!r} is owned by {', '.join(owners_)} in different targets: {listed}; if it "
                    "is one test's code, declare the targets in config.variants (or record rr.file) so it "
                    "cannot verify two requirements",
                    entities=tuple(owners_),
                )

    def owner_origin(self, key: CaseKey) -> str:
        if self.via.get(key) == VIA_TAG:
            return "through its tag"
        claims = [c for c in self.claims_of(key) if c.entity == self.owner.get(key)]
        return f"claimed at {', '.join(str(c.location) for c in claims)}" if claims else "through its claim"

    # --- step 5: members ----------------------------------------------------

    def build_members(self) -> dict[str, tuple[Member, ...]]:
        members: dict[str, list[Member]] = {e: [] for e in self.entities}
        have: dict[str, set[CaseKey]] = {e: set() for e in self.entities}

        def add(member: Member) -> None:
            members[member.entity].append(member)
            if member.key is not None:
                have[member.entity].add(member.key)

        # Owned keys: through the entity's claims (model), or its tag (hybrid).
        unmatched: list[Claim] = []
        for i, claim in enumerate(self.claims):
            ent = claim.entity
            if ent not in members:
                continue
            selector = claim.pattern if claim.pattern is not None else WHOLE_SELECTOR
            keys = self.matched[i]
            if not keys:
                unmatched.append(claim)
            for key in keys:
                if self.owner.get(key) == ent and key not in have[ent]:
                    add(self.owned_member(ent, key, selector, VIA_MODEL, claim.level, str(claim.location)))
        for key, ent in self.owner.items():
            if self.via[key] == VIA_TAG and key not in have[ent]:
                add(self.owned_member(ent, key, TAG_SELECTOR, VIA_TAG, "", ""))
        # Quarantined keys, for every entity they name.
        for q in self.quarantine.values():
            res = self.cases[q.key]
            for ent in q.entities:
                if ent not in members:
                    continue
                claims = [c for c in q.claims if c.entity == ent]
                if claims:
                    claim = claims[0]
                    selector = claim.pattern if claim.pattern is not None else WHOLE_SELECTOR
                    via, level, origin = VIA_MODEL, claim.level, str(claim.location)
                else:
                    selector, via, level, origin = TAG_SELECTOR, VIA_TAG, "", ""
                add(
                    Member(
                        ent,
                        q.key,
                        selector,
                        via,
                        QUARANTINED,
                        self.level_of(res, level),
                        result=res,
                        target=q.key.target,
                        origin=origin,
                        reason=q.code,
                    )
                )
        # Expected members: a selector that matched nothing, and lock entries.
        for claim in unmatched:
            expected = self.literal_key(claim)
            if expected is not None and expected in have[claim.entity]:
                continue
            state, reason = self.absent(claim.target)
            add(
                Member(
                    claim.entity,
                    expected,
                    claim.pattern if claim.pattern is not None else WHOLE_SELECTOR,
                    VIA_MODEL,
                    state,
                    self.claim_level(claim.level),
                    target=claim.target,
                    origin=str(claim.location),
                    reason=reason,
                )
            )
        if self.lock is not None:
            self.lock_members(self.lock, add, have)
            for key, ent in self.owner.items():
                if self.lock.entry(key.target, key.path) is None:
                    self.issue(
                        "unlocked-member",
                        f"{key} is a member of {ent}'s verification set but not in the lock "
                        f"{self.lock.path or '(sets_lock)'}; re-lock (`rr sets lock --write`) and review the addition",
                        key=key,
                        entities=(ent,),
                    )
        return {ent: tuple(sorted(ms, key=_member_order)) for ent, ms in members.items()}

    def lock_members(self, lock: Lock, add: Callable[[Member], None], have: dict[str, set[CaseKey]]) -> None:
        where = lock.path or "<lock>"
        by_owner_target: dict[tuple[str, str], list[Claim]] = {}
        for claim in self.claims:
            by_owner_target.setdefault((claim.entity, claim.target), []).append(claim)
        for entry in lock.entries:
            ent = entry.owner
            if ent not in have:
                continue  # not a verifiable entity: lock-invalid, which validation reports
            key = CaseKey(entry.target, entry.path)
            origin = f"{where}:{entry.line}" if entry.line else where
            if self.mode == "model" and not any(
                _claim_matches(c, entry.path) for c in by_owner_target.get((ent, entry.target), ())
            ):
                self.issue(
                    "lock-stale",
                    f"{key} is locked to {ent} ({origin}), but no claim of {ent} selects it; re-lock "
                    "(`rr sets lock --write --allow-removals`) or restore the claim",
                    key=key,
                    entities=(ent,),
                )
            if key in have[ent]:
                continue  # owned (or quarantined) already: the lock only confirms it
            q = self.quarantine.get(key)
            if q is not None or key in self.cases:
                now = self.owner.get(key)
                what = f"quarantined ({q.code})" if q is not None else (f"owned by {now}" if now else "owned by nobody")
                add(
                    Member(
                        ent,
                        key,
                        LOCK_SELECTOR,
                        VIA_LOCK,
                        MOVED,
                        self.claim_level(""),
                        result=self.cases.get(key),
                        target=key.target,
                        origin=origin,
                        reason=f"locked to {ent}, now {what}",
                    )
                )
                self.issue(
                    "lock-owner-changed",
                    f"{key} is locked to {ent} ({origin}) but is now {what}; re-lock (`rr sets lock --write`) and "
                    "review the owner change",
                    key=key,
                    entities=tuple(e for e in (ent, now) if e),
                )
                continue
            state, reason = self.absent(entry.target)
            add(
                Member(
                    ent,
                    key,
                    LOCK_SELECTOR,
                    VIA_LOCK,
                    state,
                    self.claim_level(""),
                    target=key.target,
                    origin=origin,
                    reason=reason,
                )
            )

    def literal_key(self, claim: Claim) -> CaseKey | None:
        if claim.pattern is None or not claim.literal:
            return None
        try:
            return CaseKey(claim.target, case_selectors.literal_path(claim.pattern))
        except ValueError:
            return None

    def owned_member(self, ent: str, key: CaseKey, selector: str, via: str, claim_level: str, origin: str) -> Member:
        res = self.cases[key]
        run = self.targets.get(key.target)
        if run is not None and run.taint:
            state, reason = ERROR, f"tainted: {run.taint_message}"
        else:
            state, reason = res.status, ""
        return Member(
            ent,
            key,
            selector,
            via,
            state,
            self.level_of(res, claim_level, ent),
            stale=state == PASSED and res.stale(self.current),
            flaky=res.flaky,
            result=res,
            target=key.target,
            origin=origin,
            reason=reason,
        )

    def absent(self, target: str) -> tuple[str, str]:
        """The state of an expected member whose case is not in the evidence."""
        run = self.targets.get(target)
        if run is None or not run.ran:
            return NOT_RUN, f"{target} has no evidence in this report"
        if run.taint:
            return ERROR, f"tainted: {run.taint_message}"
        if run.synthetic_only:
            failed = [self.cases[k] for k in self.by_target.get(target, ()) if self.cases[k].is_failure]
            if failed:
                first = (failed[0].message or failed[0].status).splitlines()[0][:200]
                return ERROR, f"{target} reported no per-case results and {failed[0].status}: {first}"
            return MISSING, f"{target} reports no per-case results (only its whole-target result)"
        return MISSING, f"{target} ran without it"

    def claim_level(self, level: str) -> str:
        return (level or "").strip().lower() or self.c.default_provided_level

    def level_of(self, res: CaseResult, claim_level: str, ent: str = "") -> str:
        """The case's own level, else the claim's, else the default; the lower
        of the two (``level-mismatch``) when both are given and differ."""
        own = (res.level or "").strip().lower()
        claimed = (claim_level or "").strip().lower()
        if own and claimed and own != claimed:
            lower = own if self._rank(own) <= self._rank(claimed) else claimed
            if ent:
                self.issue(
                    "level-mismatch",
                    f"{res.key} provides {own} but {ent}'s claim says {claimed}; counted as {lower}",
                    key=res.key,
                    entities=(ent,),
                )
            return lower
        return own or claimed or self.c.default_provided_level

    def _rank(self, level: str) -> int:
        rank = self.c.rank(level)
        return -1 if rank is None else rank


def _claim_matches(claim: Claim, path: str) -> bool:
    try:
        return claim.matches(path)
    except ValueError:
        return False


def _origins(claims: Iterable[Claim]) -> str:
    """``PR-13 (requirements.yaml:233) and PR-29 (requirements.yaml:411)``: every claim's entity and origin."""
    by_entity: dict[str, list[str]] = {}
    for claim in claims:
        by_entity.setdefault(claim.entity, []).append(str(claim.location))
    parts = [f"{ent} ({', '.join(dedupe(locs))})" for ent, locs in by_entity.items()]
    if len(parts) < 2:
        return "".join(parts)
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def _key_order(key: CaseKey) -> tuple[Any, ...]:
    return (natural_key(key.target), natural_key(key.path))


def _by_key(item: tuple[CaseKey, Any]) -> tuple[Any, ...]:
    return _key_order(item[0])


def _member_order(m: Member) -> tuple[Any, ...]:
    return (natural_key(m.target), natural_key(m.key.path if m.key is not None else ""), m.selector, m.via)
