# SPDX-License-Identifier: AGPL-3.0-or-later
"""Structural, referential and coverage validation of a :class:`Model`.

Every finding is an :class:`Issue` with a stable ``code`` so tooling (CI, the
web editor, agents) can filter and link to documentation. Shape and reference
problems are always errors; coverage findings follow ``config.rules``.

The claim checks are the static half of "a test case verifies at most one
requirement": claims of two entities that can select one case are a
``shared-case`` error with a concrete witness case, and so are claims on two
targets declared to run the same test code (``same-code-multiple-owners``).
Neither can be configured off. The verification-set lock (``config.sets_lock``)
is checked against the claims too.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Collection

from rules_requirements import case_selectors, labels
from rules_requirements import config as cfg
from rules_requirements import lock as rr_lock
from rules_requirements.model import (
    Claim,
    Entity,
    Location,
    Mitigation,
    Model,
    Requirement,
    Risk,
    VerifiedBy,
    claim_items,
)
from rules_requirements.util import natural_key


@dataclass(frozen=True)
class Issue:
    severity: str  # "error" | "warning"
    code: str
    message: str
    entity: str = ""
    location: Location = field(default_factory=Location)

    def __str__(self) -> str:
        where = f"{self.location}: " if self.location.path else ""
        return f"{where}{self.severity}: [{self.code}] {self.message}"


class ValidationError(Exception):
    """The model has errors. Carries every issue, not just the first."""

    def __init__(self, issues: list[Issue]):
        self.issues = issues
        self.errors = [str(i) for i in issues]
        super().__init__("requirements model is invalid:\n  - " + "\n  - ".join(self.errors))


def validate(model: Model, strict: bool = False, known_targets: Collection[str] | None = None) -> list[Issue]:
    """All issues in ``model``. ``strict`` promotes warnings to errors.

    ``known_targets`` (the labels ``bazel query 'tests(//...)'`` prints, in
    any spelling) makes a claim, a ``config.variants`` entry or a lock
    target naming any other label an ``unknown-target`` error;
    pseudo-targets (``suite:``, ``record:``) are exempt.
    """
    v = _Validator(model, known_targets)
    v.run()
    issues = v.issues
    if strict:
        issues = [
            Issue("error", i.code, i.message, i.entity, i.location) if i.severity == "warning" else i for i in issues
        ]
    return sorted(
        issues,
        key=lambda i: (i.severity != "error", i.location.path, i.location.line, natural_key(i.entity)),
    )


class _Validator:
    def __init__(self, model: Model, known_targets: Collection[str] | None = None):
        self.m = model
        self.c = model.config
        self.known: set[str] | None = None
        if known_targets is not None:
            norm = (labels.try_normalize(label, self.c.main_repo) for label in known_targets)
            self.known = {n for n in norm if n}
        self.issues: list[Issue] = []

    def add_at(self, code: str, message: str, entity: str, location: Location, severity: str = "error") -> None:
        self.issues.append(Issue(severity, code, message, entity, location))

    def unknown(self, target: str) -> bool:
        """Whether ``--known-targets`` was given and does not list ``target``
        (normalized; pseudo-targets are never unknown)."""
        return self.known is not None and not labels.is_pseudo(target) and target not in self.known

    def rule_at(self, name: str, message: str, entity: str, location: Location) -> None:
        sev = self.c.rule(name)
        if sev != "off":
            self.add_at(name, message, entity, location, sev)

    def add(self, code: str, message: str, ent: Entity | None = None, severity: str = "error") -> None:
        self.issues.append(
            Issue(
                severity,
                code,
                message,
                ent.id if ent else "",
                ent.location if ent else Location(),
            )
        )

    def rule(self, name: str, message: str, ent: Entity | None) -> None:
        sev = self.c.rule(name)
        if sev != "off":
            self.add(name, message, ent, sev)

    def run(self) -> None:
        for err in self.m.parse_errors:
            self.issues.append(Issue("error", "shape", err))
        for msg in self.m.unknown_fields:
            self.rule("unknown-field", msg, None)
        for ent in self.m.entities():
            self.check_common(ent)
        for req in self.m.requirements.values():
            self.check_requirement(req)
        for risk in self.m.risks.values():
            self.check_risk(risk)
        for mit in self.m.mitigations.values():
            self.check_mitigation(mit)
        for tm in self.m.test_methods.values():
            if tm.level and self.c.level(tm.level) is None:
                self.add("bad-level", f"{tm.id}: unknown level {tm.level!r}", tm)
            if not tm.level:
                self.add("missing-level", f"{tm.id}: a test method must declare its level", tm)
        self.check_refines_cycles()
        for un in self.m.user_needs.values():
            if not self.m.requirements_for_need(un.id):
                self.rule("need-unsatisfied", f"{un.id}: no requirement satisfies this need", un)
        self.check_claims()
        self.check_lock()

    # --- per-kind checks -----------------------------------------------------

    def check_common(self, ent: Entity) -> None:
        if not self.c.id_regex(ent.kind).match(ent.id):
            pattern = self.c.id_pattern.replace("{prefix}", self.c.prefix(ent.kind))
            self.add("bad-id", f"{ent.id}: {ent.kind} ids must match {pattern}", ent)
        if ent.status and ent.status not in cfg.STATUSES:
            self.add("bad-status", f"{ent.id}: status must be one of {cfg.STATUSES}", ent)

    def ref(self, ent: Entity, relation: str, target: str, kind: str) -> bool:
        section = self.m.section(kind)
        if target in section:
            return True
        other = self.m.get(target)
        if other is not None:
            self.add(
                "bad-reference",
                f"{ent.id}: {relation} {target}, which is a {other.kind}, not a {kind}",
                ent,
            )
        else:
            self.add("dangling-reference", f"{ent.id}: {relation} unknown {kind} {target}", ent)
        return False

    def check_requirement(self, req: Requirement) -> None:
        for un in req.satisfies:
            self.ref(req, "satisfies", un, cfg.USER_NEED)
        for parent in req.refines:
            if parent == req.id:
                self.add("bad-reference", f"{req.id}: refines itself", req)
            else:
                self.ref(req, "refines", parent, cfg.REQUIREMENT)
        if req.method:
            is_tm = req.method in self.m.test_methods
            is_level = self.c.level(req.method) is not None
            if not is_tm and not is_level:
                self.add(
                    "bad-method",
                    f"{req.id}: method {req.method!r} is neither a test method nor a level "
                    f"({', '.join(self.c.level_names())})",
                    req,
                )
        traced = req.satisfies or req.refines or self.m.mitigations_implemented_by(req.id)
        if not traced:
            self.rule(
                "requirement-orphan",
                f"{req.id}: satisfies no user need, refines no requirement and implements no mitigation",
                req,
            )

    def check_risk(self, risk: Risk) -> None:
        for field_name, allowed in (
            ("severity", self.c.severities),
            ("residual_severity", self.c.severities),
            ("likelihood", self.c.likelihoods),
            ("residual_likelihood", self.c.likelihoods),
        ):
            value = getattr(risk, field_name)
            if value and value not in allowed:
                self.add("bad-enum", f"{risk.id}: {field_name} must be one of {allowed}, got {value!r}", risk)
        mits = self.m.mitigations_for_risk(risk.id)
        for mit_id in risk.mitigated_by:
            if (
                self.ref(risk, "mitigated_by", mit_id, cfg.MITIGATION)
                and risk.id not in self.m.mitigations[mit_id].mitigates
            ):
                self.add(
                    "inconsistent-trace",
                    f"{risk.id}: mitigated_by {mit_id}, but {mit_id}.mitigates does not list {risk.id}",
                    risk,
                )
        if risk.mitigated_by:
            for mit in mits:
                if mit.id not in risk.mitigated_by:
                    self.add(
                        "inconsistent-trace",
                        f"{risk.id}: {mit.id} mitigates it but is missing from mitigated_by",
                        risk,
                    )
        if not mits:
            self.rule("risk-unmitigated", f"{risk.id}: no mitigation controls this risk", risk)
        if self.c.acceptable_risk_score is not None:
            sev = risk.residual_severity or risk.severity
            likelihood = risk.residual_likelihood or risk.likelihood
            score = self.c.risk_score(sev, likelihood)
            if score is not None and score > self.c.acceptable_risk_score:
                self.rule(
                    "risk-unacceptable",
                    f"{risk.id}: residual risk score {score} ({sev} x {likelihood}) exceeds the "
                    f"acceptable threshold {self.c.acceptable_risk_score}",
                    risk,
                )

    def check_mitigation(self, mit: Mitigation) -> None:
        if mit.type and mit.type not in cfg.MITIGATION_TYPES:
            self.add("bad-enum", f"{mit.id}: type must be one of {cfg.MITIGATION_TYPES}", mit)
        if not mit.mitigates:
            self.add("mitigation-no-risk", f"{mit.id}: mitigates no risk", mit)
        for risk in mit.mitigates:
            self.ref(mit, "mitigates", risk, cfg.RISK)
        for req in mit.implemented_by:
            self.ref(mit, "implemented_by", req, cfg.REQUIREMENT)
        if not mit.implemented_by:
            self.rule(
                "mitigation-unimplemented",
                f"{mit.id}: no requirement implements this mitigation",
                mit,
            )

    def check_refines_cycles(self) -> None:
        reqs = self.m.requirements
        state: dict[str, int] = {}  # 1 = visiting, 2 = done

        def visit(rid: str, path: list[str]) -> None:
            if state.get(rid) == 2:
                return
            if state.get(rid) == 1:
                cycle = path[path.index(rid) :] + [rid]
                self.add("refines-cycle", "refines cycle: " + " -> ".join(cycle), reqs[rid])
                return
            state[rid] = 1
            for parent in reqs[rid].refines:
                if parent in reqs and parent != rid:
                    visit(parent, path + [rid])
            state[rid] = 2

        for rid in sorted(reqs, key=natural_key):
            visit(rid, [])

    # --- claims: one owner per test case --------------------------------------

    def check_claims(self) -> None:
        """Every claim item on its own, then every pair of claims on one
        target (and on targets of one ``variants`` group)."""
        for ent in self.m.entities():
            items = claim_items(ent)
            for index, vb in enumerate(items):
                self.check_item(ent, index, vb)
            if items and ent.kind == cfg.REQUIREMENT and self.m.children(ent.id):
                kids = ", ".join(c.id for c in self.m.children(ent.id))
                self.rule(
                    "parent-with-claims",
                    f"{ent.id}: is refined by {kids} and also claims test cases of its own; "
                    "prefer verifying it through its children (its own set must then be complete too)",
                    ent,
                )
        for group in self.c.variants:
            for label in group:
                try:
                    norm = labels.normalize_label(label, self.c.main_repo)
                except labels.BadTarget as exc:
                    self.add("bad-target", f"config.variants: {exc}")
                    continue
                if self.unknown(norm):
                    # a typo here would silently drop the variant from same-code-multiple-owners
                    self.add(
                        "unknown-target",
                        f"config.variants: {label}: no such test target (not in --known-targets); "
                        "the same-code check would silently skip it",
                    )

        by_target: dict[str, list[Claim]] = {}
        for claim in self.m.claims():
            if claim.pattern is not None and not _selector_ok(claim.pattern):
                continue  # reported as bad-selector; it cannot be compared
            by_target.setdefault(claim.target, []).append(claim)
        for target in sorted(by_target, key=natural_key):
            claims = by_target[target]
            for i, a in enumerate(claims):
                for b in claims[i + 1 :]:
                    self.compare(a, b)
        for group in self.c.variant_groups():
            for i, t1 in enumerate(group):
                for t2 in group[i + 1 :]:
                    for a in by_target.get(t1, ()):
                        for b in by_target.get(t2, ()):
                            if a.entity != b.entity:
                                self.compare_variants(a, b)

    def check_item(self, ent: Entity, index: int, vb: VerifiedBy) -> None:
        rel = "validated_by" if ent.kind == cfg.USER_NEED else "verified_by"
        where = f"{ent.id}: {rel} {vb.label}"
        loc = vb.location if vb.location.path else ent.location
        try:
            labels.normalize_label(vb.label, self.c.main_repo)
        except labels.BadTarget as exc:
            self.add_at("bad-target", f"{ent.id}: {rel}[{index}]: {exc}", ent.id, loc)
        else:
            if self.unknown(vb.target):
                self.add_at(
                    "unknown-target",
                    f"{where}: no such test target (not in --known-targets); a typo would read as not run forever",
                    ent.id,
                    loc,
                )
        if vb.problem:
            self.add_at("bad-selector", f"{where}: {vb.problem}", ent.id, loc)
        for pattern in vb.cases:
            try:
                case_selectors.check(pattern)
            except case_selectors.BadSelector as exc:
                self.add_at("bad-selector", f"{where}: {exc}", ent.id, loc)
                continue
            if not case_selectors.is_literal(pattern):
                self.rule_at(
                    "glob-selector",
                    f"{where}: selector {pattern!r} is a glob; list the cases (or pin them with sets_lock)",
                    ent.id,
                    loc,
                )
        if vb.level and self.c.level(vb.level) is None:
            self.add_at("bad-level", f"{ent.id}: {rel} {vb.label} has unknown level {vb.level!r}", ent.id, loc)
        if vb.legacy:
            self.rule_at(
                "bare-target-reference",
                f"{where}: a bare target reference claims the whole target; name its cases "
                f"({{target: {vb.label}, cases: ['*']}}) or write {{target: {vb.label}, whole: true, reason: ...}}",
                ent.id,
                loc,
            )
        elif vb.whole and not vb.reason:
            self.rule_at(
                "whole-target-reference",
                f"{where}: whole: true needs a reason (why can the target not be claimed per case?)",
                ent.id,
                loc,
            )

    def compare(self, a: Claim, b: Claim) -> None:
        """Two claims on one target: an error across entities, a redundancy
        within one."""
        overlap, example = _overlap(a, b)
        if not overlap:
            return
        first, second = _ordered(a, b)
        if a.entity == b.entity:
            self.rule_at(
                "redundant-selector",
                f"{a.entity}: {first.describe()} and {second.describe()} of {a.target} overlap{_eg(example)}; "
                "one of them is redundant",
                a.entity,
                second.location,
            )
            return
        self.add_at(
            "shared-case",
            f"{second.entity} and {first.entity} both claim {_what(second, first, a.target)}{_eg(example)}"
            f"{_also(first)}. A test case verifies at most one requirement: {_advice(second, first)}.",
            second.entity,
            second.location,
        )

    def compare_variants(self, a: Claim, b: Claim) -> None:
        """Claims of two entities on two targets that run the same test code."""
        overlap, example = _overlap(a, b)
        if not overlap:
            return
        first, second = _ordered(a, b)
        self.add_at(
            "same-code-multiple-owners",
            f"{second.entity} claims {second.describe()} of {second.target} and {first.entity} claims "
            f"{first.describe()} of {first.target}, which run the same test code (config.variants){_eg(example)}"
            f"{_also(first)}. A test case verifies at most one requirement: give the variants' cases to one "
            "of them, or give the variants distinct case names.",
            second.entity,
            second.location,
        )

    # --- the verification-set lock --------------------------------------------

    def check_lock(self) -> None:
        if not self.c.sets_lock:
            return
        shown = self.m.lock_path(shown=True)
        loc = Location(shown)
        try:
            lock = rr_lock.load_lock(self.m.lock_path(), self.c.main_repo, shown=shown)
        except rr_lock.LockError as exc:
            for problem in exc.problems:
                self.add_at("lock-invalid", problem, "", loc)
            return
        by_target: dict[str, list[Claim]] = {}
        for claim in self.m.claims():
            by_target.setdefault(claim.target, []).append(claim)
        reported: set[str] = set()
        for entry in lock.entries:
            if entry.target not in reported and self.unknown(entry.target):
                reported.add(entry.target)
                self.add_at(
                    "unknown-target",
                    f"{entry.target}: no such test target (not in --known-targets); its locked cases "
                    "would read as missing forever",
                    "",
                    Location(shown, entry.target_line),
                )
            at = Location(shown, entry.line)
            key = f"{entry.target}#{entry.path}"
            if not self.m.is_verifiable(entry.owner):
                ent = self.m.get(entry.owner)
                what = f"a {ent.kind}" if ent is not None else "not defined"
                self.add_at(
                    "lock-invalid",
                    f"{key} is locked to {entry.owner}, which is {what}; only user needs, requirements and "
                    "mitigations hold verification sets",
                    entry.owner,
                    at,
                )
                continue
            if self.c.attribution != "model":
                continue  # hybrid: a tag may own a locked case no claim covers
            claimants = sorted(
                {c.entity for c in by_target.get(entry.target, ()) if _claim_matches(c, entry.path)},
                key=natural_key,
            )
            others = [e for e in claimants if e != entry.owner]
            if others:
                self.add_at(
                    "lock-owner-changed",
                    f"{key} is locked to {entry.owner} but claimed by {', '.join(others)}; re-lock "
                    "(`rr sets lock --write`) and review the owner change",
                    entry.owner,
                    at,
                )
            elif not claimants:
                self.rule_at(
                    "lock-stale",
                    f"{key} is locked to {entry.owner}, but no claim of {entry.owner} selects it; "
                    "re-lock (`rr sets lock --write --allow-removals`) or restore the claim",
                    entry.owner,
                    at,
                )


def _selector_ok(pattern: str) -> bool:
    try:
        case_selectors.check(pattern)
    except case_selectors.BadSelector:
        return False
    return True


def _claim_matches(claim: Claim, path: str) -> bool:
    if claim.pattern is not None and not _selector_ok(claim.pattern):
        return False
    return claim.matches(path)


def _overlap(a: Claim, b: Claim) -> tuple[bool, str | None]:
    """Whether two claims (same target, or same code) can select one case,
    and an example case path (None when either is a whole claim of the
    target's every case, "" when any case path will do)."""
    if a.pattern is None and b.pattern is None:
        return True, None
    if a.pattern is None or b.pattern is None:
        pattern = b.pattern if a.pattern is None else a.pattern
        return True, case_selectors.witness("*", pattern or "*")
    found = case_selectors.witness(a.pattern, b.pattern)
    return found is not None, found


def _ordered(a: Claim, b: Claim) -> tuple[Claim, Claim]:
    """(earlier, later) by where they are written."""
    ka = (a.location.path, a.location.line, natural_key(a.entity), a.index)
    kb = (b.location.path, b.location.line, natural_key(b.entity), b.index)
    return (a, b) if ka <= kb else (b, a)


def _what(a: Claim, b: Claim, target: str) -> str:
    if a.pattern is None and b.pattern is None:
        return f"the whole target {target}"
    return f"cases of {target} ({a.describe()} vs {b.describe()})"


def _eg(example: str | None) -> str:
    if example is None:
        return ""
    if example == "":
        return ", e.g. any case"
    return f", e.g. {example!r}"


def _also(first: Claim) -> str:
    return f" ({first.entity} claims it at {first.location})" if first.location.path else ""


def _advice(a: Claim, b: Claim) -> str:
    if a.pattern is None and b.pattern is None:
        return "give the target to one of them, or split it into per-case selectors"
    if a.pattern is None or b.pattern is None:
        return "replace the whole-target claim with selectors that leave the other's cases out"
    return "narrow one selector"
