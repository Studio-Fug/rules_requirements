# SPDX-License-Identifier: AGPL-3.0-or-later
"""Join the model with test evidence (and optionally source annotations) into a
traceability matrix, and derive the gaps that remain.

**A test case verifies at most one requirement.** Which entity a case
verifies is decided by :func:`rules_requirements.attribution.attribute` alone;
this module reads nothing but the resulting
:class:`~rules_requirements.attribution.Attribution`. Each requirement (user
need, mitigation) is verified by its *verification set*: the cases it owns,
the cases its literal selectors and the lock expect, and every quarantined
case that names it (:meth:`~rules_requirements.attribution.Attribution.members_of`).

Verification (IEC 62304 §5.5-5.7): is each **requirement** proven, *the way it
needs to be proven*? :func:`verdict_from_members`, the first rule that applies:

* ``INVALID`` — a member is quarantined: a case naming it is ambiguous
  (several ids, several claimants, one test code with several owners), so it
  verifies nothing through it until the case has one owner.
* ``FAILED`` — a member failed or errored (a target-scope failure taints
  every member of its target), or passed only on a retry under
  ``flaky: fail``.
* ``UNVERIFIED`` — no members, or none of them ran.
* ``INCOMPLETE`` — a member is missing, did not run, was skipped or moved
  (or its builds are mixed under ``set_consistency: enforce``).
* ``UNDER-VERIFIED`` — the whole set passed, but a member is stale (stamped
  with another build than ``current_build``), passed only on a retry
  (``flaky: under-verify``, the default), or the best level is below the
  demanded one (its ``method``).
* ``VERIFIED`` — otherwise: the whole set passed together, fresh, at the
  demanded rigor.
* ``PARTIAL`` — (decomposed requirements) its refinements are only partly
  verified.

Validation (design validation, IEC 62304 §5.1 / 21 CFR 820.30(g)): each **user
need** rolls up the requirements that satisfy it -> ``VALIDATED``, ``PARTIAL``,
``FAILED`` or ``UNVALIDATED``.

Risk control (ISO 14971 §7.2): each **mitigation** rolls up the requirements
that implement it (``VERIFIED``/``PARTIAL``/``FAILED``/``UNVERIFIED``) and each
**risk** rolls up its mitigations -> ``MITIGATED``, ``PARTIAL``, ``FAILED`` or
``OPEN``.

Rollups (``refines``, ``satisfies``, ``implemented_by``, ``mitigates``)
propagate verdicts between entities, never cases: an INVALID entity rolls up
like a FAILED one and an INCOMPLETE one like a PARTIAL one, and each verdict
says whether it rests on its own set (``basis: own``), on other entities'
verdicts (``derived``, with ``derived_from``) or on both.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Iterable, Mapping, Sequence

from rules_requirements import case_selectors
from rules_requirements import config as cfg
from rules_requirements import lock as rr_lock
from rules_requirements.annotations import Reference
from rules_requirements.attribution import (
    MISSING,
    MOVED,
    NOT_RUN,
    QUARANTINED,
    TAG_SELECTOR,
    WHOLE_SELECTOR,
    Attribution,
    AttributionIssue,
    Member,
    attribute,
    is_stale,
)
from rules_requirements.ingest import ERROR, PASSED, SKIPPED, Evidence
from rules_requirements.ingest import FAILED as FAILED_CASE
from rules_requirements.model import Model, Requirement
from rules_requirements.util import natural_key

__all__ = [
    "FAILED",
    "INCOMPLETE",
    "INVALID",
    "MITIGATED",
    "OPEN",
    "PARTIAL",
    "UNDER_VERIFIED",
    "UNVALIDATED",
    "UNVERIFIED",
    "VALIDATED",
    "VERIFIED",
    "EvidenceRef",
    "Gap",
    "Matrix",
    "SetVerdict",
    "Verdict",
    "build_matrix",
    "classify",
    "find_gaps",
    "is_stale",
    "route_for",
    "verdict_from_members",
]

VERIFIED = "VERIFIED"
UNDER_VERIFIED = "UNDER-VERIFIED"
FAILED = "FAILED"
UNVERIFIED = "UNVERIFIED"
PARTIAL = "PARTIAL"
INCOMPLETE = "INCOMPLETE"
INVALID = "INVALID"
VALIDATED = "VALIDATED"
UNVALIDATED = "UNVALIDATED"
MITIGATED = "MITIGATED"
OPEN = "OPEN"

ROUTE_AUTONOMOUS = "autonomous"
ROUTE_HUMAN = "human-gate"

BASIS_OWN = "own"
BASIS_DERIVED = "derived"
BASIS_BOTH = "own+derived"


@dataclass
class EvidenceRef:
    """One piece of evidence as it applies to one entity: a view of one owned
    member of its verification set (kept for 0.3.x readers of ``evidence``)."""

    name: str
    status: str
    level: str
    target: str = ""
    source: str = ""
    message: str = ""
    stale: bool = False
    kind: str = "case"  # always "case" since 0.3 (whole-target claims expand to their cases)

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {"name": self.name, "status": self.status, "level": self.level}
        if self.target:
            out["target"] = self.target
        if self.kind != "case":
            out["kind"] = self.kind
        if self.stale:
            out["stale"] = True
        if self.message and self.status in ("failed", "error"):
            out["message"] = self.message.splitlines()[0][:300]
        return out


@dataclass
class Verdict:
    """The traceability state of one entity."""

    id: str
    kind: str
    status: str
    demanded: str = ""  # requirements only
    provided: str = ""  # best passing level
    stale: bool = False
    pyramid_violation: bool = False
    # A view of the owned members (0.3.x compatibility): the cases it owns, with their states.
    evidence: list[EvidenceRef] = field(default_factory=list)
    implemented_in: list[Reference] = field(default_factory=list)
    verified_in: list[Reference] = field(default_factory=list)
    # Added in v0.3.
    members: tuple[Member, ...] = ()  # its verification set (Attribution.members_of)
    basis: str = ""  # own | derived | own+derived
    derived_from: list[str] = field(default_factory=list)  # the entities whose verdicts it rolls up
    flaky: bool = False  # a passed member needed a retry
    mixed_builds: tuple[str, ...] = ()  # artifact keys its passed members disagree on
    reasons: tuple[str, ...] = ()  # why UNDER-VERIFIED: stale, flaky, level


@dataclass
class Gap:
    """Something the model or its evidence is missing — a unit of work."""

    kind: str
    entity: str
    message: str
    route: str = ROUTE_AUTONOMOUS
    demanded: str = ""
    provided: str = ""

    def to_dict(self) -> dict[str, str]:
        out = {"kind": self.kind, "entity": self.entity, "message": self.message, "route": self.route}
        if self.demanded:
            out["demanded_level"] = self.demanded
        if self.provided:
            out["provided_level"] = self.provided
        return out


@dataclass
class Matrix:
    model: Model
    verdicts: dict[str, Verdict]
    gaps: list[Gap]
    evidence: Evidence
    unknown_evidence: dict[str, list[str]] = field(default_factory=dict)  # id -> case keys
    annotations_scanned: bool = False
    # Added in v0.3: who owns each case, and every entity's verification set.
    attribution: Attribution | None = None

    def status(self, entity_id: str) -> str:
        return self.verdicts[entity_id].status

    def of_kind(self, kind: str) -> list[Verdict]:
        return sorted((v for v in self.verdicts.values() if v.kind == kind), key=lambda v: natural_key(v.id))

    def high_open_risks(self) -> list[str]:
        c = self.model.config
        return [
            r.id
            for r in sorted(self.model.risks.values(), key=lambda r: natural_key(r.id))
            if r.severity in c.high_severities and self.status(r.id) != MITIGATED
        ]

    def pyramid_violations(self) -> list[str]:
        return [v.id for v in self.of_kind(cfg.REQUIREMENT) if v.pyramid_violation]

    def module_status(self) -> dict[str, str]:
        by_module: dict[str, list[str]] = {}
        for req in self.model.requirements.values():
            for mod in req.modules:
                by_module.setdefault(mod, []).append(self.status(req.id))
        return {m: _rollup(s, VERIFIED, UNVERIFIED) for m, s in sorted(by_module.items())}

    def counts(self) -> dict[str, int]:
        def n(kind: str, status: str | None = None) -> int:
            return sum(1 for v in self.of_kind(kind) if status is None or v.status == status)

        return {
            "user_needs": n(cfg.USER_NEED),
            "user_needs_validated": n(cfg.USER_NEED, VALIDATED),
            "requirements": n(cfg.REQUIREMENT),
            "requirements_verified": n(cfg.REQUIREMENT, VERIFIED),
            "requirements_under_verified": n(cfg.REQUIREMENT, UNDER_VERIFIED),
            "requirements_partial": n(cfg.REQUIREMENT, PARTIAL),
            "requirements_failed": n(cfg.REQUIREMENT, FAILED),
            "requirements_unverified": n(cfg.REQUIREMENT, UNVERIFIED),
            "requirements_incomplete": n(cfg.REQUIREMENT, INCOMPLETE),
            "requirements_invalid": n(cfg.REQUIREMENT, INVALID),
            "risks": n(cfg.RISK),
            "risks_mitigated": n(cfg.RISK, MITIGATED),
            "mitigations": n(cfg.MITIGATION),
            "mitigations_verified": n(cfg.MITIGATION, VERIFIED),
            "test_cases": len(self.evidence.cases),
            "gaps": len(self.gaps),
        }


# --------------------------------------------------------------------------- #
# Classification helpers                                                      #
# --------------------------------------------------------------------------- #


def _best(levels: Iterable[str], c: cfg.Config) -> tuple[int | None, str]:
    """(best ordered rank, its name); an unordered level only if nothing ranks."""
    best_rank: int | None = None
    best_name = ""
    unordered = ""
    for name in levels:
        rank = c.rank(name)
        if rank is None:
            unordered = unordered or name
        elif best_rank is None or rank > best_rank:
            best_rank, best_name = rank, name
    if best_rank is None:
        return None, unordered
    return best_rank, best_name


def classify(passed_levels: list[str], failed: bool, demanded: str, c: cfg.Config) -> tuple[str, str]:
    """(status, best provided level) for passing levels against a demand."""
    if failed:
        return FAILED, ""
    if not passed_levels:
        return UNVERIFIED, ""
    best_rank, best_name = _best(passed_levels, c)
    if not demanded:  # ungraded (direct validation evidence): any pass counts
        return VERIFIED, best_name
    want = c.rank(demanded)
    if want is None:  # unordered demand (inspection): met only by the same level
        return (VERIFIED if demanded in passed_levels else UNDER_VERIFIED), (
            demanded if demanded in passed_levels else best_name
        )
    if best_rank is not None and best_rank >= want:
        return VERIFIED, best_name
    return UNDER_VERIFIED, best_name


def _rollup(statuses: list[str], all_ok: str, none: str) -> str:
    """One verdict from other entities' verdicts: INVALID rolls up like
    FAILED, INCOMPLETE like PARTIAL."""
    if not statuses:
        return none
    if FAILED in statuses or INVALID in statuses:
        return FAILED
    ok = (VERIFIED, VALIDATED, MITIGATED)
    if all(s in ok for s in statuses):
        return all_ok
    if any(s in ok or s in (UNDER_VERIFIED, PARTIAL, INCOMPLETE) for s in statuses):
        return PARTIAL
    return none


def route_for(level: str, c: cfg.Config) -> str:
    """<= autonomous_max_level: an agent can close it; else a human/bench gate."""
    rank = c.rank(level)
    limit = c.rank(c.autonomous_max_level)
    if rank is not None and limit is not None and rank <= limit:
        return ROUTE_AUTONOMOUS
    return ROUTE_HUMAN


# --------------------------------------------------------------------------- #
# One verification set                                                        #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SetVerdict:
    """What :func:`verdict_from_members` concludes about one verification set."""

    status: str
    provided: str = ""
    stale: bool = False
    flaky: bool = False
    mixed_builds: tuple[str, ...] = ()
    pyramid_violation: bool = False
    reasons: tuple[str, ...] = ()  # UNDER-VERIFIED: stale | flaky | level


def _mixed_builds(members: Sequence[Member]) -> tuple[str, ...]:
    """Artifact keys on which the passed members' stamps disagree (unstamped never conflict)."""
    values: dict[str, set[str]] = {}
    for m in members:
        if m.state != PASSED or m.result is None:
            continue
        stamps = m.result.artifacts or ((m.result.artifact,) if m.result.artifact else ())
        for stamp in stamps:
            for k, v in stamp.items():
                values.setdefault(k, set()).add(str(v))
    return tuple(sorted(k for k, vs in values.items() if len(vs) > 1))


