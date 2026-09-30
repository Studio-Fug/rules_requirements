# SPDX-License-Identifier: AGPL-3.0-or-later
"""Join the model with test evidence (and optionally source annotations) into a
traceability matrix, and derive the gaps that remain.

Verification (IEC 62304 §5.5-5.7): is each **requirement** proven, *the way it
needs to be proven*?

* ``VERIFIED`` — passing evidence provides rigor >= the level the requirement
  demands (its ``method``).
* ``UNDER-VERIFIED`` — passing evidence exists, but all of it is below the
  demanded rigor (e.g. a ``hil`` requirement covered only by simulation), or
  the only passing evidence is *stale* (recorded against an older build).
* ``FAILED`` — any evidence for it failed or errored.
* ``UNVERIFIED`` — no evidence at all.
* ``PARTIAL`` — (decomposed requirements) its refinements are only partly
  verified.

Validation (design validation, IEC 62304 §5.1 / 21 CFR 820.30(g)): each **user
need** rolls up the requirements that satisfy it -> ``VALIDATED``, ``PARTIAL``,
``FAILED`` or ``UNVALIDATED``.

Risk control (ISO 14971 §7.2): each **mitigation** rolls up the requirements
that implement it (``VERIFIED``/``PARTIAL``/``FAILED``/``UNVERIFIED``) and each
**risk** rolls up its mitigations -> ``MITIGATED``, ``PARTIAL``, ``FAILED`` or
``OPEN``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping

from rules_requirements import config as cfg
from rules_requirements.annotations import Reference
from rules_requirements.ingest import Evidence, TestCase
from rules_requirements.model import Model, Requirement
from rules_requirements.util import natural_key

VERIFIED = "VERIFIED"
UNDER_VERIFIED = "UNDER-VERIFIED"
FAILED = "FAILED"
UNVERIFIED = "UNVERIFIED"
PARTIAL = "PARTIAL"
VALIDATED = "VALIDATED"
UNVALIDATED = "UNVALIDATED"
MITIGATED = "MITIGATED"
OPEN = "OPEN"

ROUTE_AUTONOMOUS = "autonomous"
ROUTE_HUMAN = "human-gate"


@dataclass
class EvidenceRef:
    """One piece of evidence as it applies to one entity."""

    name: str
    status: str
    level: str
    target: str = ""
    source: str = ""
    message: str = ""
    stale: bool = False
    kind: str = "case"  # "case" (per-testcase tag) | "target" (verified_by label)

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
    evidence: list[EvidenceRef] = field(default_factory=list)
    implemented_in: list[Reference] = field(default_factory=list)
    verified_in: list[Reference] = field(default_factory=list)


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
    unknown_evidence: dict[str, list[str]] = field(default_factory=dict)  # id -> case names
    annotations_scanned: bool = False

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
    """(status, best provided level) for one entity's own evidence."""
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


def is_stale(artifact: Mapping[str, str], current: Mapping[str, str] | None) -> bool:
    """Evidence is stale when it recorded an artifact identity that differs from
    the current build on any shared key. No identity (or no reference) -> fresh."""
    if not artifact or not current:
        return False
    return any(k in current and str(current[k]) != str(v) for k, v in artifact.items())


def _rollup(statuses: list[str], all_ok: str, none: str) -> str:
    if not statuses:
        return none
    if FAILED in statuses:
        return FAILED
    ok = (VERIFIED, VALIDATED, MITIGATED)
    if all(s in ok for s in statuses):
        return all_ok
    if any(s in ok or s in (UNDER_VERIFIED, PARTIAL) for s in statuses):
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
# The matrix                                                                  #
# --------------------------------------------------------------------------- #


