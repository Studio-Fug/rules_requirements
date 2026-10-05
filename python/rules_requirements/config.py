# SPDX-License-Identifier: AGPL-3.0-or-later
"""Project-level configuration of the requirements model.

Every knob that differs between projects lives here, so the same tooling can
serve a project that calls its requirements ``REQ-0001`` and one that calls
them ``PR-12``. A model file may carry a ``config:`` section; anything it omits
falls back to the defaults below.

The defaults encode a conventional medical-device-software vocabulary:

* verification *levels* ordered by rigor (``analysis < simulation < sil < hil <
  hitl``) plus the unordered ``inspection`` (IEC 62304 §5.5–5.7 verification
  activities; the ordering is what lets a report say "under-verified");
* ordinal ``severities`` and ``likelihoods`` for risk estimation (ISO 14971 §5.5);
* an optional acceptability threshold on ``severity x likelihood`` for risk
  evaluation (ISO 14971 §6 / §7.4 residual risk).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from rules_requirements import labels

# Canonical entity kinds, in model (and report) order.
USER_NEED = "user_need"
REQUIREMENT = "requirement"
RISK = "risk"
MITIGATION = "mitigation"
TEST_METHOD = "test_method"
KINDS = (USER_NEED, REQUIREMENT, RISK, MITIGATION, TEST_METHOD)

# The YAML section that holds each kind.
SECTIONS = {
    USER_NEED: "user_needs",
    REQUIREMENT: "requirements",
    RISK: "risks",
    MITIGATION: "mitigations",
    TEST_METHOD: "test_methods",
}
KIND_BY_SECTION = {v: k for k, v in SECTIONS.items()}

DEFAULT_PREFIXES = {
    USER_NEED: "UN",
    REQUIREMENT: "REQ",
    RISK: "RISK",
    MITIGATION: "MIT",
    TEST_METHOD: "TM",
}


@dataclass(frozen=True)
class Level:
    """A verification rigor level.

    ``rank`` is ``None`` for unordered levels (e.g. ``inspection``): they are
    incomparable, so an unordered demand is met only by evidence at exactly that
    level, and unordered evidence never satisfies an ordered demand.
    """

    name: str
    rank: int | None
    description: str = ""


DEFAULT_LEVELS = (
    Level("analysis", 1, "Static argument: derivation, review of a proof, static analysis."),
    Level("simulation", 2, "Host-side unit or simulation test."),
    Level("sil", 3, "Software-in-the-loop: the integrated software against simulated I/O."),
    Level("hil", 4, "Hardware-in-the-loop: a component on real hardware."),
    Level("hitl", 5, "Full system on real hardware, end to end."),
    Level("inspection", None, "Manual or visual sign-off recorded as evidence."),
)

DEFAULT_SEVERITIES = ("negligible", "low", "medium", "high", "critical")
DEFAULT_LIKELIHOODS = ("rare", "unlikely", "possible", "likely", "certain")
MITIGATION_TYPES = ("inherent", "protective", "information")
STATUSES = ("draft", "proposed", "approved", "implemented", "obsolete")

# Coverage rules and whether each is an error or a warning by default. Projects
# that are still growing their model usually start with warnings.
DEFAULT_RULES = {
    # A user need no requirement satisfies can never be validated.
    "need-unsatisfied": "error",
    # A requirement that neither satisfies a need, refines another requirement,
    # nor implements a mitigation is untraceable ("gold plating" or a lost link).
    "requirement-orphan": "error",
    # A risk with no mitigation (and no recorded acceptance) is uncontrolled.
    "risk-unmitigated": "error",
    # A mitigation that no requirement implements is a promise, not a control.
    "mitigation-unimplemented": "error",
    # Residual risk above the acceptability threshold.
    "risk-unacceptable": "warning",
    # Unknown keys usually mean a typo that silently drops a trace.
    "unknown-field": "error",
    # --- claims: one owner per test case (0.3 defaults) ----------------------
    # A legacy verified_by item (a bare label, {target} or {target, level}):
    # it claims the whole target. Error by default from 0.4.
    "bare-target-reference": "warning",
    # A `whole: true` claim without a `reason`.
    "whole-target-reference": "warning",
    # `whole: true` on a target that reports per-case results (report time).
    "coarse-claim": "warning",
    # Any selector with a '*' (opt in to require literal case lists).
    "glob-selector": "off",
    # Two selectors of one entity on one target can select the same case.
    "redundant-selector": "warning",
    # A case's single tag names another entity than the one whose claim owns it.
    "tag-mismatch": "warning",
    # Model mode: a single-id tag on a case no claim covers.
    "unclaimed-tag": "warning",
    # A requirement property on a JUnit testsuite (no longer inherited by its cases).
    "suite-level-requirement": "warning",
    # The same case key twice in one run.
    "duplicate-case": "warning",
    # The case's own level differs from the level of the claim that owns it.
    "level-mismatch": "warning",
    # Equal case paths in different targets with different owners and no
    # source file to tell whether they are the same test code.
    "same-path-multiple-owners": "warning",
    # A requirement that refines two or more parents: refines must form a tree,
    # so each test case's evidence rolls up one chain of requirements.
    "multi-parent-refines": "error",
    # A requirement that implements a mitigation and has another parent (a
    # second mitigation, or a requirement it refines): each test case's
    # evidence would roll up two chains.
    "multi-parent-implements": "error",
    # A requirement with refining children also claims cases of its own.
    "parent-with-claims": "warning",
    # `rr scan`: a verifies annotation naming several ids. Error by default from 0.4.
    "multi-verifies-annotation": "warning",
    # A lock entry that no claim of its owner matches (model mode), or the
    # entry of an absent suite:/record: pseudo-target whose case now runs
    # under another target.
    "lock-stale": "error",
}
RULE_SEVERITIES = ("error", "warning", "off")

# Findings that keep one test case from counting toward two requirements, or
# that make the model unreadable. They are always errors: naming one under
# `config.rules` is itself an error (and `--strict` only escalates warnings).
HARD_ERRORS = (
    "shared-case",
    "same-code-multiple-owners",
    "bad-selector",
    "bad-target",
    "lock-owner-changed",
    "lock-invalid",
    "unknown-target",
)
# Report-time quarantine codes: not rules at all.
QUARANTINE_CODES = ("multi-tag", "attribution-conflict", "same-code-multiple-owners")

ATTRIBUTION_MODES = ("hybrid", "model")
FLAKY_POLICIES = ("accept", "flag", "under-verify", "fail")
SET_CONSISTENCY = ("off", "warn", "enforce")


@dataclass(frozen=True)
class Config:
    prefixes: Mapping[str, str] = field(default_factory=lambda: dict(DEFAULT_PREFIXES))
    # ``{prefix}`` is substituted; the result must match the whole id.
    id_pattern: str = r"{prefix}-\d+"
    levels: tuple[Level, ...] = DEFAULT_LEVELS
    # What a requirement demands when it names no method, and what evidence
    # provides when it names no level.
    default_level: str = "simulation"
    default_provided_level: str = "simulation"
    # Gaps for requirements demanding at most this level can be closed by an
    # agent alone ("autonomous"); anything above (or unordered) needs a human
    # or a physical bench ("human-gate").
    autonomous_max_level: str = "sil"
    # Cost-pyramid policy: a requirement demanding >= pyramid_min_level must also
    # be backed by evidence at one of pyramid_cheap_levels. Empty disables it.
    pyramid_min_level: str = "hil"
    pyramid_cheap_levels: tuple[str, ...] = ("analysis", "simulation")
    severities: tuple[str, ...] = DEFAULT_SEVERITIES
    likelihoods: tuple[str, ...] = DEFAULT_LIKELIHOODS
    # Risk evaluation: risk score = (severity index + 1) * (likelihood index + 1).
    # A residual score above this is flagged ``risk-unacceptable``. None = off.
    acceptable_risk_score: int | None = None
    # Severities that make a risk "high" (hoisted to the top of the report when
    # not fully mitigated).
    high_severities: tuple[str, ...] = ("high", "critical")
    rules: Mapping[str, str] = field(default_factory=lambda: dict(DEFAULT_RULES))
    # Extra annotation regexes (group 1 = the id list), e.g. a legacy
    # ``Requirements: PR-1, PR-2`` docstring convention.
    annotation_patterns: tuple[str, ...] = ()
    # --- attribution (one owner per test case) -------------------------------
    # Who owns a case no claim covers: "hybrid" lets a single-id tag own it,
    # "model" leaves it unowned (tags only cross-check claims). Neither mode
    # can give a case two owners.
    attribution: str = "hybrid"
    # The apparent name other modules give this repository: "@<main_repo>//x:y"
    # is read as "//x:y".
    main_repo: str = ""
    # The verification-set lock (generated by `rr sets lock`), relative to the
    # model file holding this config section. "" = no lock.
    sets_lock: str = ""
    # A pass that needed a retry: accept | flag | under-verify | fail.
    flaky: str = "under-verify"
    # Members of one set stamped with different builds: off | warn | enforce.
    set_consistency: str = "warn"
    # Groups of targets that run the same test code under another
    # configuration (labels as written; see variant_groups()).
    variants: tuple[tuple[str, ...], ...] = ()

    # --- derived helpers ----------------------------------------------------

    def prefix(self, kind: str) -> str:
        return self.prefixes[kind]

    def pattern(self, kind: str) -> str:
        """The id regex (unanchored) for ``kind``.

        ``{prefix}`` is substituted literally, so patterns may use braces of
        their own (``{prefix}-\\d{4}``).
        """
        return self.id_pattern.replace("{prefix}", re.escape(self.prefix(kind)))

    def id_regex(self, kind: str) -> re.Pattern[str]:
        return re.compile("^(?:" + self.pattern(kind) + ")$")

    def any_id_regex(self) -> re.Pattern[str]:
        """Matches any entity id of any kind, as a word inside free text.

        Use ``finditer(...).group(0)``: a pattern may contain capture groups.
        """
        kinds = sorted(KINDS, key=lambda k: len(self.prefix(k)), reverse=True)
        alts = "|".join("(?:" + self.pattern(k) + ")" for k in kinds)
        return re.compile(r"(?<![\w-])(?:" + alts + r")(?![\w-])")

    def kind_of(self, entity_id: str) -> str | None:
        for kind in KINDS:
            if self.id_regex(kind).match(entity_id):
                return kind
        return None

    def level(self, name: str) -> Level | None:
        name = (name or "").strip().lower()
        for lvl in self.levels:
            if lvl.name == name:
                return lvl
        return None

    def level_names(self) -> tuple[str, ...]:
        return tuple(lvl.name for lvl in self.levels)

    def rank(self, name: str) -> int | None:
        lvl = self.level(name)
        return lvl.rank if lvl else None

    def rule(self, name: str) -> str:
        if name in HARD_ERRORS:
            return "error"
        return self.rules.get(name, DEFAULT_RULES.get(name, "error"))

    def variant_groups(self) -> tuple[tuple[str, ...], ...]:
        """``variants`` with every label normalized (bad labels left out;
        validation reports them as ``bad-target``)."""
        out = []
        for group in self.variants:
            norm = [labels.try_normalize(label, self.main_repo) for label in group]
            out.append(tuple(dict.fromkeys(n for n in norm if n)))
        return tuple(out)

    def risk_score(self, severity: str, likelihood: str) -> int | None:
        if severity not in self.severities or likelihood not in self.likelihoods:
            return None
        return (self.severities.index(severity) + 1) * (self.likelihoods.index(likelihood) + 1)


def parse_config(raw: Mapping[str, Any] | None, errors: list[str]) -> Config:
    """Build a :class:`Config` from a ``config:`` mapping, collecting errors."""
    if not raw:
        return Config()
    if not isinstance(raw, Mapping):
        errors.append("config: must be a mapping")
        return Config()

    kwargs: dict[str, Any] = {}
    known = {
        "prefixes",
        "id_pattern",
        "levels",
        "default_level",
        "default_provided_level",
        "autonomous_max_level",
        "pyramid_min_level",
        "pyramid_cheap_levels",
        "severities",
        "likelihoods",
        "acceptable_risk_score",
        "high_severities",
        "rules",
        "annotation_patterns",
        "attribution",
        "main_repo",
        "sets_lock",
        "flaky",
        "set_consistency",
        "variants",
    }
    for key in raw:
        if key not in known:
            errors.append(f"config: unknown key {key!r}")

    for key in ("prefixes", "rules"):
        if key in raw and not isinstance(raw[key] or {}, Mapping):
            errors.append(f"config.{key}: must be a mapping")
            raw = {k: v for k, v in raw.items() if k != key}
    for key in (
        "levels",
        "severities",
        "likelihoods",
        "pyramid_cheap_levels",
        "high_severities",
        "annotation_patterns",
        "variants",
    ):
        if key in raw and not isinstance(raw[key] or [], (list, tuple)):
            errors.append(f"config.{key}: must be a list")
            raw = {k: v for k, v in raw.items() if k != key}

    if "prefixes" in raw:
        prefixes = dict(DEFAULT_PREFIXES)
        for kind, prefix in (raw["prefixes"] or {}).items():
            kind = KIND_BY_SECTION.get(kind, kind)
            if kind not in KINDS:
                errors.append(f"config.prefixes: unknown entity kind {kind!r}")
                continue
            prefixes[kind] = str(prefix)
        if len(set(prefixes.values())) != len(prefixes):
            errors.append("config.prefixes: prefixes must be distinct")
        kwargs["prefixes"] = prefixes

    if "id_pattern" in raw:
        pattern = str(raw["id_pattern"])
        if "{prefix}" not in pattern:
            errors.append("config.id_pattern: must contain '{prefix}'")
        else:
            try:
                re.compile(pattern.replace("{prefix}", "X"))
                kwargs["id_pattern"] = pattern
            except re.error as exc:
                errors.append(f"config.id_pattern: invalid regex: {exc}")

    if "levels" in raw:
        levels: list[Level] = []
        rank = 0
        for item in raw["levels"] or []:
            if isinstance(item, str):
                item = {"name": item}
            if not isinstance(item, Mapping):
                errors.append(f"config.levels: {item!r} must be a name or a mapping")
                continue
            name = str(item.get("name", "")).strip().lower()
            if not name:
                errors.append("config.levels: every level needs a name")
                continue
            if item.get("ordered", True):
                rank += 1
                levels.append(Level(name, rank, str(item.get("description", ""))))
            else:
                levels.append(Level(name, None, str(item.get("description", ""))))
        if not levels:
            errors.append("config.levels: at least one level is required")
        else:
            kwargs["levels"] = tuple(levels)

    for key in ("severities", "likelihoods", "pyramid_cheap_levels", "high_severities"):
        if key in raw:
            kwargs[key] = tuple(str(v).strip().lower() for v in raw[key] or ())
    if "annotation_patterns" in raw:
        pats = tuple(str(p) for p in raw["annotation_patterns"] or ())
        for p in pats:
            try:
                if re.compile(p).groups < 1:
                    errors.append(f"config.annotation_patterns: {p!r} needs a capture group")
            except re.error as exc:
                errors.append(f"config.annotation_patterns: invalid regex {p!r}: {exc}")
        kwargs["annotation_patterns"] = pats

    for key in (
        "default_level",
        "default_provided_level",
        "autonomous_max_level",
        "pyramid_min_level",
    ):
        if key in raw:
            kwargs[key] = str(raw[key] or "").strip().lower()

    if "acceptable_risk_score" in raw:
        val = raw["acceptable_risk_score"]
        if val is not None and not isinstance(val, int):
            errors.append("config.acceptable_risk_score: must be an integer or null")
        else:
            kwargs["acceptable_risk_score"] = val

    if "rules" in raw:
        rules = dict(DEFAULT_RULES)
        for name, sev in (raw["rules"] or {}).items():
            if sev is False:  # YAML 1.1 reads an unquoted `off` (and `no`) as false
                sev = "off"
            if name in HARD_ERRORS:
                errors.append(
                    f"config.rules: {name!r} is always an error and cannot be configured "
                    "(a test case verifies at most one requirement)"
                )
            elif name in QUARANTINE_CODES:
                errors.append(f"config.rules: {name!r} is a report-time quarantine, not a rule; it is not configurable")
            elif name not in DEFAULT_RULES:
                errors.append(f"config.rules: unknown rule {name!r}")
            elif sev not in RULE_SEVERITIES:
                errors.append(f"config.rules.{name}: must be one of {RULE_SEVERITIES}")
            else:
                rules[name] = sev
        kwargs["rules"] = rules

    _parse_attribution(raw, kwargs, errors)

    cfg = Config(**kwargs)
    try:
        cfg.any_id_regex()  # every kind's pattern combined, as the scanner uses it
    except re.error as exc:
        errors.append(f"config.id_pattern: cannot be combined across kinds ({exc}); use (?:...) not named groups")
        cfg = Config(**{k: v for k, v in kwargs.items() if k != "id_pattern"})

    names = cfg.level_names()
    for key in ("default_level", "default_provided_level", "autonomous_max_level"):
        if getattr(cfg, key) not in names:
            errors.append(f"config.{key}: {getattr(cfg, key)!r} is not a defined level")
    if cfg.pyramid_min_level and cfg.pyramid_min_level not in names:
        errors.append(f"config.pyramid_min_level: {cfg.pyramid_min_level!r} is not a level")
    for lvl in cfg.pyramid_cheap_levels:
        if lvl not in names:
            errors.append(f"config.pyramid_cheap_levels: {lvl!r} is not a defined level")
    for sev in cfg.high_severities:
        if sev not in cfg.severities:
            errors.append(f"config.high_severities: {sev!r} is not a defined severity")
    return cfg


def _parse_attribution(raw: Mapping[str, Any], kwargs: dict[str, Any], errors: list[str]) -> None:
    """The attribution keys: attribution, main_repo, sets_lock, flaky,
    set_consistency, variants."""
    for key, allowed in (
        ("attribution", ATTRIBUTION_MODES),
        ("flaky", FLAKY_POLICIES),
        ("set_consistency", SET_CONSISTENCY),
    ):
        if key not in raw:
            continue
        value = raw[key]
        if value is False:  # YAML 1.1 reads an unquoted `off` as false
            value = "off"
        value = str(value or "").strip().lower()
        if value not in allowed:
            errors.append(f"config.{key}: must be one of {allowed}, got {raw[key]!r}")
        else:
            kwargs[key] = value
    if "main_repo" in raw:
        name = raw["main_repo"]
        if not isinstance(name, str) or not labels.is_repo_name(name.strip()):
            errors.append(f"config.main_repo: {name!r} is not a repository name (write it without '@')")
        else:
            kwargs["main_repo"] = name.strip()
    if "sets_lock" in raw:
        path = raw["sets_lock"]
        if not isinstance(path, str) or not path.strip():
            errors.append("config.sets_lock: must be a path (relative to the model file holding this config)")
        else:
            kwargs["sets_lock"] = path.strip()
    if "variants" in raw:
        main_repo = kwargs.get("main_repo", "")
        groups: list[tuple[str, ...]] = []
        seen: dict[str, int] = {}
        for i, group in enumerate(raw["variants"] or ()):
            if not isinstance(group, (list, tuple)) or not all(isinstance(t, str) for t in group):
                errors.append(f"config.variants[{i}]: must be a list of target labels")
                continue
            members = tuple(t.strip() for t in group)
            norm = [labels.try_normalize(t, main_repo) for t in members]
            distinct = list(dict.fromkeys(n for n in norm if n))
            if len(set(norm)) < len(norm):
                errors.append(f"config.variants[{i}]: lists a target twice")
            if len(members) < 2:
                errors.append(f"config.variants[{i}]: a variant group needs at least two targets")
            for label in distinct:
                if label in seen and seen[label] != i:
                    errors.append(
                        f"config.variants[{i}]: {label} is also in variants[{seen[label]}] (merge the two groups)"
                    )
                seen.setdefault(label, i)
            groups.append(members)
        kwargs["variants"] = tuple(groups)