def _pyramid(levels: list[str], demanded: str, c: cfg.Config) -> bool:
    """A physical result (>= pyramid_min_level) for a physical demand, with no
    cheap evidence backing it (computed over the passed members)."""
    want = c.rank(demanded)
    floor = c.rank(c.pyramid_min_level) if c.pyramid_min_level else None
    if want is None or floor is None or want < floor:
        return False
    physical = [lvl for lvl in levels if (c.rank(lvl) or 0) >= floor]
    # A violation needs physical evidence standing alone; lower evidence
    # without any physical result is UNDER-VERIFIED, not this.
    return bool(physical) and not any(lvl in c.pyramid_cheap_levels for lvl in levels)


def verdict_from_members(members: Sequence[Member], demanded: str, config: cfg.Config) -> SetVerdict:
    """The verdict of one verification set (the first rule that applies):

    1. a member is quarantined → INVALID;
    2. a member failed or errored (a taint included), or a passed member was
       flaky under ``flaky: fail`` → FAILED;
    3. no members, or none of them ran → UNVERIFIED;
    4. a member is missing, not run, skipped or moved, or the set mixes
       builds under ``set_consistency: enforce`` → INCOMPLETE (``provided``
       is the best passed level so far);
    5. a member is stale, flaky under ``flaky: under-verify``, or the best
       level is below ``demanded`` → UNDER-VERIFIED;
    6. otherwise → VERIFIED, ``provided`` the best level of the set.
    """
    c = config
    states = [m.state for m in members]
    passed = [m for m in members if m.state == PASSED]
    levels = [m.level for m in passed]
    stale = any(m.stale for m in passed)
    flaky = any(m.flaky for m in passed)
    mixed = _mixed_builds(members) if c.set_consistency != "off" else ()
    pyramid = _pyramid(levels, demanded, c)
    best = _best(levels, c)[1]
    if QUARANTINED in states:
        return SetVerdict(INVALID, "", False, flaky, mixed, pyramid)
    if any(s in (FAILED_CASE, ERROR) for s in states) or (flaky and c.flaky == "fail"):
        return SetVerdict(FAILED, "", False, flaky, mixed, pyramid)
    if not members or all(s == NOT_RUN for s in states):
        return SetVerdict(UNVERIFIED, "", False, flaky, mixed, pyramid)
    if any(s in (MISSING, NOT_RUN, SKIPPED, MOVED) for s in states) or (mixed and c.set_consistency == "enforce"):
        return SetVerdict(INCOMPLETE, best, stale, flaky, mixed, pyramid)
    reasons: list[str] = []
    if stale:
        reasons.append("stale")
    if flaky and c.flaky == "under-verify":
        reasons.append("flaky")
    status, provided = classify(levels, False, demanded, c)
    if status != VERIFIED:
        reasons.append("level")
    if reasons:
        return SetVerdict(UNDER_VERIFIED, provided, stale, flaky, mixed, pyramid, tuple(reasons))
    return SetVerdict(VERIFIED, provided, False, flaky, mixed, pyramid)