def build_matrix(
    model: Model,
    evidence: Evidence,
    current_build: Mapping[str, str] | None = None,
    references: list[Reference] | None = None,
) -> Matrix:
    """Compute a :class:`Verdict` for every entity and the list of gaps.

    ``references`` (from :func:`rules_requirements.annotations.scan`) adds
    implementation/verification source links and the ``no-implementation``
    gap; omit it to trace from test evidence alone.
    """
    c = model.config
    by_id: dict[str, list[TestCase]] = {}
    by_target: dict[str, list[TestCase]] = {}
    for case in evidence.cases:
        for rid in case.requirements:
            by_id.setdefault(rid, []).append(case)
        if case.target:
            by_target.setdefault(case.target, []).append(case)

    def target_stale(target: str) -> bool:
        """A target's evidence is stale when its identity-stamped cases say so.

        One run is one binary, so a mismatched stamp is decisive unless another
        case in the target carries a matching one; unstamped cases (a helper
        test without RR_ARTIFACT) do not make stale evidence fresh."""
        if not current_build:
            return False
        stamped = [
            cs for cs in by_target.get(target, []) if cs.status == "passed" and set(cs.artifact) & set(current_build)
        ]
        return bool(stamped) and all(is_stale(cs.artifact, current_build) for cs in stamped)

    known = model.ids()
    unknown = {
        rid: sorted(case.full_name for case in cases)
        for rid, cases in sorted(by_id.items(), key=lambda kv: natural_key(kv[0]))
        if rid not in known
    }

    def own(entity_id: str, demanded: str, verified_by: Iterable[object] = ()) -> Verdict:
        refs: list[EvidenceRef] = []
        for case in by_id.get(entity_id, []):
            level = case.level or c.default_provided_level
            refs.append(
                EvidenceRef(
                    name=f"{case.classname}::{case.name}" if case.classname else case.name,
                    status=case.status,
                    level=level,
                    target=case.target,
                    source=case.source,
                    message=case.message,
                    stale=case.status == "passed" and is_stale(case.artifact, current_build),
                )
            )
        for vb in verified_by:
            target, level = vb.target, vb.level or c.default_provided_level  # type: ignore[attr-defined]
            status = evidence.target_status.get(target)
            if status is not None:
                refs.append(
                    EvidenceRef(
                        name=target,
                        status=status,
                        level=level,
                        target=target,
                        kind="target",
                        stale=status == "passed" and target_stale(target),
                    )
                )
        refs.sort(key=lambda r: (r.target, r.name, r.kind))
        failed = any(r.status in ("failed", "error") for r in refs)
        fresh = [r.level for r in refs if r.status == "passed" and not r.stale]
        stale = [r.level for r in refs if r.status == "passed" and r.stale]
        if fresh or failed or not stale:
            status, provided = classify(fresh, failed, demanded, c)
            stale_flag = False
        else:
            status, provided = UNDER_VERIFIED, _best(stale, c)[1]
            stale_flag = True
        pyramid = False
        want = c.rank(demanded)
        floor = c.rank(c.pyramid_min_level) if c.pyramid_min_level else None
        passed_all = fresh + stale
        physical = [lvl for lvl in passed_all if (c.rank(lvl) or 0) >= (floor or 0)]
        # A violation needs physical (>= floor) evidence standing alone; lower
        # evidence without any physical result is UNDER-VERIFIED, not this.
        if want is not None and floor is not None and want >= floor and physical:
            pyramid = not any(lvl in c.pyramid_cheap_levels for lvl in passed_all)
        return Verdict(
            id=entity_id,
            kind="",
            status=status,
            demanded=demanded,
            provided=provided,
            stale=stale_flag,
            pyramid_violation=pyramid,
            evidence=refs,
        )

    verdicts: dict[str, Verdict] = {}

    # Requirements, bottom-up through `refines`.
    visiting: set[str] = set()

    def req_verdict(req: Requirement) -> Verdict:
        if req.id in verdicts:
            return verdicts[req.id]
        v = own(req.id, model.demanded_level(req), req.verified_by)
        v.kind = cfg.REQUIREMENT
        visiting.add(req.id)
        kids = [req_verdict(k) for k in model.children(req.id) if k.id not in visiting]
        visiting.discard(req.id)
        if kids:
            child = _rollup([k.status for k in kids], VERIFIED, UNVERIFIED)
            if FAILED in (v.status, child):
                v.status = FAILED
            elif child == VERIFIED:
                # Verified refinements carry the parent only if their rigor
                # meets the parent's own demand: a hitl system requirement is
                # not proven by simulation-verified software requirements.
                want = c.rank(v.demanded)
                ranks = [c.rank(k.provided) for k in kids]
                meets = v.status == VERIFIED or (want is not None and all(r is not None and r >= want for r in ranks))
                weakest = min(kids, key=lambda k: c.rank(k.provided) or 0).provided
                if meets:
                    if v.status != VERIFIED:
                        # Verified through the refinements, whose own verdicts
                        # already weighed staleness and the cost pyramid.
                        v.provided, v.stale, v.pyramid_violation = weakest, False, False
                    v.status = VERIFIED
                else:
                    v.status = UNDER_VERIFIED
                    v.provided = v.provided or weakest
            elif v.status in (VERIFIED, UNDER_VERIFIED) or child == PARTIAL:
                v.status = PARTIAL
        verdicts[req.id] = v
        return v

    for req in sorted(model.requirements.values(), key=lambda r: natural_key(r.id)):
        req_verdict(req)

    # Directly-tagged evidence on needs/mitigations (e.g. a usability study
    # validating a need) counts alongside the rollup; level is not graded.
    def with_direct(entity_id: str, kind: str, statuses: list[str], all_ok: str, none: str) -> Verdict:
        direct = own(entity_id, "")
        if direct.evidence:
            statuses = statuses + [direct.status]
        v = Verdict(id=entity_id, kind=kind, status=_rollup(statuses, all_ok, none), evidence=direct.evidence)
        verdicts[entity_id] = v
        return v

    for un in model.user_needs.values():
        with_direct(
            un.id,
            cfg.USER_NEED,
            [verdicts[r.id].status for r in model.requirements_for_need(un.id)],
            VALIDATED,
            UNVALIDATED,
        )
    for mit in model.mitigations.values():
        with_direct(
            mit.id,
            cfg.MITIGATION,
            [verdicts[r].status for r in mit.implemented_by if r in verdicts],
            VERIFIED,
            UNVERIFIED,
        )
    for risk in model.risks.values():
        mits = model.mitigations_for_risk(risk.id)
        verdicts[risk.id] = Verdict(
            id=risk.id,
            kind=cfg.RISK,
            status=_rollup([verdicts[m.id].status for m in mits], MITIGATED, OPEN),
        )
    for tm in model.test_methods.values():
        users = [verdicts[r.id].status for r in model.requirements_for_method(tm.id)]
        verdicts[tm.id] = Verdict(id=tm.id, kind=cfg.TEST_METHOD, status=_rollup(users, VERIFIED, UNVERIFIED))

    if references is not None:
        for ref in references:
            for rid in ref.ids:
                v = verdicts.get(rid)
                if v is None:
                    continue
                (v.verified_in if ref.relation == "verifies" else v.implemented_in).append(ref)

    matrix = Matrix(
        model=model,
        verdicts=verdicts,
        gaps=[],
        evidence=evidence,
        unknown_evidence=unknown,
        annotations_scanned=references is not None,
    )
    matrix.gaps = find_gaps(matrix)
    return matrix


