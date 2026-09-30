# SPDX-License-Identifier: AGPL-3.0-or-later
"""The built-in workflows.

``completeness``          Gaps in the model and its traceability: validation
                          issues, unverified / under-verified / stale / failing
                          requirements, uncontrolled risks, missing
                          implementation links — plus, with an LLM, a review of
                          whether the requirements cover the needs at all.
``test_adequacy``         Does test X actually assert what requirement Y states?
``implementation_review`` Does the annotated code implement requirement Y?
``mitigation_adequacy``   Do the requirements behind a mitigation actually
                          control risk W (and is the residual estimate plausible)?
``risk_discovery``        Hazards the analysis may be missing.
``assistant``             Free-form instruction -> proposed model operations.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from rules_requirements import config as cfg
from rules_requirements.agents import Finding, Job, Workflow
from rules_requirements.agents.llm import LLM, LLMError
from rules_requirements.annotations import Reference, candidate_files, is_test_path
from rules_requirements.edit import entity_to_dict
from rules_requirements.ingest import TestCase
from rules_requirements.model import FIELDS, Model
from rules_requirements.trace import Matrix
from rules_requirements.util import natural_key

SYSTEM = (
    "You review requirements-driven development artifacts for a product team: user needs, requirements, "
    "risks (ISO 14971 hazard -> hazardous situation -> harm), risk control measures (mitigations) and the tests "
    "and code that trace to them, in the spirit of IEC 62304 design controls. Be concrete and skeptical: judge "
    "only from the text and code you are shown, cite entity ids, and never assume evidence you were not given. "
    "Prefer a few precise, actionable findings over many vague ones."
)

_GAP_SEVERITY = {
    "failed": "error",
    "high-risk-open": "error",
    "unknown-id": "error",
    "unverified": "warning",
    "under-verified": "warning",
    "stale": "warning",
    "partial": "warning",
    "pyramid": "warning",
    "no-implementation": "warning",
}


@dataclass
class Context:
    """What a workflow can see."""

    model: Model
    matrix: Matrix
    root: str
    references: list[Reference] | None = None
    issues: list[Any] = field(default_factory=list)
    _texts: dict[str, list[str]] = field(default_factory=dict)
    _files: list[str] | None = None

    # --- source access ----------------------------------------------------

    def lines(self, rel: str) -> list[str]:
        if rel not in self._texts:
            path = os.path.realpath(os.path.join(self.root, rel))
            if not path.startswith(os.path.realpath(self.root) + os.sep):
                self._texts[rel] = []
            else:
                try:
                    with open(path, encoding="utf-8") as fh:
                        self._texts[rel] = fh.read().splitlines()
                except (OSError, UnicodeDecodeError):
                    self._texts[rel] = []
        return self._texts[rel]

    def files(self) -> list[str]:
        if self._files is None:
            self._files = candidate_files(self.root)
        return self._files

    def snippet(self, rel: str, line: int, max_lines: int = 120) -> str:
        """The definition starting at/after ``line`` (1-based), with line numbers."""
        lines = self.lines(rel)
        if not lines:
            return ""
        start = max(0, line - 1)
        # Walk to the definition itself if the line is a comment/decorator above it.
        for i in range(start, min(len(lines), start + 8)):
            if re.match(r"\s*(?:(?:pub\s+)?(?:async\s+)?(?:def|fn|class|struct|impl|func|function)\b|TEST(?:_F|_P)?\s*\()", lines[i]):
                start = i
                break
        # Include decorators / attributes / comments immediately above.
        while start > 0 and re.match(r"\s*(?:@|#\[|//|#)", lines[start - 1]):
            start -= 1
        end = _block_end(lines, start, max_lines)
        width = len(str(end))
        return "\n".join(f"{i + 1:>{width}} | {lines[i]}" for i in range(start, end))

    def locate_case(self, case: TestCase) -> tuple[str, int] | None:
        """Best-effort source location of a test case from its name."""
        name = re.sub(r"\[.*\]$", "", case.name)
        classname = case.classname or ""
        patterns = [re.compile(rf"^\s*(?:async\s+)?def\s+{re.escape(name)}\b"), re.compile(rf"^\s*(?:pub\s+)?fn\s+{re.escape(name)}\b")]
        if classname:
            patterns.insert(0, re.compile(rf"^\s*(?:TEST|TEST_F|TEST_P|TYPED_TEST)\s*\(\s*{re.escape(classname.split('.')[-1])}\s*,\s*{re.escape(name)}\s*\)"))
        candidates = []
        if classname:
            mod = classname.split("::")[0].replace(".", "/")
            candidates += [f"{mod}.py", f"{mod.rsplit('/', 1)[0]}.py" if "/" in mod else ""]
        candidates += [f for f in self.files() if is_test_path(f) or f.endswith((".rs", ".cc", ".cpp", "_test.py"))]
        seen = set()
        for rel in candidates:
            if not rel or rel in seen:
                continue
            seen.add(rel)
            for i, text in enumerate(self.lines(rel)):
                if any(p.match(text) for p in patterns):
                    return rel, i + 1
        return None

    # --- model digests ----------------------------------------------------

    def digest(self, kinds: Iterable[str] = cfg.KINDS, with_status: bool = True) -> str:
        out = []
        for kind in kinds:
            ents = sorted(self.model.section(kind).values(), key=lambda e: natural_key(e.id))
            if not ents:
                continue
            out.append(f"## {cfg.SECTIONS[kind]}")
            for ent in ents:
                d = entity_to_dict(ent)
                d.pop("notes", None)
                status = self.matrix.verdicts[ent.id].status if with_status and ent.id in self.matrix.verdicts else ""
                head = f"- {ent.id}" + (f" [{status}]" if status else "") + f": {ent.title}"
                rest = {k: v for k, v in d.items() if k not in ("id", "title")}
                out.append(head + ("\n  " + "; ".join(f"{k}={v}" for k, v in rest.items()) if rest else ""))
        return "\n".join(out)


def _block_end(lines: list[str], start: int, max_lines: int) -> int:
    """End (exclusive) of the block starting at ``start``: braces or indentation."""
    limit = min(len(lines), start + max_lines)
    first = next((i for i in range(start, limit) if not re.match(r"\s*(?:@|#\[|//|#)", lines[i])), start)
    head = lines[first] if first < len(lines) else ""
    if "{" in "".join(lines[first : first + 3]) and not head.rstrip().endswith(":"):
        depth, opened = 0, False
        for i in range(first, limit):
            for ch in lines[i]:
                if ch == "{":
                    depth, opened = depth + 1, True
                elif ch == "}":
                    depth -= 1
            if opened and depth <= 0:
                return i + 1
        return limit
    indent = len(head) - len(head.lstrip())
    end = first + 1
    for i in range(first + 1, limit):
        text = lines[i]
        if text.strip() and len(text) - len(text.lstrip()) <= indent:
            break
        end = i + 1
    return end


# --------------------------------------------------------------------------- #
# Proposals                                                                   #
# --------------------------------------------------------------------------- #

PROPOSAL_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["kind", "title", "description", "satisfies", "refines", "mitigates", "implemented_by", "severity", "likelihood", "method", "type"],
    "properties": {
        "kind": {"type": "string", "enum": ["none", "user_need", "requirement", "risk", "mitigation"]},
        "title": {"type": "string"},
        "description": {"type": "string"},
        "satisfies": {"type": "array", "items": {"type": "string"}},
        "refines": {"type": "array", "items": {"type": "string"}},
        "mitigates": {"type": "array", "items": {"type": "string"}},
        "implemented_by": {"type": "array", "items": {"type": "string"}},
        "severity": {"type": "string"},
        "likelihood": {"type": "string"},
        "method": {"type": "string"},
        "type": {"type": "string"},
    },
}

FINDINGS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["findings"],
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["severity", "category", "entity", "refs", "title", "detail", "proposal"],
                "properties": {
                    "severity": {"type": "string", "enum": ["info", "warning", "error"]},
                    "category": {"type": "string"},
                    "entity": {"type": "string", "description": "id of the entity the finding is about, or empty"},
                    "refs": {"type": "array", "items": {"type": "string"}},
                    "title": {"type": "string"},
                    "detail": {"type": "string"},
                    "proposal": PROPOSAL_SCHEMA,
                },
            },
        }
    },
}


def to_proposal(raw: dict[str, Any] | None, model: Model) -> dict[str, Any] | None:
    """Model-valid entity data from an LLM proposal, or None."""
    if not raw or raw.get("kind") in (None, "", "none"):
        return None
    kind = str(raw["kind"])
    if kind not in cfg.KINDS or not str(raw.get("title", "")).strip():
        return None
    data = {k: v for k, v in raw.items() if k in FIELDS[kind] and v not in ("", [], None)}
    known = model.ids()
    for key in ("satisfies", "refines", "mitigates", "implemented_by"):
        if key in data:
            data[key] = [x for x in data[key] if x in known]
            if not data[key]:
                data.pop(key)
    if kind == cfg.RISK:
        for key, allowed in (("severity", model.config.severities), ("likelihood", model.config.likelihoods)):
            if data.get(key) and data[key] not in allowed:
                data.pop(key)
    if kind == cfg.MITIGATION and data.get("type") not in cfg.MITIGATION_TYPES:
        data.pop("type", None)
    if kind == cfg.REQUIREMENT and data.get("method") and not (
        data["method"] in model.test_methods or model.config.level(data["method"])
    ):
        data.pop("method")
    return {"kind": kind, "data": data}


def _llm_findings(workflow: str, raw: Any, model: Model, default_entity: str = "") -> list[Finding]:
    out = []
    for item in (raw or {}).get("findings", []):
        entity = str(item.get("entity", "") or default_entity)
        if entity and model.get(entity) is None:
            entity = default_entity
        out.append(
            Finding(
                workflow=workflow,
                severity=item.get("severity", "info") if item.get("severity") in ("info", "warning", "error") else "info",
                category=str(item.get("category", "review")),
                title=str(item.get("title", "")).strip() or "(untitled finding)",
                detail=str(item.get("detail", "")).strip(),
                entity=entity,
                refs=[r for r in item.get("refs", []) if isinstance(r, str)],
                proposal=to_proposal(item.get("proposal"), model),
                source="llm",
            )
        )
    return out


# --------------------------------------------------------------------------- #
# Workflows                                                                   #
# --------------------------------------------------------------------------- #


def completeness(ctx: Context, job: Job, llm: LLM | None, use_llm: bool = True, **_: Any) -> list[Finding]:
    findings: list[Finding] = []
    for issue in ctx.issues:
        findings.append(
            Finding("completeness", issue.severity, f"validation:{issue.code}", issue.message, entity=issue.entity or "")
        )
    job.say(f"model validation: {len(ctx.issues)} issue(s)")
    for gap in ctx.matrix.gaps:
        findings.append(
            Finding(
                "completeness",
                _GAP_SEVERITY.get(gap.kind, "info"),
                f"trace:{gap.kind}",
                f"{gap.entity}: {gap.message}",
                detail=f"route: {gap.route}" + (f"; demanded {gap.demanded}" if gap.demanded else ""),
                entity=gap.entity if ctx.model.get(gap.entity) else "",
            )
        )
    job.say(f"traceability gaps: {len(ctx.matrix.gaps)}")
    if llm is None or not use_llm:
        job.say("coverage review skipped (no LLM)")
        return findings
    job.say(f"coverage review with {llm.name} …")
    prompt = (
        "Review this requirements model for completeness and quality. Report:\n"
        "1. user needs whose satisfying requirements do not fully cover the need (propose the missing requirement);\n"
        "2. requirements that are ambiguous, compound, or not objectively verifiable as written (say how to fix);\n"
        "3. behaviors implied by the requirements that introduce hazards the risk analysis lacks (propose a risk);\n"
        "4. traces that look wrong (a requirement attached to an unrelated need, a mitigation that does not address its risk).\n"
        "Only report real problems. For proposals, reference existing ids in satisfies/mitigates/implemented_by; "
        'use kind "none" when no new object is warranted.\n\n' + ctx.digest()
    )
    try:
        raw = llm.json(SYSTEM, prompt, FINDINGS_SCHEMA)
    except LLMError as exc:
        job.say(f"coverage review failed: {exc}")
        return findings
    extra = _llm_findings("completeness", raw, ctx.model)
    job.say(f"coverage review: {len(extra)} finding(s)")
    return findings + extra


ADEQUACY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["tests", "missing_checks", "summary"],
    "properties": {
        "tests": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["test", "verdict", "rationale"],
                "properties": {
                    "test": {"type": "string"},
                    "verdict": {"type": "string", "enum": ["proves", "partially", "does-not-prove", "cannot-tell"]},
                    "rationale": {"type": "string"},
                },
            },
        },
        "missing_checks": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
}


def _requirement_block(ctx: Context, req_id: str) -> str:
    req = ctx.model.requirements[req_id]
    v = ctx.matrix.verdicts[req_id]
    parts = [f"{req.id}: {req.title}"]
    if req.description:
        parts.append(req.description)
    parts.append(f"(demands verification at level: {v.demanded}; current status: {v.status})")
    return "\n".join(parts)


def linked_tests(ctx: Context, req_id: str, max_tests: int = 8) -> list[tuple[str, str]]:
    """(label, source excerpt) for tests linked to a requirement by evidence or annotation."""
    v = ctx.matrix.verdicts[req_id]
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, int]] = set()
    for ref in v.verified_in:
        if (ref.path, ref.line) in seen:
            continue
        seen.add((ref.path, ref.line))
        code = ctx.snippet(ref.path, ref.line)
        if code:
            out.append((f"{ref.symbol or 'test'} ({ref.path}:{ref.line})", code))
    for case in ctx.matrix.evidence.for_id(req_id):
        loc = ctx.locate_case(case)
        if loc is None or loc in seen:
            continue
        seen.add(loc)
        code = ctx.snippet(*loc)
        if code:
            out.append((f"{case.full_name} ({loc[0]}:{loc[1]}, last result: {case.status})", code))
    return out[:max_tests]


def _targets(ctx: Context, entities: Any, kind: str) -> list[str]:
    ids = [e for e in (entities or []) if e in ctx.model.section(kind)]
    return ids or sorted(ctx.model.section(kind), key=natural_key)


def test_adequacy(ctx: Context, job: Job, llm: LLM | None, entities: Any = None, limit: int = 25, **_: Any) -> list[Finding]:
    assert llm is not None
    findings: list[Finding] = []
    for req_id in _targets(ctx, entities, cfg.REQUIREMENT)[: int(limit)]:
        tests = linked_tests(ctx, req_id)
        if not tests:
            job.say(f"{req_id}: no linked test source found, skipped")
            continue
        job.say(f"{req_id}: reviewing {len(tests)} test(s) …")
        prompt = (
            "Requirement under verification:\n" + _requirement_block(ctx, req_id) + "\n\n"
            "For each test below, decide whether its assertions actually prove the requirement as stated "
            "(not merely exercise related code). A test 'proves' it only if it would fail were the requirement "
            "violated. List checks that are missing to prove the requirement completely.\n\n"
            + "\n\n".join(f"### {label}\n```\n{code}\n```" for label, code in tests)
        )
        try:
            raw = llm.json(SYSTEM, prompt, ADEQUACY_SCHEMA)
        except LLMError as exc:
            job.say(f"{req_id}: review failed: {exc}")
            continue
        weak = [t for t in raw.get("tests", []) if t.get("verdict") != "proves"]
        for t in weak:
            findings.append(
                Finding(
                    "test_adequacy",
                    "warning" if t.get("verdict") in ("partially", "cannot-tell") else "error",
                    f"test-adequacy:{t.get('verdict')}",
                    f"{req_id}: {t.get('test')} {t.get('verdict', '').replace('-', ' ')}",
                    detail=str(t.get("rationale", "")),
                    entity=req_id,
                    source="llm",
                )
            )
        missing = [m for m in raw.get("missing_checks", []) if str(m).strip()]
        if missing:
            findings.append(
                Finding(
                    "test_adequacy",
                    "warning",
                    "test-adequacy:missing-checks",
                    f"{req_id}: tests do not check everything the requirement states",
                    detail="\n".join(f"- {m}" for m in missing) + (f"\n\n{raw.get('summary', '')}" if raw.get("summary") else ""),
                    entity=req_id,
                    source="llm",
                )
            )
        if not weak and not missing:
            findings.append(
                Finding("test_adequacy", "info", "test-adequacy:ok", f"{req_id}: tests prove the requirement", detail=str(raw.get("summary", "")), entity=req_id, source="llm")
            )
    return findings


IMPLEMENTATION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "rationale", "missing_behavior"],
    "properties": {
        "verdict": {"type": "string", "enum": ["implements", "partially", "does-not-implement", "cannot-tell"]},
        "rationale": {"type": "string"},
        "missing_behavior": {"type": "array", "items": {"type": "string"}},
    },
}


def implementation_review(ctx: Context, job: Job, llm: LLM | None, entities: Any = None, limit: int = 25, **_: Any) -> list[Finding]:
    assert llm is not None
    findings: list[Finding] = []
    for req_id in _targets(ctx, entities, cfg.REQUIREMENT)[: int(limit)]:
        refs = ctx.matrix.verdicts[req_id].implemented_in
        code = [(f"{r.symbol or r.path} ({r.path}:{r.line})", ctx.snippet(r.path, r.line)) for r in refs[:6]]
        code = [(label, c) for label, c in code if c]
        if not code:
            continue
        job.say(f"{req_id}: reviewing {len(code)} implementation site(s) …")
        prompt = (
            "Requirement:\n" + _requirement_block(ctx, req_id) + "\n\n"
            "Code annotated as implementing it follows. Does it implement the requirement completely and correctly?\n\n"
            + "\n\n".join(f"### {label}\n```\n{c}\n```" for label, c in code)
        )
        try:
            raw = llm.json(SYSTEM, prompt, IMPLEMENTATION_SCHEMA)
        except LLMError as exc:
            job.say(f"{req_id}: review failed: {exc}")
            continue
        verdict = raw.get("verdict", "cannot-tell")
        if verdict != "implements" or raw.get("missing_behavior"):
            missing = "\n".join(f"- {m}" for m in raw.get("missing_behavior", []))
            findings.append(
                Finding(
                    "implementation_review",
                    "error" if verdict == "does-not-implement" else "warning",
                    f"implementation:{verdict}",
                    f"{req_id}: implementation {verdict.replace('-', ' ')}",
                    detail=(raw.get("rationale", "") + ("\n\nMissing:\n" + missing if missing else "")).strip(),
                    entity=req_id,
                    source="llm",
                )
            )
    return findings


MITIGATION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["mitigations", "residual_assessment", "findings"],
    "properties": {
        "mitigations": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["mitigation", "verdict", "rationale"],
                "properties": {
                    "mitigation": {"type": "string"},
                    "verdict": {"type": "string", "enum": ["effective", "partially-effective", "ineffective", "cannot-tell"]},
                    "rationale": {"type": "string"},
                },
            },
        },
        "residual_assessment": {"type": "string"},
        "findings": FINDINGS_SCHEMA["properties"]["findings"],
    },
}


def mitigation_adequacy(ctx: Context, job: Job, llm: LLM | None, entities: Any = None, limit: int = 25, **_: Any) -> list[Finding]:
    assert llm is not None
    findings: list[Finding] = []
    for risk_id in _targets(ctx, entities, cfg.RISK)[: int(limit)]:
        risk = ctx.model.risks[risk_id]
        mits = ctx.model.mitigations_for_risk(risk_id)
        chain = entity_to_dict(risk)
        chain.pop("notes", None)
        blocks = [f"RISK {risk_id}: " + "; ".join(f"{k}={v}" for k, v in chain.items() if k != "id")]
        for mit in mits:
            blocks.append(f"MITIGATION {mit.id} ({mit.type or 'untyped'}): {mit.title}. {mit.description}")
            for req_id in mit.implemented_by:
                if req_id in ctx.model.requirements:
                    blocks.append("  implemented by " + _requirement_block(ctx, req_id).replace("\n", "\n    "))
        if not mits:
            blocks.append("(no mitigations)")
        job.say(f"{risk_id}: assessing {len(mits)} mitigation(s) …")
        prompt = (
            "Assess whether the mitigations and the requirements implementing them actually control this risk "
            "(ISO 14971 §7): would they, if met, prevent the hazardous situation or reduce the harm as claimed? "
            "Judge each mitigation, assess whether the residual severity/likelihood estimate is plausible, and "
            "report gaps as findings (propose a missing requirement or mitigation where one is needed; reference "
            f'{risk_id} and existing ids; kind "none" otherwise).\n\n' + "\n".join(blocks)
        )
        try:
            raw = llm.json(SYSTEM, prompt, MITIGATION_SCHEMA)
        except LLMError as exc:
            job.say(f"{risk_id}: review failed: {exc}")
            continue
        for m in raw.get("mitigations", []):
            if m.get("verdict") != "effective":
                target = m.get("mitigation") if ctx.model.get(str(m.get("mitigation"))) else risk_id
                findings.append(
                    Finding(
                        "mitigation_adequacy",
                        "error" if m.get("verdict") == "ineffective" else "warning",
                        f"mitigation:{m.get('verdict')}",
                        f"{m.get('mitigation')} is {str(m.get('verdict', '')).replace('-', ' ')} against {risk_id}",
                        detail=str(m.get("rationale", "")),
                        entity=target,
                        refs=[risk_id],
                        source="llm",
                    )
                )
        if str(raw.get("residual_assessment", "")).strip():
            findings.append(
                Finding("mitigation_adequacy", "info", "mitigation:residual", f"{risk_id}: residual risk assessment", detail=str(raw["residual_assessment"]), entity=risk_id, source="llm")
            )
        findings += _llm_findings("mitigation_adequacy", raw, ctx.model, default_entity=risk_id)
    return findings


def risk_discovery(ctx: Context, job: Job, llm: LLM | None, **_: Any) -> list[Finding]:
    assert llm is not None
    job.say(f"hazard analysis with {llm.name} …")
    prompt = (
        "Perform a preliminary hazard analysis of the product described by this model. Identify hazards and "
        "hazardous situations that the existing risks do not cover — foreseeable misuse, failure modes of the "
        "specified behavior, and sequences of events (ISO 14971 §5.4). For each, propose a risk (kind \"risk\" "
        "with severity/likelihood from the model's scales) and describe candidate controls in the detail. "
        "Do not repeat risks that are already present.\n\n"
        f"Severity scale: {', '.join(ctx.model.config.severities)}. Likelihood scale: {', '.join(ctx.model.config.likelihoods)}.\n\n"
        + ctx.digest()
    )
    raw = llm.json(SYSTEM, prompt, FINDINGS_SCHEMA)
    return _llm_findings("risk_discovery", raw, ctx.model)


OPERATIONS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["reply", "operations"],
    "properties": {
        "reply": {"type": "string"},
        "operations": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["op", "id", "rationale", "note", "entity"],
                "properties": {
                    "op": {"type": "string", "enum": ["create", "update", "note"]},
                    "id": {"type": "string", "description": "entity to update or annotate; empty for create"},
                    "rationale": {"type": "string"},
                    "note": {"type": "string", "description": "note text for op=note"},
                    "entity": PROPOSAL_SCHEMA,
                },
            },
        },
    },
}


def assistant(ctx: Context, job: Job, llm: LLM | None, instruction: str = "", focus: Any = None, **_: Any) -> list[Finding]:
    """Turn a free-form instruction into proposed operations (never applied here)."""
    assert llm is not None
    if not instruction.strip():
        raise ValueError("an instruction is required")
    focus_ids = [f for f in (focus or []) if ctx.model.get(f)]
    detail = ""
    if focus_ids:
        detail = "\n\nFocus entities (full data):\n" + "\n".join(
            f"- {entity_to_dict(ctx.model.get(f))}" for f in focus_ids  # type: ignore[arg-type]
        )
    prompt = (
        "You help maintain this requirements model. Carry out the user's instruction by proposing operations: "
        '"create" a new entity (fill entity), "update" an existing entity (id + the complete new field values in entity; '
        'fields you leave empty are removed, so repeat unchanged values), or "note" to attach a note to an entity. '
        "Keep ids out of new entities (they are assigned on save). Reply briefly with what you propose and why.\n\n"
        f"Instruction: {instruction}{detail}\n\nModel:\n" + ctx.digest()
    )
    raw = llm.json(SYSTEM, prompt, OPERATIONS_SCHEMA)
    ops = []
    findings = []
    for op in raw.get("operations", []):
        kind = op.get("op")
        if kind == "note" and ctx.model.get(op.get("id", "")):
            findings.append(Finding("assistant", "info", "assistant:note", f"Note on {op['id']}", detail=op.get("note", ""), entity=op["id"], source="llm"))
            ops.append(op)
        elif kind in ("create", "update"):
            prop = to_proposal(op.get("entity"), ctx.model)
            if prop is None:
                continue
            target = op.get("id", "") if kind == "update" else ""
            if kind == "update":
                ent = ctx.model.get(target)
                if ent is None or ent.kind != prop["kind"]:
                    continue
            findings.append(
                Finding(
                    "assistant",
                    "info",
                    f"assistant:{kind}",
                    (f"Update {target}" if kind == "update" else f"New {prop['kind'].replace('_', ' ')}") + f": {prop['data'].get('title', '')}",
                    detail=op.get("rationale", ""),
                    entity=target,
                    proposal={**prop, "op": kind},
                    source="llm",
                )
            )
            ops.append(op)
    job.result = {"reply": raw.get("reply", "")}
    job.say(f"assistant proposed {len(findings)} operation(s)")
    return findings


WORKFLOWS: dict[str, Workflow] = {
    wf.id: wf
    for wf in (
        Workflow(
            "completeness",
            "Completeness check",
            "Validation issues, traceability gaps (unverified, under-verified, stale, failing, uncontrolled risks, "
            "missing implementation links) and, with an LLM, a coverage review of needs vs requirements.",
            completeness,
            params={"use_llm": "also run the LLM coverage review (default true)"},
        ),
        Workflow(
            "test_adequacy",
            "Test adequacy",
            "For each requirement, read the linked tests and judge whether their assertions actually prove it.",
            test_adequacy,
            needs_llm=True,
            params={"entities": "requirement ids (default: all)", "limit": "max requirements (default 25)"},
        ),
        Workflow(
            "implementation_review",
            "Implementation review",
            "For each requirement with @rr implementation annotations, judge whether the code implements it.",
            implementation_review,
            needs_llm=True,
            params={"entities": "requirement ids (default: all)", "limit": "max requirements (default 25)"},
        ),
        Workflow(
            "mitigation_adequacy",
            "Mitigation adequacy",
            "For each risk, judge whether its mitigations' requirements actually control it, and whether the "
            "residual estimate is plausible.",
            mitigation_adequacy,
            needs_llm=True,
            params={"entities": "risk ids (default: all)", "limit": "max risks (default 25)"},
        ),
        Workflow(
            "risk_discovery",
            "Hazard discovery",
            "Preliminary hazard analysis: hazards and hazardous situations the risk analysis does not cover yet.",
            risk_discovery,
            needs_llm=True,
        ),
        Workflow(
            "assistant",
            "Assistant",
            "Describe a change in plain language; get proposed creates, updates and notes to review and apply.",
            assistant,
            needs_llm=True,
            params={"instruction": "what to do", "focus": "entity ids to include in full"},
        ),
    )
}