def _evidence_view(members: Iterable[Member]) -> list[EvidenceRef]:
    """The owned members as 0.2-style evidence references."""
    refs = []
    for m in members:
        if not m.owned or m.key is None or m.result is None:
            continue
        res = m.result
        refs.append(
            EvidenceRef(
                name=m.key.path,
                status=m.state,
                level=m.level,
                target=m.key.target,
                source=res.sources[0] if res.sources else "",
                message=m.reason if m.reason and m.state == ERROR and res.status != ERROR else res.message,
                stale=m.stale,
            )
        )
    refs.sort(key=lambda r: (r.target, r.name, r.kind))
    return refs


# --------------------------------------------------------------------------- #
# The matrix                                                                  #
# --------------------------------------------------------------------------- #


def build_matrix(
    model: Model,
    evidence: Evidence,
    current_build: Mapping[str, str] | None = None,
    references: list[Reference] | None = None,
    *,
    lock: rr_lock.Lock | None = None,
) -> Matrix:
    """Compute a :class:`Verdict` for every entity and the list of gaps.

    Always runs :func:`~rules_requirements.attribution.attribute` and checks
    its invariant: verdicts read only the attribution's verification sets,
    never the evidence's tags. ``lock`` is the verification-set lock; without
    one, the lock ``config.sets_lock`` names is read (a lock that cannot be
    read is a ``lock-invalid`` issue, and the sets are not pinned).

    ``references`` (from :func:`rules_requirements.annotations.scan`) adds
    implementation/verification source links and the ``no-implementation``
    gap; omit it to trace from test evidence alone. They are documentation:
    they never add a member.
    """
    c = model.config
    problem = ""
    if lock is None:
        try:
            lock = rr_lock.configured_lock(model)
        except rr_lock.LockError as exc:
            problem = str(exc)
    attribution = attribute(model, evidence, current_build=current_build, lock=lock)
    if problem:
        issue = AttributionIssue("lock-invalid", f"{problem}; the verification sets are not pinned", "error")
        attribution = replace(attribution, issues=(*attribution.issues, issue))
    attribution.check_invariant()

    verdicts: dict[str, Verdict] = {}

    def own(entity_id: str, kind: str, demanded: str) -> Verdict:
        members = attribution.members_of(entity_id)
        sv = verdict_from_members(members, demanded, c)
        return Verdict(
            id=entity_id,
            kind=kind,
            status=sv.status,
            demanded=demanded,
            provided=sv.provided,
            stale=sv.stale,
            pyramid_violation=sv.pyramid_violation,
            evidence=_evidence_view(members),
            members=members,
            basis=BASIS_OWN,
            flaky=sv.flaky,
            mixed_builds=sv.mixed_builds,
            reasons=sv.reasons,
        )

    # Requirements, bottom-up through `refines`.
    visiting: set[str] = set()

    def req_verdict(req: Requirement) -> Verdict:
        if req.id in verdicts:
            return verdicts[req.id]
        v = own(req.id, cfg.REQUIREMENT, model.demanded_level(req))
        visiting.add(req.id)
        kids = [req_verdict(k) for k in model.children(req.id) if k.id not in visiting]
        visiting.discard(req.id)
        if kids:
            _refine(v, kids, c)
        verdicts[req.id] = v
        return v

    for req in sorted(model.requirements.values(), key=lambda r: natural_key(r.id)):
        req_verdict(req)

    # User needs and mitigations: their own sets (validated_by / verified_by
    # claims, tags) join the rollup as one more child; level is not graded.
    def with_own(entity_id: str, kind: str, derived: list[str], all_ok: str, none: str) -> Verdict:
        members = attribution.members_of(entity_id)
        v = own(entity_id, kind, "")
        statuses = [verdicts[d].status for d in derived]
        if members:
            statuses.append(v.status)
        v.status = INVALID if v.status == INVALID else _rollup(statuses, all_ok, none)
        v.provided = ""
        v.basis = (BASIS_BOTH if derived else BASIS_OWN) if members else BASIS_DERIVED
        v.derived_from = list(derived)
        verdicts[entity_id] = v
        return v

    for un in model.user_needs.values():
        with_own(un.id, cfg.USER_NEED, [r.id for r in model.requirements_for_need(un.id)], VALIDATED, UNVALIDATED)
    for mit in model.mitigations.values():
        with_own(mit.id, cfg.MITIGATION, [r for r in mit.implemented_by if r in verdicts], VERIFIED, UNVERIFIED)
    for risk in model.risks.values():
        mits = model.mitigations_for_risk(risk.id)
        verdicts[risk.id] = Verdict(
            id=risk.id,
            kind=cfg.RISK,
            status=_rollup([verdicts[m.id].status for m in mits], MITIGATED, OPEN),
            basis=BASIS_DERIVED,
            derived_from=[m.id for m in mits],
        )
    for tm in model.test_methods.values():
        users = model.requirements_for_method(tm.id)
        verdicts[tm.id] = Verdict(
            id=tm.id,
            kind=cfg.TEST_METHOD,
            status=_rollup([verdicts[r.id].status for r in users], VERIFIED, UNVERIFIED),
            basis=BASIS_DERIVED,
            derived_from=[r.id for r in users],
        )

    if references is not None:
        for ref in references:
            for rid in ref.ids:
                found = verdicts.get(rid)
                if found is None:
                    continue
                (found.verified_in if ref.relation == "verifies" else found.implemented_in).append(ref)

    unknown: dict[str, list[str]] = {}
    for issue in attribution.issues:
        if issue.code == "unknown-id" and issue.key is not None:
            for rid in issue.entities:
                unknown.setdefault(rid, []).append(str(issue.key))
    matrix = Matrix(
        model=model,
        verdicts=verdicts,
        gaps=[],
        evidence=evidence,
        unknown_evidence={
            rid: sorted(set(keys), key=natural_key)
            for rid, keys in sorted(unknown.items(), key=lambda kv: natural_key(kv[0]))
        },
        annotations_scanned=references is not None,
        attribution=attribution,
    )
    matrix.gaps = find_gaps(matrix)
    return matrix