def find_gaps(matrix: Matrix) -> list[Gap]:
    """Everything that stands between the model and a complete V&V argument."""
    m, c = matrix.model, matrix.model.config
    gaps: list[Gap] = []
    for v in matrix.of_kind(cfg.REQUIREMENT):
        route = route_for(v.demanded, c)
        if v.status == FAILED:
            failing = [e.name for e in v.evidence if e.status in ("failed", "error")]
            # Reproducing a failure needs the same rig the evidence ran on.
            gaps.append(Gap("failed", v.id, "failing evidence: " + ", ".join(failing), route, v.demanded))
        elif v.stale:
            gaps.append(
                Gap("stale", v.id, "only passing evidence is against an older build", route, v.demanded, v.provided)
            )
        elif v.status == UNVERIFIED:
            gaps.append(Gap("unverified", v.id, "no test evidence", route, v.demanded))
        elif v.status == UNDER_VERIFIED:
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
        elif v.status == PARTIAL:
            gaps.append(Gap("partial", v.id, "refinements are only partly verified", route, v.demanded))
        if v.pyramid_violation:
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
        if matrix.annotations_scanned and not v.implemented_in and not m.children(v.id):
            gaps.append(Gap("no-implementation", v.id, "no source annotation implements it", ROUTE_AUTONOMOUS))
    for kind in (cfg.USER_NEED, cfg.MITIGATION):
        for v in matrix.of_kind(kind):
            failing = [e.name for e in v.evidence if e.status in ("failed", "error")]
            if failing:
                gaps.append(Gap("failed", v.id, "failing evidence: " + ", ".join(failing), ROUTE_AUTONOMOUS))
    for kind in (cfg.RISK, cfg.TEST_METHOD):
        for ent in m.section(kind).values():
            tagged = matrix.evidence.for_id(ent.id)
            if tagged:
                names = ", ".join(sorted({cs.full_name for cs in tagged}))
                gaps.append(
                    Gap(
                        "misdirected-evidence",
                        ent.id,
                        f"tests tagged with a {kind.replace('_', ' ')} id verify nothing (tag the requirement "
                        f"instead): {names}",
                        ROUTE_AUTONOMOUS,
                    )
                )
    covered_targets = {vb.target for req in m.requirements.values() for vb in req.verified_by}
    for cs in matrix.evidence.cases:
        if cs.is_failure and not cs.requirements and cs.target not in covered_targets:
            gaps.append(
                Gap(
                    "untraced-failure",
                    cs.target or cs.full_name,
                    f"{cs.full_name} {cs.status} and traces to no requirement",
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
                v = matrix.verdicts.get(ent.id)
                route = ROUTE_HUMAN if note.kind == "question" else route_for(v.demanded, c) if v and v.demanded else ROUTE_AUTONOMOUS
                gaps.append(Gap(f"note:{note.kind}", ent.id, note.text.splitlines()[0][:200], route))
    for rid, cases in matrix.unknown_evidence.items():
        gaps.append(
            Gap("unknown-id", rid, f"evidence references an undefined id: {', '.join(cases)}", ROUTE_AUTONOMOUS)
        )
    return gaps
