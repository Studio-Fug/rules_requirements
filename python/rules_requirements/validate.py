# SPDX-License-Identifier: AGPL-3.0-or-later
"""Structural, referential and coverage validation of a :class:`Model`.

Every finding is an :class:`Issue` with a stable ``code`` so tooling (CI, the
web editor, agents) can filter and link to documentation. Shape and reference
problems are always errors; coverage findings follow ``config.rules``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rules_requirements import config as cfg
from rules_requirements.model import (
    Entity,
    Location,
    Mitigation,
    Model,
    Requirement,
    Risk,
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


def validate(model: Model, strict: bool = False) -> list[Issue]:
    """All issues in ``model``. ``strict`` promotes warnings to errors."""
    v = _Validator(model)
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
    def __init__(self, model: Model):
        self.m = model
        self.c = model.config
        self.issues: list[Issue] = []

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
        for vb in req.verified_by:
            if vb.level and self.c.level(vb.level) is None:
                self.add("bad-level", f"{req.id}: verified_by {vb.target} has unknown level {vb.level!r}", req)
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