def _refine(v: Verdict, kids: list[Verdict], c: cfg.Config) -> None:
    """Fold the children's verdicts into a requirement's own (``refines``)."""
    has_own = bool(v.members)
    child = _rollup([k.status for k in kids], VERIFIED, UNVERIFIED)
    own = v.status
    v.basis = BASIS_BOTH if has_own else BASIS_DERIVED
    v.derived_from = [k.id for k in kids]
    if own == INVALID:  # a quarantined case names it: that is its own verdict
        return
    if FAILED in (own, child):
        v.status = FAILED
        return
    if has_own and own in (INCOMPLETE, UNVERIFIED):
        # A parent with claims of its own needs its own set complete too.
        v.status = UNVERIFIED if (own == UNVERIFIED and child == UNVERIFIED) else INCOMPLETE
        return
    if child == VERIFIED:
        # Verified refinements carry the parent only if their rigor meets
        # the parent's own demand: a hitl system requirement is not proven
        # by simulation-verified software requirements. A stale or flaky
        # own set is not made good by the children.
        want = c.rank(v.demanded)
        ranks = [c.rank(k.provided) for k in kids]
        meets = own == VERIFIED or (want is not None and all(r is not None and r >= want for r in ranks))
        held_back = own == UNDER_VERIFIED and bool({"stale", "flaky"} & set(v.reasons))
        weakest = min(kids, key=lambda k: c.rank(k.provided) or 0).provided
        if meets and not held_back:
            if own != VERIFIED:
                # Verified through the refinements, whose own verdicts already
                # weighed staleness and the cost pyramid.
                v.provided, v.stale, v.pyramid_violation, v.reasons = weakest, False, False, ()
            v.status = VERIFIED
        else:
            v.status = UNDER_VERIFIED
            v.provided = v.provided or weakest
            v.reasons = v.reasons or ("level",)
    elif own in (VERIFIED, UNDER_VERIFIED) or child == PARTIAL:
        v.status = PARTIAL


# --------------------------------------------------------------------------- #
# Gaps                                                                        #
# --------------------------------------------------------------------------- #


def _names(members: Iterable[Member]) -> str:
    return ", ".join(m.key.path if m.key is not None else f"{m.target} {m.selector}" for m in members)


def _breakdown(members: Sequence[Member], mixed: tuple[str, ...] = ()) -> str:
    """``17/23 passed; 6 not run (//pi/hitl/harness:e2e_netstack)``."""
    parts = [f"{sum(m.state == PASSED for m in members)}/{len(members)} passed"]
    for state, label in ((MISSING, "missing"), (NOT_RUN, "not run"), (SKIPPED, "skipped"), (MOVED, "moved")):
        those = [m for m in members if m.state == state]
        if those:
            targets = sorted({m.target for m in those}, key=natural_key)
            parts.append(f"{len(those)} {label} ({', '.join(targets)})")
    if mixed:
        parts.append(f"mixed builds ({', '.join(mixed)})")
    return "; ".join(parts)


def _member_route(members: Iterable[Member], c: cfg.Config) -> str:
    """human-gate when any of ``members`` needs a level above autonomous_max_level."""
    return ROUTE_HUMAN if any(route_for(m.level, c) == ROUTE_HUMAN for m in members) else ROUTE_AUTONOMOUS


def _set_gaps(v: Verdict, route: str, c: cfg.Config, matrix: Matrix) -> list[Gap]:
    """The gaps an entity's verdict and verification set call for."""
    gaps: list[Gap] = []
    members = v.members
    if v.status == INVALID:
        bad = [m for m in members if m.state == QUARANTINED]
        listed = ", ".join(f"{m.key} ({m.reason})" for m in bad)
        gaps.append(
            Gap(
                "invalid",
                v.id,
                f"quarantined case(s) name it: {listed}; it verifies nothing through them until each has one owner",
                ROUTE_AUTONOMOUS,
                v.demanded,
            )
        )
    elif v.status == FAILED:
        failing = [m for m in members if m.state in (FAILED_CASE, ERROR)]
        parts = []
        if failing:
            parts.append("failing evidence: " + _names(failing))
        if v.flaky and c.flaky == "fail":
            parts.append("passed only on a retry (config flaky: fail): " + _names(m for m in members if m.flaky))
        if not parts:
            kids = [d for d in v.derived_from if matrix.verdicts[d].status in (FAILED, INVALID)]
            parts.append("failing refinements: " + ", ".join(kids))
        # Reproducing a failure needs the same rig the evidence ran on.
        gaps.append(Gap("failed", v.id, "; ".join(parts), route, v.demanded))
    elif v.status == INCOMPLETE:
        open_ = [m for m in members if m.state in (NOT_RUN, SKIPPED)]
        gaps.append(
            Gap(
                "incomplete",
                v.id,
                _breakdown(members, v.mixed_builds if c.set_consistency == "enforce" else ()),
                _member_route(open_, c),
                v.demanded,
                v.provided,
            )
        )
    elif v.status == UNVERIFIED:
        if members and all(m.state == NOT_RUN for m in members):
            targets = sorted({m.target for m in members}, key=natural_key)
            gaps.append(
                Gap("unverified", v.id, "not run: " + ", ".join(targets), _member_route(members, c), v.demanded)
            )
        else:
            gaps.append(Gap("unverified", v.id, "no test evidence", route, v.demanded))
    elif v.status == UNDER_VERIFIED:
        if "stale" in v.reasons:
            stale = [m for m in members if m.stale]
            gaps.append(
                Gap(
                    "stale",
                    v.id,
                    "passing evidence recorded against another build than the current one: " + _names(stale),
                    route,
                    v.demanded,
                    v.provided,
                )
            )
        if "flaky" in v.reasons:
            gaps.append(_flaky_gap(v, route))
        if "level" in v.reasons or not v.reasons:
            gaps.append(
                Gap(
                    "under-verified",
                    v.id,
                    f"demands {v.demanded}, best passing evidence is {v.provided}",
                    route,
                    v.demanded,
                    v.provided,
                )
            )
    elif v.status == PARTIAL and v.kind == cfg.REQUIREMENT:
        gaps.append(Gap("partial", v.id, "refinements are only partly verified", route, v.demanded))
    if v.flaky and c.flaky == "flag" and v.status not in (INVALID, FAILED):
        gaps.append(_flaky_gap(v, route))
    if v.mixed_builds and c.set_consistency == "warn" and v.status not in (INVALID, FAILED):
        gaps.append(
            Gap(
                "mixed-builds",
                v.id,
                f"its passed members were stamped with different builds ({', '.join(v.mixed_builds)}); "
                "the set should pass together on one build",
                route,
                v.demanded,
            )
        )
    return gaps


def _flaky_gap(v: Verdict, route: str) -> Gap:
    flaky = [m for m in v.members if m.flaky and m.state == PASSED]
    return Gap(
        "flaky",
        v.id,
        "passed only on a retry: " + _names(flaky),
        route,
        v.demanded,
        v.provided,
    )


class _Nearest:
    """The nearest case path a target reported, for ``missing-case`` hints:
    a renamed test, usually. difflib is quadratic in the path length, so the
    hints of one report are bounded: candidates from the same class or
    module first and at most :attr:`POOL` of them, at most :attr:`BUDGET`
    hints in all (later gaps carry none)."""

    POOL = 100
    BUDGET = 200

    def __init__(self, attribution: Attribution | None) -> None:
        self.paths: dict[str, dict[str, list[str]]] = {}
        for key in attribution.cases if attribution is not None else ():
            groups = self.paths.setdefault(key.target, {})
            groups.setdefault("", []).append(key.path)
            groups.setdefault(key.path.rpartition("::")[0] + "::", []).append(key.path)
        self.left = self.BUDGET

    def __call__(self, target: str, wanted: str) -> str | None:
        import difflib

        groups = self.paths.get(target)
        if not groups or self.left <= 0:
            return None
        self.left -= 1
        pool = groups.get(wanted.rpartition("::")[0] + "::") or groups[""]
        if len(pool) > self.POOL:
            pool = sorted(pool, key=lambda p: abs(len(p) - len(wanted)))[: self.POOL]
        near = difflib.get_close_matches(wanted, pool, n=1, cutoff=0.5)
        return near[0] if near else None


def _missing_gaps(v: Verdict, nearest: _Nearest) -> list[Gap]:
    """``missing-case``: a selector or lock entry whose case the target did not
    report, with the nearest case it did report."""
    gaps = []
    for m in v.members:
        if m.state != MISSING:
            continue
        wanted = m.key.path if m.key is not None else m.selector
        near = nearest(m.target, wanted) if m.key is not None else None  # a glob or whole claim names no case
        what = (
            "lock entry" if m.via == "lock" else ("whole-target claim" if m.selector == WHOLE_SELECTOR else "selector")
        )
        hint = f"; nearest existing case: {near!r}" if near else ""
        gaps.append(
            Gap(
                "missing-case",
                v.id,
                f"{what} {wanted!r} of {m.target} matched no case ({m.reason}){hint}",
                ROUTE_AUTONOMOUS,
            )
        )
    return gaps


def _issue_entity(issue: AttributionIssue) -> str:
    if issue.entities:
        return issue.entities[0]
    if issue.key is not None:
        return issue.key.target
    return issue.target


# Attribution issues gathered into one gap per entity instead of one per issue.
_AGGREGATED_ISSUES = ("misdirected-evidence", "unknown-id")


def find_gaps(matrix: Matrix) -> list[Gap]:
    """Everything that stands between the model and a complete V&V argument."""
    m, c = matrix.model, matrix.model.config
    att = matrix.attribution
    gaps: list[Gap] = []
    nearest = _Nearest(att)
    # Quarantined cases first: each lists every claim's origin.
    for q in att.quarantined if att is not None else ():
        gaps.append(Gap(q.code, str(q.key), q.detail, ROUTE_AUTONOMOUS))
    for v in matrix.of_kind(cfg.REQUIREMENT):
        route = route_for(v.demanded, c)
        gaps.extend(_set_gaps(v, route, c, matrix))
        if v.pyramid_violation and v.status not in (INVALID, FAILED):
            gaps.append(
                Gap(
                    "pyramid",
                    v.id,
                    f"demands {v.demanded} but has no {'/'.join(c.pyramid_cheap_levels)} evidence "
                    "backing the physical result",
                    ROUTE_AUTONOMOUS,
                    v.demanded,
                    v.provided,
                )
            )
        gaps.extend(_missing_gaps(v, nearest))
        if matrix.annotations_scanned and not v.implemented_in and not m.children(v.id):
            gaps.append(Gap("no-implementation", v.id, "no source annotation implements it", ROUTE_AUTONOMOUS))
    for kind in (cfg.USER_NEED, cfg.MITIGATION):
        for v in matrix.of_kind(kind):
            if not v.members:
                continue
            own = verdict_from_members(v.members, "", c)
            gaps.extend(_set_gaps(replace(v, status=own.status), ROUTE_AUTONOMOUS, c, matrix))
            gaps.extend(_missing_gaps(v, nearest))
    issues = att.issues if att is not None else ()
    misdirected: dict[str, list[str]] = {}
    for issue in issues:
        if issue.code == "misdirected-evidence" and issue.key is not None:
            for rid in issue.entities:
                misdirected.setdefault(rid, []).append(str(issue.key))
    for kind in (cfg.RISK, cfg.TEST_METHOD):
        for ent in m.section(kind).values():
            keys = misdirected.get(ent.id)
            if keys:
                gaps.append(
                    Gap(
                        "misdirected-evidence",
                        ent.id,
                        f"tests tagged with a {kind.replace('_', ' ')} id verify nothing (tag the requirement "
                        f"instead): {', '.join(sorted(set(keys), key=natural_key))}",
                        ROUTE_AUTONOMOUS,
                    )
                )
    if att is not None:
        gaps.extend(_unattributed(att))
        # Every other attribution issue is a unit of work: a gap each (none
        # may be silent, e.g. same-path-multiple-owners, the only defence
        # when one test code in two targets has no recorded source file).
        for issue in issues:
            if issue.code not in _AGGREGATED_ISSUES:
                gaps.append(Gap(issue.code, _issue_entity(issue), issue.message, ROUTE_AUTONOMOUS))
        if att.lock is None:
            unpinned = [ent for ent, members in att.members.items() if any(_unpinned(member) for member in members)]
            if unpinned:
                gaps.append(
                    Gap(
                        "unpinned-sets",
                        "",
                        "membership not pinned (no config.sets_lock): a deleted test would go unnoticed in the "
                        f"sets of {', '.join(sorted(unpinned, key=natural_key))}; lock them with "
                        "`rr sets lock --write`",
                        ROUTE_AUTONOMOUS,
                    )
                )
    for rid in matrix.high_open_risks():
        risk = m.risks[rid]
        gaps.append(
            Gap(
                "high-risk-open",
                rid,
                f"{risk.severity} risk; mitigation is {matrix.status(rid)}",
                ROUTE_HUMAN,
            )
        )
    # Open notes are work items too: an agent's gap finding, a reviewer's
    # question or a TODO recorded on an entity drives the next cycle.
    for ent in m.entities():
        for note in ent.notes:
            if note.status == "open" and note.kind in ("gap", "todo", "question"):
                nv = matrix.verdicts.get(ent.id)
                route = (
                    ROUTE_HUMAN
                    if note.kind == "question"
                    else route_for(nv.demanded, c)
                    if nv and nv.demanded
                    else ROUTE_AUTONOMOUS
                )
                gaps.append(Gap(f"note:{note.kind}", ent.id, note.text.splitlines()[0][:200], route))
    for rid, cases in matrix.unknown_evidence.items():
        gaps.append(
            Gap("unknown-id", rid, f"evidence references an undefined id: {', '.join(cases)}", ROUTE_AUTONOMOUS)
        )
    return gaps


def _unpinned(member: Member) -> bool:
    """A member a deleted test would silently leave: tag-owned, or selected by a glob or a whole claim."""
    if member.state == QUARANTINED or member.via == "lock":
        return False
    if member.selector in (TAG_SELECTOR, WHOLE_SELECTOR):
        return True
    try:
        return not case_selectors.is_literal(member.selector)
    except case_selectors.BadSelector:
        return False


def _unattributed(att: Attribution) -> list[Gap]:
    """Failures no requirement owns: ``unattributed-failure`` (and, for
    0.3.x readers, the same as ``untraced-failure``)."""
    gaps = []
    for key, res in att.cases.items():
        if res.is_failure and key not in att.owner and att.quarantine_of(key) is None:
            gaps.append(Gap("unattributed-failure", key.target, f"{key} {res.status} and is owned by no requirement"))
            name = res.cases[0].full_name if res.cases else str(key)
            gaps.append(Gap("untraced-failure", key.target, f"{name} {res.status} and traces to no requirement"))
    affected = {m.target for ms in att.members.values() for m in ms if m.state == ERROR}
    for target, run in att.targets.items():
        if run.taint and target not in affected:
            gaps.append(
                Gap(
                    "unattributed-failure",
                    target,
                    f"{target}: {run.taint_message} (a failure of the whole target run; no member is affected)",
                )
            )
            for case in run.taint:
                gaps.append(
                    Gap("untraced-failure", target, f"{case.full_name} {case.status} and traces to no requirement")
                )
    return gaps
