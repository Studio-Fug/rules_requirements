# SPDX-License-Identifier: AGPL-3.0-or-later
"""``rr`` — the rules_requirements command line.

::

    rr validate  requirements/                    # shape, references, coverage rules
    rr scan      --model requirements/ --root .   # source annotations -> unknown ids fail
    rr report    --model requirements/ --evidence bazel-testlogs \\
                 --html report.html --json report.json --md report.md
    rr graph     --model requirements/ --format mermaid
    rr ingest    bazel-testlogs                   # debug: show parsed test cases
    rr case      --name "flash ok" --status passed --requirement REQ-21   # shell harnesses
    rr cases     --evidence bazel-testlogs        # every case key, to copy into the model
    rr attribution --model requirements/ --evidence bazel-testlogs --check   # who owns each case
    rr sets      lock --model requirements/ --evidence bazel-testlogs --write  # pin the sets
    rr check-report report.json                   # re-prove one owner per case from the JSON
    rr migrate   plan --model requirements/ --evidence bazel-testlogs --out attribution.rrplan
    rr migrate   verify --worksheet attribution.rrplan --evidence ci/bazel-testlogs --baseline bazel-testlogs

Under ``bazel run``, relative paths resolve against the workspace root.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from dataclasses import replace
from typing import Any, Iterable, Mapping

from rules_requirements import __version__, graph, ingest, report
from rules_requirements import annotations as rr_annotations
from rules_requirements.attribution import AttributionIssue
from rules_requirements.labels import read_known_targets, try_normalize
from rules_requirements.lock import NO_LOCK, Lock
from rules_requirements.model import Model, read_model
from rules_requirements.trace import FAILED, INCOMPLETE, INVALID, UNVERIFIED, Matrix, build_matrix
from rules_requirements.util import natural_key
from rules_requirements.validate import Issue, validate

DEFAULT_MODEL = "requirements"


def _path(p: str) -> str:
    """Resolve a user-supplied path against the invoking directory.

    Under ``bazel run`` a relative path means "relative to where I ran it" —
    unless it only exists relative to the current directory, which is how the
    runfiles paths baked into generated mains (``rr_annotations_check``) look.
    """
    if not p or p == "-" or os.path.isabs(p):
        return p
    base = os.environ.get("BUILD_WORKING_DIRECTORY") or os.environ.get("BUILD_WORKSPACE_DIRECTORY")
    if not base:
        return p
    candidate = os.path.join(base, p)
    if os.path.exists(candidate) or not os.path.exists(p):
        return candidate
    return p


def _root() -> str:
    return os.environ.get("BUILD_WORKSPACE_DIRECTORY") or os.getcwd()


def _write(path: str, text: str) -> None:
    path = _path(path)
    if path == "-":
        sys.stdout.write(text)
        return
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _kv(pairs: list[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in pairs or []:
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            raise SystemExit(f"expected KEY=VALUE, got {item!r}")
        out[key.strip()] = value.strip()
    return out


# Claim conflicts `rr migrate` exists to resolve: it plans and applies on a
# model that still has them (every other error still stops it).
_MIGRATE_TOLERATES = ("shared-case", "same-code-multiple-owners")


def _with_lock(model: Model, sets_lock: str = "", no_lock: bool = False) -> Model:
    """``model`` reading the lock at ``sets_lock`` instead of the configured
    one (``--sets-lock``), or none at all (``--no-lock``)."""
    if no_lock:
        return replace(model, config=replace(model.config, sets_lock=""))
    if sets_lock:
        return replace(model, config=replace(model.config, sets_lock=os.path.abspath(_path(sets_lock))))
    return model


def _lock_arg(args: argparse.Namespace) -> Lock | None:
    """The ``lock=`` for build_matrix: NO_LOCK under ``--no-lock``, else the configured one."""
    return NO_LOCK if getattr(args, "no_lock", False) else None


def _load(
    paths: list[str],
    strict: bool = False,
    quiet: bool = False,
    known_targets: list[str] | None = None,
    tolerate: tuple[str, ...] = (),
    *,
    sets_lock: str = "",
    no_lock: bool = False,
    creating_lock: bool = False,
) -> tuple[Model, bool]:
    """Read + validate; print issues. Returns (model, ok); errors whose code
    is in ``tolerate`` are printed but do not make the model invalid.
    ``sets_lock`` / ``no_lock`` replace the configured verification-set lock.
    ``creating_lock`` (``rr sets lock``): a lock file that is missing or blank
    is about to be written, so it is not validated (no ``lock-invalid``)."""
    resolved = [_path(p) for p in paths]
    for p in resolved:
        if not os.path.exists(p):
            print(f"rr: model path not found: {p}", file=sys.stderr)
            return Model(), False
    model, warnings = read_model(resolved, root=_root())
    model = _with_lock(model, sets_lock, no_lock)
    checked = model
    lock_path = model.lock_path()
    if creating_lock and lock_path and (not os.path.exists(lock_path) or _blank_file(lock_path)):
        checked = replace(model, config=replace(model.config, sets_lock=""))
    issues = validate(checked, strict=strict, known_targets=known_targets)
    for w in warnings:
        print(f"warning: [unknown-field] {w}", file=sys.stderr)
    errors = 0
    for issue in issues:
        errors += issue.severity == "error" and issue.code not in tolerate
        if not quiet or issue.severity == "error":
            print(str(issue), file=sys.stderr)
    return model, errors == 0


def _known_targets(path: str) -> list[str] | None:
    """The labels of a `--known-targets` file (`bazel query 'tests(//...)'`)."""
    if not path:
        return None
    try:
        with open(_path(path), encoding="utf-8") as fh:
            known, bad = read_known_targets(fh.read())
    except OSError as exc:
        raise SystemExit(f"rr validate: cannot read --known-targets: {exc}") from None
    for line in bad:
        print(f"warning: --known-targets: not a label: {line!r}", file=sys.stderr)
    return known


# `rr validate` JUnit: one case per check family (`rr.validate::<family>`).
VALIDATE_FAMILIES = ("shape", "references", "coverage-rules", "claims", "lock")
_FAMILY = {
    **dict.fromkeys(
        ("shape", "unknown-field", "bad-id", "bad-enum", "bad-status", "bad-level", "missing-level", "duplicate-id"),
        "shape",
    ),
    **dict.fromkeys(("bad-reference", "dangling-reference", "refines-cycle"), "references"),
    **dict.fromkeys(
        (
            "shared-case",
            "same-code-multiple-owners",
            "bad-selector",
            "bad-target",
            "unknown-target",
            "redundant-selector",
            "glob-selector",
            "bare-target-reference",
            "whole-target-reference",
            "parent-with-claims",
        ),
        "claims",
    ),
    **dict.fromkeys(("lock-invalid", "lock-owner-changed", "lock-stale"), "lock"),
}


def validate_family(issue: Issue) -> str:
    """The check family an issue belongs to (``coverage-rules`` for every configurable coverage rule)."""
    if issue.code == "unknown-target" and "locked cases" in issue.message:
        return "lock"
    return _FAMILY.get(issue.code, "coverage-rules")


def write_validate_junit(path: str, issues: Iterable[Issue]) -> None:
    """One JUnit case per check family: failed when the family has an error."""
    from rules_requirements.hooks.junit_writer import JUnitWriter

    by_family: dict[str, list[Issue]] = {f: [] for f in VALIDATE_FAMILIES}
    for issue in issues:
        by_family[validate_family(issue)].append(issue)
    writer = JUnitWriter("rr.validate", classname="rr.validate", file="")
    for family, found in by_family.items():
        errors = [str(i) for i in found if i.severity == "error"]
        warnings = [str(i) for i in found if i.severity != "error"]
        status = "failed" if errors else "passed"
        message = "\n".join(errors) if errors else ("\n".join(warnings) if warnings else "")
        writer.add(family, None, status=status, message=message)
    writer.write(path)


def cmd_validate(args: argparse.Namespace) -> int:
    known = _known_targets(args.known_targets)
    model, ok = _load(args.model, strict=args.strict, known_targets=known, sets_lock=args.sets_lock)
    junit = _path(args.junit) if args.junit else os.environ.get("XML_OUTPUT_FILE", "")
    if junit:
        write_validate_junit(junit, validate(model, strict=args.strict, known_targets=known))
    if args.format == "json":
        issues = validate(model, strict=args.strict, known_targets=known)
        print(
            json.dumps(
                [
                    {
                        "severity": i.severity,
                        "code": i.code,
                        "message": i.message,
                        "entity": i.entity,
                        "path": i.location.path,
                        "line": i.location.line,
                    }
                    for i in issues
                ],
                indent=2,
            )
        )
    elif ok:
        n = {k: len(model.section(k)) for k in ("user_need", "requirement", "risk", "mitigation", "test_method")}
        print(
            f"model OK: {n['user_need']} user needs, {n['requirement']} requirements, "
            f"{n['risk']} risks, {n['mitigation']} mitigations, {n['test_method']} test methods"
        )
    return 0 if ok else 1


def _scan(args: argparse.Namespace, model: Model) -> list[rr_annotations.Reference]:
    root = _path(args.root) if args.root else _root()
    files = getattr(args, "files", None)
    return rr_annotations.scan(
        root, model.config, include=args.include or (), exclude=args.exclude or (), files=files or None
    )


def cmd_scan(args: argparse.Namespace) -> int:
    model, ok = _load(args.model, quiet=True)
    if not ok and not args.ignore_model_errors:
        return 2
    refs = _scan(args, model)
    unknown = rr_annotations.unknown_references(refs, model)
    severity = model.config.rule(rr_annotations.MULTI_VERIFIES)
    if args.strict and severity == "warning":
        severity = "error"
    multi = rr_annotations.multi_verifies(refs) if severity != "off" else []
    if args.list:
        for ref in refs:
            sym = f" [{ref.symbol}]" if ref.symbol else ""
            txt = f" — {ref.text}" if ref.text else ""
            print(f"{ref.path}:{ref.line}: {ref.relation} {', '.join(ref.ids)}{sym}{txt}")
    if args.json:
        _write(args.json, json.dumps([r.to_dict() for r in refs], indent=2) + "\n")
    print(f"scanned: {len(refs)} annotation(s), {sum(len(r.ids) for r in refs)} reference(s)")
    for _ref, message in multi:
        print(f"{severity}: [{rr_annotations.MULTI_VERIFIES}] {message}", file=sys.stderr)
    if unknown:
        print(f"{len(unknown)} reference(s) to undefined ids:", file=sys.stderr)
        for ref, rid in unknown:
            print(f"  {ref.path}:{ref.line}: {rid}", file=sys.stderr)
        return 1
    print("all references resolve.")
    return 1 if multi and severity == "error" else 0


def _lane(args: argparse.Namespace, model: Model) -> report.Lane:
    """``--lane NAME`` and ``--lane-targets FILE`` (labels, one per line)."""
    targets: frozenset[str] | None = None
    if getattr(args, "lane_targets", ""):
        try:
            with open(_path(args.lane_targets), encoding="utf-8") as fh:
                labels, bad = read_known_targets(fh.read())
        except OSError as exc:
            raise SystemExit(f"rr report: cannot read --lane-targets: {exc}") from None
        for line in bad:
            print(f"warning: --lane-targets: not a label: {line!r}", file=sys.stderr)
        norm = (try_normalize(label, model.config.main_repo) for label in labels)
        targets = frozenset(n for n in norm if n)
    return report.Lane(getattr(args, "lane", "") or "", targets)


def _print_quarantines(matrix: Matrix, warn: bool = False) -> None:
    """One ``ATTRIBUTION ERROR:`` line per quarantined case (stderr)."""
    quarantined = matrix.attribution.quarantined if matrix.attribution is not None else ()
    for q in quarantined:
        print(f"ATTRIBUTION ERROR: {q.code}: {q.detail}", file=sys.stderr)
    if quarantined:
        tail = (
            "--on-attribution-error=warn: the exit status ignores them"
            if warn
            else "exit 3 (--on-attribution-error=warn to report without failing)"
        )
        print(
            f"rr: {len(quarantined)} quarantined test case(s) count for no requirement; every entity they name "
            f"is INVALID; {tail}",
            file=sys.stderr,
        )


def cmd_report(args: argparse.Namespace) -> int:
    for spec in args.ingestor or []:
        ingest.load_ingestor(spec)
    model, ok = _load(args.model, strict=args.strict, quiet=True, sets_lock=args.sets_lock, no_lock=args.no_lock)
    if not ok:
        print("rr: requirements model is invalid (see above)", file=sys.stderr)
        return 2
    evidence = ingest.collect([_path(p) for p in args.evidence], only=args.format or None)
    refs = _scan(args, model) if args.scan else None
    matrix = build_matrix(model, evidence, current_build=_kv(args.current_build), references=refs, lock=_lock_arg(args))
    lane = _lane(args, model)

    outputs = {"html": args.html, "json": args.json, "md": args.md}
    for extra in args.out or []:
        ext = extra.rsplit(".", 1)[-1].lower()
        fmt = {"htm": "html", "markdown": "md"}.get(ext, ext)
        if fmt not in report.FORMATS:
            print(f"rr: cannot infer report format from {extra!r}", file=sys.stderr)
            return 2
        outputs[fmt] = extra
    # The reports are written first, whatever the exit status.
    for fmt, path in outputs.items():
        if path:
            _write(path, report.FORMATS[fmt](matrix, args.title, lane=lane))
    if args.queue_out:
        # Gaps only out-of-lane members cause are another lane's work.
        away = {id(g) for g in report.out_of_lane_gaps(matrix, lane)}
        queue = [g.to_dict() for g in matrix.gaps if id(g) not in away]
        _write(args.queue_out, json.dumps({"queue": queue}, indent=2) + "\n")

    c = matrix.counts()
    print(
        f"evidence: {len(evidence.files)} file(s), {c['test_cases']} test case(s) | "
        f"validation: {c['user_needs_validated']}/{c['user_needs']} needs | "
        f"verification: {c['requirements_verified']}/{c['requirements']} requirements "
        f"({c['requirements_failed']} failed, {c['requirements_unverified']} unverified, "
        f"{c['requirements_under_verified']} under-verified, {c['requirements_invalid']} invalid, "
        f"{c['requirements_incomplete']} incomplete) | "
        f"risks: {c['risks_mitigated']}/{c['risks']} mitigated | gaps: {c['gaps']}",
        file=sys.stderr,
    )
    reqs = matrix.of_kind("requirement")
    # Any FAILED verdict gates: a failing validation test on a need, or a
    # failing effectiveness test on a mitigation, counts as much as a
    # requirement's. INVALID (a quarantined case names it) counts as failed,
    # INCOMPLETE as unverified.
    failed = sorted((v.id for v in matrix.verdicts.values() if v.status == FAILED), key=natural_key)
    invalid = sorted((v.id for v in matrix.verdicts.values() if v.status == INVALID), key=natural_key)
    unverified = [v.id for v in reqs if v.status == UNVERIFIED]
    incomplete = [v.id for v in reqs if v.status == INCOMPLETE]
    current = _kv(args.current_build)
    if current:
        keys = {k for cs in evidence.cases for k in cs.artifact}
        if not keys & set(current):
            print(
                "warning: --current-build keys (" + ", ".join(sorted(current)) + ") match no artifact identity "
                "recorded in the evidence"
                + (f" ({', '.join(sorted(keys))})" if keys else "")
                + "; nothing can be stale",
                file=sys.stderr,
            )
    violations = matrix.pyramid_violations()
    if failed:
        print("FAILED: " + ", ".join(failed), file=sys.stderr)
    if invalid:
        print("INVALID: " + ", ".join(invalid), file=sys.stderr)
    # A quarantined case counts for nobody: say so loudly (one line per case).
    warn = args.on_attribution_error == "warn"
    _print_quarantines(matrix, warn)
    # Attribution issues are gaps; one at error severity (a hard error, a rule
    # set to error, or any warning under --strict) also gates, one per line.
    attribution_errors: list[AttributionIssue] = []
    attribution_warnings: list[AttributionIssue] = []
    for issue in matrix.attribution.issues if matrix.attribution is not None else ():
        error = issue.severity == "error" or (args.strict and issue.severity == "warning")
        (attribution_errors if error else attribution_warnings).append(issue)
    for issue in attribution_errors:
        print(f"ATTRIBUTION ERROR: [{issue.code}] {issue.message}", file=sys.stderr)
    if attribution_warnings:
        by_code: dict[str, int] = {}
        for issue in attribution_warnings:
            by_code[issue.code] = by_code.get(issue.code, 0) + 1
        print(
            f"attribution: {len(attribution_warnings)} warning(s) ("
            + ", ".join(f"{code} x{n}" for code, n in sorted(by_code.items()))
            + "), listed as gaps",
            file=sys.stderr,
        )
    if violations and args.pyramid_policy != "off":
        print(f"cost-pyramid {args.pyramid_policy}: " + ", ".join(violations), file=sys.stderr)
    if matrix.unknown_evidence:
        print("evidence references undefined ids: " + ", ".join(matrix.unknown_evidence), file=sys.stderr)

    rc = 1 if attribution_errors else 0
    if args.fail_on in ("failed", "unverified", "gaps") and (failed or invalid):
        rc = 1
    if args.fail_on in ("unverified", "gaps") and unverified:
        print("UNVERIFIED: " + ", ".join(unverified), file=sys.stderr)
        rc = 1
    if args.fail_on in ("unverified", "gaps") and incomplete:
        print("INCOMPLETE: " + ", ".join(incomplete), file=sys.stderr)
        rc = 1
    if args.fail_on == "gaps" and matrix.gaps:
        rc = 1
    if args.pyramid_policy == "error" and violations:
        rc = 1
    if matrix.attribution is not None and matrix.attribution.quarantined and not warn:
        rc = 3  # the reports are written; the quarantine gates
    return rc


def cmd_graph(args: argparse.Namespace) -> int:
    model, ok = _load(args.model, quiet=True)
    if not ok:
        return 2
    statuses: dict[str, str] = {}
    if args.cases and not args.evidence:
        print("rr graph: --cases needs --evidence (a case's owner comes from attribution over it)", file=sys.stderr)
        return 2
    attribution = None
    if args.evidence:
        matrix = build_matrix(model, ingest.collect([_path(p) for p in args.evidence]))
        statuses = {k: v.status for k, v in matrix.verdicts.items()}
        attribution = matrix.attribution
    nodes, edges = graph.build(model, statuses, include_methods=args.methods)
    if args.cases and attribution is not None:
        case_nodes, case_edges = graph.cases(attribution, {n.id for n in nodes})
        nodes, edges = nodes + case_nodes, edges + case_edges
    render = {"dot": graph.to_dot, "mermaid": graph.to_mermaid, "json": graph.to_json, "svg": graph.to_svg}
    _write(args.out, render[args.format](nodes, edges))
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    for spec in args.ingestor or []:
        ingest.load_ingestor(spec)
    from rules_requirements.case_keys import run_dims_from_path

    ev = ingest.collect([_path(p) for p in args.paths], only=args.format or None)
    rows: list[dict[str, Any]] = [
        {
            "name": c.name,
            "classname": c.classname,
            "status": c.status,
            "declared": list(c.declared),  # tags, never owners
            "requirements": list(c.declared),  # deprecated name of "declared" (0.3.x)
            "level": c.level,
            "artifact": c.artifact,
            "target": c.target,
            "scope": c.scope,
            "synthetic": c.synthetic,
            "attempt": run_dims_from_path(c.source).attempt,
            "file": c.file,
            "line": c.line,
            "source": c.source,
        }
        for c in ev.cases
    ]
    _print_ingest_issues(ev)
    print(json.dumps({"files": ev.files, "cases": rows}, indent=2))
    return 0


def _print_ingest_issues(ev: ingest.Evidence) -> None:
    for issue in ev.issues:
        print(issue, file=sys.stderr)


def _collect_evidence(args: argparse.Namespace, command: str, given: list[str] | None = None) -> ingest.Evidence | None:
    """The evidence named by ``--evidence`` (or ``given``); None (after saying why) when it holds no evidence at all.

    A path that contributes no evidence file is a warning: a mistyped path
    must not read as "this target has no results".
    """
    for spec in getattr(args, "ingestor", None) or []:
        ingest.load_ingestor(spec)
    given = args.evidence if given is None else given
    paths = [_path(p) for p in given]
    ev = ingest.collect(paths, only=args.format or None)
    used = set(ev.files)
    for name, path in zip(given, paths):
        if not any(f in used for f in ingest.expand([path])):
            print(f"warning: [no-evidence] {name}: no evidence file found there", file=sys.stderr)
    _print_ingest_issues(ev)
    if not ev.files:
        print(f"rr {command}: no evidence found in {' '.join(given) or '(nothing given)'}", file=sys.stderr)
        return None
    return ev


def _tsv(text: str) -> str:
    """One TSV field: tabs, newlines and backslashes escaped (the JSON output keeps them)."""
    return text.replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r")


def cmd_cases(args: argparse.Namespace) -> int:
    from rules_requirements.case_keys import index_cases, is_unscoped

    ev = _collect_evidence(args, "cases")
    if ev is None:
        return 2
    rows = [r for r in index_cases(ev).values() if not args.target or r.key.target in args.target]
    if args.json:
        print(json.dumps([r.to_dict() for r in rows], indent=2, ensure_ascii=False))
    else:
        for r in rows:
            flags = [f for f in ("synthetic", "target_scope", "flaky", "duplicate") if getattr(r, f)]
            fields = [str(r.key), r.status, ",".join(r.declared) or "-", ",".join(flags) or "-", r.file or "-"]
            print("\t".join(_tsv(f) for f in fields))
    targets = {r.key.target for r in rows}
    print(f"{len(rows)} case(s) in {len(targets)} target(s)", file=sys.stderr)
    unscoped = sorted(t for t in targets if is_unscoped(t))
    if unscoped:
        print(
            f"warning: [unscoped-evidence] {len(unscoped)} suite(s) outside a bazel-testlogs tree cannot be pinned "
            "to a build target: " + ", ".join(unscoped),
            file=sys.stderr,
        )
    return 0


def cmd_migrate_plan(args: argparse.Namespace) -> int:
    from rules_requirements import migrate

    model, ok = _load(args.model, quiet=True, tolerate=_MIGRATE_TOLERATES)
    if not ok:
        print("rr: requirements model is invalid (see above)", file=sys.stderr)
        return 2
    previous = None
    if args.merge:
        try:
            previous = migrate.load_worksheet(_path(args.merge))
        except migrate.WorksheetError as exc:
            print(f"rr migrate: {exc}", file=sys.stderr)
            return 2
        problems = migrate.check_worksheet(previous)
        for problem in problems:
            print(f"rr migrate: {args.merge}: {problem}", file=sys.stderr)
        if problems:
            return 2
    evidence = _collect_evidence(args, "migrate plan")
    if evidence is None:
        return 2
    plan = migrate.census(model, evidence)
    doc = migrate.worksheet(
        plan,
        inputs={"model": migrate.plan_paths(args.model), "evidence": migrate.plan_paths(args.evidence)},
        previous=previous,
    )
    outputs = [(args.out, migrate.render_yaml), (args.json, migrate.render_json), (args.md, migrate.render_markdown)]
    if not any(path for path, _ in outputs):
        outputs[0] = ("-", migrate.render_yaml)
    for path, render in outputs:
        if path:
            _write(path, render(doc))
    s = doc["summary"]
    print(
        f"{s['contested_units']} of {s['attributed']} attributed evidence unit(s) count toward two or more "
        f"entities; {s['shared_targets']} target(s) shared between requirements; "
        f"{s['decided']} decided, {s['open']} open",
        file=sys.stderr,
    )
    _warn_no_evidence(migrate.model_edits(doc))
    return 0


def _warn_no_evidence(edits: list[dict[str, str]]) -> None:
    missing: dict[str, list[str]] = {}
    for e in edits:
        if e["action"] == "no-evidence":
            missing.setdefault(e["target"], []).append(e["requirement"])
    for target, reqs in missing.items():
        print(
            f"warning: [no-evidence] {target} (verified_by of {', '.join(reqs)}) has no result in the evidence "
            "given: nothing is decided about it, and its references are not unused",
            file=sys.stderr,
        )


def _symlinked_writes(root: str, paths: Iterable[str]) -> list[tuple[str, str]]:
    """``(path, realpath)`` of each path under ``root`` that is, or lies under, a symlink."""
    root_real = os.path.realpath(root)
    out = []
    for path in sorted(paths):
        real = os.path.realpath(os.path.join(root, path))
        if real != os.path.join(root_real, os.path.normpath(path)):
            out.append((path, real))
    return out


def _decided_worksheet(args: argparse.Namespace) -> tuple[dict[str, Any], Model | None] | None:
    """The worksheet ``args.worksheet``, checked (against ``--model``, else the
    model it was planned with when that is still there), with that model (None
    without one); None, after saying why, when it cannot be used."""
    from rules_requirements import migrate

    try:
        doc = migrate.load_worksheet(_path(args.worksheet))
    except migrate.WorksheetError as exc:
        print(f"rr migrate: {exc}", file=sys.stderr)
        return None
    model = None
    model_paths = list(args.model)
    if not model_paths:
        # The model the worksheet was planned with, when it is still there.
        planned = (doc.get("inputs") or {}).get("model") if isinstance(doc.get("inputs"), dict) else None
        if isinstance(planned, list) and planned and all(os.path.exists(_path(str(p))) for p in planned):
            model_paths = [str(p) for p in planned]
            print(f"rr migrate: checking owners against the planned model: {' '.join(model_paths)}", file=sys.stderr)
    if model_paths:
        model, ok = _load(model_paths, quiet=True, tolerate=_MIGRATE_TOLERATES)
        if not ok:
            print("rr: requirements model is invalid (see above)", file=sys.stderr)
            return None
    problems = migrate.check_worksheet(doc, model)
    for problem in problems:
        print(f"rr migrate: {args.worksheet}: {problem}", file=sys.stderr)
    return None if problems else (doc, model)


def cmd_migrate_apply(args: argparse.Namespace) -> int:
    import difflib

    from rules_requirements import migrate, tag_codemod

    if args.stage == "model":
        try:
            sheet = migrate.load_worksheet(_path(args.worksheet))
        except migrate.WorksheetError as exc:
            print(f"rr migrate: {exc}", file=sys.stderr)
            return 2
        return _migrate_apply_model(args, sheet)
    checked = _decided_worksheet(args)
    if checked is None:
        return 2
    doc = checked[0]
    root = _path(args.root) if args.root else _root()
    decided = migrate.decisions(doc)
    res = tag_codemod.apply_tags(
        decided,
        root,
        only=args.only or (),
        unassigned=args.unassigned,
        line_length=args.line_length,
        files_of=migrate.case_files(doc),
        trust_main_guard=args.trust_main_guard,
    )
    if args.trust_main_guard:
        print(
            'rr migrate: WARNING: --trust-main-guard: code only an if __name__ == "__main__": block reaches was '
            "left out of the static guards. That exclusion is best-effort (static analysis cannot prove what "
            "Python runs at import); only use it with a definitive check: the collection check, or rr migrate "
            "verify against fresh test evidence before merging.",
            file=sys.stderr,
        )
    writes = {f.path for f in res.to_write(partial=args.partial)}
    held = res.held_back(partial=args.partial)
    # Before writing anything, prove the rewrite against pytest itself: the
    # static guards cannot see tests pytest collects dynamically. Whenever
    # anything would be written -- --partial and --dry-run included.
    check_refused = False
    if writes:
        if args.no_collect_check:
            print(
                "rr migrate: WARNING: --no-collect-check: only the static guards ran. They are best-effort and "
                "cannot see tests pytest collects dynamically (factories, metaclasses, setattr/exec, ...); the "
                "rewrite was NOT verified against pytest --collect-only.",
                file=sys.stderr,
            )
        elif not _collect_check(args, root, res, writes, decided):
            if not args.dry_run:
                return 1
            check_refused = True
            held = {**held, **{p: "the collection check refused the rewrite (see above)" for p in writes}}
            writes = set()
    if writes and not args.dry_run:
        # Never write through a symlink: the static guards and the collection
        # check judged the file at this path, and a link (even one swapped in
        # since) would send the rewrite somewhere else, possibly outside --root.
        through = _symlinked_writes(root, writes)
        if through:
            for path, real in through:
                print(f"rr migrate: {path} is reached through a symlink (it resolves to {real})", file=sys.stderr)
            print("rr migrate: refusing to write through a symlink; nothing written", file=sys.stderr)
            return 1
    for f in res.changed:
        if args.dry_run:
            sys.stdout.writelines(
                difflib.unified_diff(
                    f.old_text.splitlines(keepends=True),
                    f.new_text.splitlines(keepends=True),
                    f"a/{f.path}",
                    f"b/{f.path}",
                )
            )
        elif f.path in writes:
            # O_NOFOLLOW: a link swapped in after the check above is refused, not followed.
            fd = os.open(os.path.join(root, f.path), os.O_WRONLY | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "w", encoding=f.encoding, newline="") as fh:
                fh.write(f.new_text)
        if f.path in writes:
            print(f"{'would rewrite' if args.dry_run else 'rewrote'} {f.path}:", file=sys.stderr)
        else:
            print(
                f"{'would hold back' if args.dry_run else 'held back'} {f.path} (left unchanged: {held[f.path]}):",
                file=sys.stderr,
            )
        for change in f.changes:
            print(f"  {change}", file=sys.stderr)
    for f in res.refused:
        print(f"refused {f.path} (left unchanged):", file=sys.stderr)
        for reason in f.reasons:
            print(f"  {reason}", file=sys.stderr)
    if res.unmatched:
        print(
            f"{len(res.unmatched)} decided case(s) are not defined where the codemod looked; check them by hand:",
            file=sys.stderr,
        )
        for key, why in res.unmatched:
            print(f"  {key}: {why}", file=sys.stderr)
    if res.unresolved:
        print(f"{len(res.unresolved)} decided case(s) have no Python test to rewrite:", file=sys.stderr)
        for key, why in res.unresolved:
            print(f"  {key}: {why}", file=sys.stderr)
    if res.outside:
        print(f"{len(res.outside)} decided case(s) outside --only were skipped", file=sys.stderr)
    if res.untagged:
        print(
            f"{len(res.untagged)} decided case(s) declare no id in their source; their owner is set by "
            "verified_by in the model:",
            file=sys.stderr,
        )
        for key in res.untagged:
            print(f"  {key}", file=sys.stderr)
    all_edits = migrate.model_edits(doc)
    edits = [e for e in all_edits if e["action"] in ("remove", "split")]
    if edits:
        print("model edits the decisions imply (verified_by):", file=sys.stderr)
        for e in edits:
            print(f"  {e['requirement']}: {e['action']} {e['target']} ({e['reason']})", file=sys.stderr)
    _warn_no_evidence(all_edits)
    if res.mismatched:
        print(
            f"{len(res.mismatched)} decided case(s) would not get their owner from the rewritten sources; "
            "nothing is written:",
            file=sys.stderr,
        )
        for key, why in res.mismatched:
            print(f"  {key}: {why}", file=sys.stderr)
    elif check_refused:
        print("apply would write nothing: the collection check refused the rewrite (see above)", file=sys.stderr)
    elif held and args.partial:
        print(
            f"{len(held)} rewritten file(s) held back: each holds a decided case not found in it, or is linked to "
            "a file that was refused or holds one (see above)",
            file=sys.stderr,
        )
    elif held:
        causes = []
        if res.refused:
            causes.append(f"{len(res.refused)} file(s) were refused")
        if res.unmatched:
            causes.append(f"{len(res.unmatched)} decided case(s) were not found in their module")
        print(
            f"nothing written: {len(held)} rewritten file(s) held back because {' and '.join(causes)}; fix "
            "those, or pass --partial to write the files not linked to them",
            file=sys.stderr,
        )
    if args.dry_run:
        print(f"{len(writes)} file(s) would be rewritten, {len(res.refused)} refused", file=sys.stderr)
    else:
        print(f"{len(writes)} file(s) rewritten, {len(res.refused)} refused", file=sys.stderr)
    return 1 if res.blocked or check_refused else 0


def _planned(doc: Mapping[str, Any], key: str) -> list[str]:
    """The worksheet's recorded ``inputs.<key>`` paths, when they all still exist."""
    planned = (doc.get("inputs") or {}).get(key) if isinstance(doc.get("inputs"), dict) else None
    if isinstance(planned, list) and planned and all(os.path.exists(_path(str(p))) for p in planned):
        return [str(p) for p in planned]
    return []


def _migrate_apply_model(args: argparse.Namespace, doc: Mapping[str, Any]) -> int:
    """``rr migrate apply --stage model``: write an explicit selector for every
    current owner, after proving that the owner table stays unchanged under
    ``attribution: model`` and that ``check_claims`` passes."""
    import difflib

    from rules_requirements import edit, migrate

    model_paths = list(args.model) or _planned(doc, "model")
    if not model_paths:
        print("rr migrate: --stage model needs the model (--model, or the worksheet's inputs)", file=sys.stderr)
        return 2
    model, ok = _load(model_paths, quiet=True)
    if not ok:
        print("rr: requirements model is invalid (see above)", file=sys.stderr)
        return 2
    problems = migrate.check_worksheet(doc, model)
    for problem in problems:
        print(f"rr migrate: {args.worksheet}: {problem}", file=sys.stderr)
    if problems:
        return 2
    if not args.evidence:
        args.evidence = _planned(doc, "evidence")
        if args.evidence:
            print(f"rr migrate: owners from the planned evidence: {' '.join(args.evidence)}", file=sys.stderr)
    evidence = _collect_evidence(args, "migrate apply")
    if evidence is None:
        return 2
    stage = migrate.model_stage(model, evidence, doc, compress=args.compress)
    written = set(stage.from_worksheet)
    for key, decision in stage.unseen.items():
        how = (
            f"a literal selector of {decision} is written"
            if key in written
            else ("no claim may select it" if decision == migrate.NONE else f"a claim of {decision} selects it")
        )
        print(f"note: {key}: decided {decision} on the worksheet, not in the evidence given: {how}", file=sys.stderr)
    for key, ent_id in stage.skipped.items():
        print(
            f"note: {key}: skipped in this evidence, now claimed by {ent_id}, which reads INCOMPLETE until it runs; "
            "make it runnable, or decide it none on the worksheet (and leave it out of the claims)",
            file=sys.stderr,
        )
    if stage.refused:
        for reason in stage.refused:
            print(f"rr migrate: {reason}", file=sys.stderr)
        print("rr migrate: --stage model refused; nothing written", file=sys.stderr)
        return 1
    root = model.root or _root()
    by_file: dict[str, list[str]] = {}
    for ent_id in stage.data:
        ent = model.get(ent_id)
        where = ent.location.path if ent is not None else ""
        if not where:
            print(f"rr migrate: {ent_id}: no source file to write", file=sys.stderr)
            return 1
        by_file.setdefault(where if os.path.isabs(where) else os.path.join(root, where), []).append(ent_id)
    rewritten: dict[str, tuple[str, str]] = {}
    for path, ids in sorted(by_file.items()):
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        new = text
        try:
            for ent_id in ids:
                step = edit.update_entity(new, ent_id, stage.data[ent_id])
                edit.verify(new, step, {ent_id: stage.data[ent_id]})
                new = step
        except (edit.EditError, KeyError) as exc:
            print(f"rr migrate: {path}: {exc}; nothing written", file=sys.stderr)
            return 1
        rewritten[path] = (text, new)
    for path, (old, new) in rewritten.items():
        shown = report._shown(path)
        if args.dry_run:
            sys.stdout.writelines(
                difflib.unified_diff(old.splitlines(True), new.splitlines(True), f"a/{shown}", f"b/{shown}")
            )
        else:
            with open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write(new)
        print(f"{'would rewrite' if args.dry_run else 'rewrote'} {shown}", file=sys.stderr)
    for ent_id in sorted(set(stage.additions) | set(stage.whole), key=natural_key):
        for target, selectors in stage.additions.get(ent_id, {}).items():
            print(f"  {ent_id}: {target}: {', '.join(selectors)}", file=sys.stderr)
        for target in stage.whole.get(ent_id, []):
            print(f"  {ent_id}: {target}: whole", file=sys.stderr)
    n = sum(len(v) for t in stage.additions.values() for v in t.values()) + sum(map(len, stage.whole.values()))
    unseen = (
        f"; {len(stage.unseen)} decided case(s) the evidence lacks keep the worksheet's owner" if stage.unseen else ""
    )
    print(
        f"{n} selector(s) for {len(stage.data)} entit{'y' if len(stage.data) == 1 else 'ies'}; over the evidence "
        f"given, the owner table ({stage.owners} case(s)) is unchanged under attribution: model and check_claims "
        f"passes{unseen}. Cases in neither the evidence nor the worksheet were not seen: re-run `rr attribution "
        "--check` over every lane's evidence. Next: set config.attribution: model (and sets_lock), then "
        "`rr sets lock --write`",
        file=sys.stderr,
    )
    return 0


def cmd_migrate_verify(args: argparse.Namespace) -> int:
    """Check test evidence from after ``rr migrate apply`` against the worksheet."""
    from rules_requirements import migrate

    checked = _decided_worksheet(args)
    if checked is None:
        return 2
    doc, model = checked
    evidence = _collect_evidence(args, "migrate verify")
    if evidence is None:
        return 2
    baseline = None
    if args.baseline:
        baseline = _collect_evidence(args, "migrate verify --baseline", args.baseline)
        if baseline is None:
            return 2
    for flag, ev in (("--evidence", evidence), ("--baseline", baseline)):
        why = migrate.unkeyed(doc, ev) if ev is not None else ""
        if why:
            print(f"rr migrate verify: {flag}: {why}", file=sys.stderr)
            return 2
    res = migrate.verify(doc, evidence, baseline, allow_missing=args.allow_missing, model=model)
    for off in res.offences:
        print(off.describe())
    if res.missing:
        targets = sorted({key.target for key in res.missing}, key=natural_key)
        print(
            f"warning: {len(res.missing)} case(s) of {len(targets)} target(s) with no result file in the evidence "
            f"(--allow-missing: not run there): not verified ({', '.join(targets)})",
            file=sys.stderr,
        )
        for key in res.missing:
            print(f"  {key}", file=sys.stderr)
    if res.untagged:
        how = (
            "the model's claims select each for exactly its owner"
            if model is not None
            else "their owner is not checked (verify reads declared ids only; with --model, the model's claims are "
            "checked too): see the verified_by edits rr migrate apply lists, a target split between owners needs "
            "case selectors (rr migrate apply --stage model)"
        )
        print(
            f"note: {len(res.untagged)} decided case(s) declare no id, before and after: they count only through "
            f"verified_by; {how}",
            file=sys.stderr,
        )
        for key in res.untagged:
            print(f"  {key}", file=sys.stderr)
    if res.pending:
        print(
            f"note: {len(res.pending)} decided case(s) declare no id, before and after, and the model's claims do "
            "not select them for exactly their owner: pending a model edit (keep/remove/split, see the verified_by "
            "edits rr migrate apply lists; a split needs case selectors: rr migrate apply --stage model)",
            file=sys.stderr,
        )
        for key, claimers in res.pending:
            print(f"  {key}: counts toward [{', '.join(claimers)}]", file=sys.stderr)
    if res.also_claimed:
        print(
            f"note: {len(res.also_claimed)} decided case(s) declare exactly their owner, but claims of other entities "
            "select them too: a claim decides a case's owner over its tag (tag-mismatch), and claims of two "
            "entities quarantine it",
            file=sys.stderr,
        )
        for key, others in res.also_claimed:
            print(f"  {key}: also [{', '.join(others)}]", file=sys.stderr)
    parts = [f"{res.decided} decided case(s) declare exactly their owner"]
    if res.untagged:
        parts.append(
            f"{len(res.untagged)} declare no id ("
            + ("owned through their owner's claims" if model is not None else "count only through verified_by")
            + ")"
        )
    if res.pending:
        parts.append(f"{len(res.pending)} pending a verified_by edit")
    if baseline is not None:
        parts.append(f"{res.unchanged} undecided case(s) kept their ids")
        parts.append(f"{res.new} case(s) are new")
    if res.offences:
        print(
            f"rr migrate verify: {len(res.offences)} case(s) contradict the worksheet; " + "; ".join(parts),
            file=sys.stderr,
        )
        return 1
    print("rr migrate verify: ok: " + "; ".join(parts), file=sys.stderr)
    return 0


def _collect_check(args: argparse.Namespace, root: str, res: Any, writes: set[str], decided: Mapping[Any, str]) -> bool:
    """Run the dynamic collection check on the files apply would write;
    print why it refuses (or what it could not see) and return whether the
    write may go ahead.

    Only the decided cases the written files settle are held to their owner;
    every other item (undecided, left to ``verified_by``, in a file held
    back, refused or outside ``--only``) must keep its before-ids."""
    from rules_requirements import collect_check

    tail = "apply would write nothing" if args.dry_run else "nothing written"
    settled = res.settled(writes)
    rewrites = {f.path: (f.new_text, f.encoding) for f in res.changed if f.path in writes}
    cc = collect_check.check(
        root,
        rewrites,
        {k: decided[k] for k in settled},
        python=args.python,
        pytest_args=shlex.split(args.pytest_args),
    )
    if cc.failed_to_run:
        print(f"rr migrate: {cc.error}; {tail}.", file=sys.stderr)
        print("Pass --no-collect-check to write on the static guards alone. Collection output:", file=sys.stderr)
        print(cc.output.rstrip("\n"), file=sys.stderr)
        return False
    if cc.skipped:
        print(
            f"rr migrate: warning: {len(cc.skipped)} collector(s) were skipped at collection time in the check's "
            "environment; tests in them were not seen by the collection check:",
            file=sys.stderr,
        )
        for nodeid, reason in sorted(cc.skipped.items()):
            print(f"  {nodeid}: {reason}", file=sys.stderr)
    if not cc.ok:
        print(
            "rr migrate: the collection check refused the rewrite: it would change what pytest collects; "
            f"{tail}. Offending items (before / after / expected ids):",
            file=sys.stderr,
        )
        for off in cc.offenders:
            print(f"  {off.describe()}", file=sys.stderr)
        return False
    return True


# --------------------------------------------------------------------------- #
# rr attribution / rr sets / rr check-report                                  #
# --------------------------------------------------------------------------- #

# Attribution findings `rr attribution --check` and `rr sets check` fail on.
_LOCK_DRIFT = ("unlocked-member", "lock-owner-changed", "lock-stale", "lock-invalid")


# What `rr sets` exists to fix: a model whose only errors are about the lock still loads.
_SETS_TOLERATES = ("lock-invalid", "lock-owner-changed", "lock-stale")


def _model_and_evidence(
    args: argparse.Namespace, command: str, tolerate: tuple[str, ...] = ()
) -> tuple[Model, ingest.Evidence] | int:
    """Load the model (with ``--sets-lock`` / ``--no-lock``) and ``--evidence``; an exit status on failure."""
    model, ok = _load(
        args.model,
        quiet=True,
        tolerate=tolerate,
        sets_lock=getattr(args, "sets_lock", ""),
        no_lock=getattr(args, "no_lock", False),
        creating_lock=command == "sets lock",
    )
    if not ok:
        print("rr: requirements model is invalid (see above)", file=sys.stderr)
        return 2
    evidence = _collect_evidence(args, command)
    if evidence is None:
        return 2
    return model, evidence


def _yaml_str(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)  # a JSON string is a YAML double-quoted scalar


def _suggestions(matrix: Matrix) -> list[str]:
    """The ``verified_by`` items that would make every tag-owned or
    unclaimed-tag case a claimed one: one literal selector per case, grouped
    by entity and target."""
    from rules_requirements.case_selectors import escape

    att = matrix.attribution
    assert att is not None
    wanted: dict[str, dict[str, list[str]]] = {}
    for key, via in att.via.items():
        if via == "tag":
            wanted.setdefault(att.owner[key], {}).setdefault(key.target, []).append(key.path)
    for issue in att.issues:
        if issue.code == "unclaimed-tag" and issue.key is not None and issue.entities:
            wanted.setdefault(issue.entities[0], {}).setdefault(issue.key.target, []).append(issue.key.path)
    out = []
    for ent in sorted(wanted, key=natural_key):
        kind = matrix.model.get(ent)
        rel = "validated_by" if kind is not None and kind.kind == "user_need" else "verified_by"
        out.append(f"# {ent}: add to {rel}")
        for target in sorted(wanted[ent], key=natural_key):
            paths = sorted(set(wanted[ent][target]), key=natural_key)
            if paths == ["[target]"]:
                out.append(f'- {{target: {_yaml_str(target)}, whole: true, reason: "reports no per-case results"}}')
                continue
            cases = ", ".join(_yaml_str(escape(p)) for p in paths if p != "[target]")
            out.append(f"- {{target: {_yaml_str(target)}, cases: [{cases}]}}")
    return out


def _attribution_problems(matrix: Matrix, only_targets: Iterable[str] | None = None) -> list[str]:
    """What ``--check`` fails on: quarantines, missing cases, lock drift and
    error-level attribution issues (restricted to ``only_targets`` when given)."""
    att = matrix.attribution
    assert att is not None
    within = set(only_targets) if only_targets is not None else None

    def inside(target: str) -> bool:
        return within is None or target in within

    problems = [f"{q.code}: {q.detail}" for q in att.quarantined if inside(q.key.target)]
    for ent, members in att.members.items():
        for m in members:
            if m.state == "missing" and inside(m.target):
                what = "lock entry" if m.via == "lock" else "selector"
                problems.append(f"missing-case: {ent}: {what} {m.name!r} matched no case ({m.reason})")
            elif m.state == "moved" and inside(m.target):
                problems.append(f"moved: {ent}: {m.name} ({m.reason})")
    for issue in att.issues:
        target = issue.key.target if issue.key is not None else issue.target
        if not inside(target) and target:
            continue
        if issue.code in _LOCK_DRIFT or issue.severity == "error":
            problems.append(f"[{issue.code}] {issue.message}")
    return list(dict.fromkeys(problems))


def cmd_attribution(args: argparse.Namespace) -> int:
    loaded = _model_and_evidence(args, "attribution")
    if isinstance(loaded, int):
        return loaded
    model, evidence = loaded
    matrix = build_matrix(model, evidence, lock=_lock_arg(args))
    att = matrix.attribution
    assert att is not None
    rows = []
    for row in report.case_rows(att):
        if args.target and row["target"] not in args.target:
            continue
        if args.unowned and row["owner"] is not None:
            continue
        rows.append(row)
    if args.output == "json":
        doc = {
            "mode": att.mode,
            "cases": rows,
            "quarantined": [q.to_dict() for q in att.quarantined],
            "issues": [i.to_dict() for i in att.issues],
        }
        print(json.dumps(doc, indent=2, ensure_ascii=False))
    else:
        for row in rows:
            note = row.get("quarantine", "")
            fields = [row["case"], row["owner"] or "-", row["via"] or "-", ",".join(row["declared"]) or "-",
                      row["status"], note or "-"]  # fmt: skip
            print("\t".join(_tsv(f) for f in fields))
    if args.suggest:
        lines = _suggestions(matrix)
        if lines:
            print("\n".join(lines), file=sys.stdout if args.output != "json" else sys.stderr)
    owned = sum(r["owner"] is not None for r in rows)
    print(
        f"{len(rows)} case(s): {owned} owned, {sum('quarantine' in r for r in rows)} quarantined, "
        f"{len(rows) - owned - sum('quarantine' in r for r in rows)} unowned (attribution: {att.mode})",
        file=sys.stderr,
    )
    if not args.check:
        return 0
    problems = _attribution_problems(matrix)
    for problem in problems:
        print(f"ATTRIBUTION ERROR: {problem}", file=sys.stderr)
    if problems:
        print(f"rr attribution --check: {len(problems)} problem(s)", file=sys.stderr)
        return 1
    print("rr attribution --check: OK: every case has at most one owner, no quarantine, no lock drift", file=sys.stderr)
    return 0


def _blank_file(path: str) -> bool:
    try:
        with open(path, encoding="utf-8") as fh:
            return not fh.read().strip()
    except OSError:
        return False


def _lock_target(args: argparse.Namespace, model: Model) -> str:
    """The lock path ``rr sets`` reads (``--sets-lock``, else config.sets_lock); "" without one."""
    if args.sets_lock:
        return os.path.abspath(_path(args.sets_lock))
    return model.lock_path()


def cmd_sets(args: argparse.Namespace) -> int:
    from rules_requirements import attribution as rr_attribution
    from rules_requirements import lock as rr_lock

    loaded = _model_and_evidence(args, f"sets {args.sets_command}", tolerate=_SETS_TOLERATES)
    if isinstance(loaded, int):
        return loaded
    model, evidence = loaded
    path = _lock_target(args, model)
    if args.sets_command == "show":
        matrix = build_matrix(model, evidence, lock=None if path else NO_LOCK)
        return _sets_show(matrix, args.entity)
    if not path:
        print(
            f"rr sets {args.sets_command}: no lock: set config.sets_lock (e.g. sets_lock: verification.rrlock) "
            "or pass --sets-lock PATH",
            file=sys.stderr,
        )
        return 2
    shown = report._shown(path)
    previous: Lock | None = None
    if _blank_file(path) and args.sets_command == "lock":
        pass  # an empty file to start from (rr_sets_lock_test needs the file to exist): no lock yet
    elif os.path.exists(path):
        try:
            previous = rr_lock.load_lock(path, model.config.main_repo, shown=shown)
        except rr_lock.LockError as exc:
            for problem in exc.problems:
                print(f"error: [lock-invalid] {problem}", file=sys.stderr)
            return 2
    elif args.sets_command == "check":
        print(f"error: [lock-invalid] {shown}: no lock there; generate it with `rr sets lock --write`", file=sys.stderr)
        return 1
    att = rr_attribution.attribute(model, evidence, lock=previous)
    if args.sets_command == "check":
        ran = [t for t, run in att.targets.items() if run.ran]
        matrix = Matrix(model=model, verdicts={}, gaps=[], evidence=evidence, attribution=att)
        problems = _attribution_problems(matrix, only_targets=ran)
        for problem in problems:
            print(f"SETS CHECK: {problem}", file=sys.stderr)
        if problems:
            print(
                f"rr sets check: {len(problems)} problem(s) against {shown}; re-lock with `rr sets lock --write` "
                "and review the diff",
                file=sys.stderr,
            )
            return 1
        n = len(previous) if previous is not None else 0
        print(f"rr sets check: OK: {shown} ({n} locked case(s)) agrees with the evidence of {len(ran)} target(s)")
        return 0
    plan = rr_lock.plan_lock(model, att, previous, allow_removals=args.allow_removals)
    for entry in plan.added:
        print(f"  + {entry.case}: {entry.owner}", file=sys.stderr)
    for old, new in plan.changed:
        print(f"  ~ {new.case}: {old.owner} -> {new.owner}", file=sys.stderr)
    for entry in plan.removed:
        kept = "" if args.allow_removals else " (kept: pass --allow-removals to drop it)"
        print(f"  - {entry.case}: {entry.owner}{kept}", file=sys.stderr)
    if plan.refused:
        for reason in plan.refused:
            print(f"ATTRIBUTION ERROR: {reason}", file=sys.stderr)
        print("rr sets lock: refusing to lock quarantined cases (they have no owner); nothing written", file=sys.stderr)
        return 3
    text = rr_lock.render_lock(plan.lock)
    old_text = rr_lock.render_lock(previous) if previous is not None else ""
    if not args.write:
        import difflib

        sys.stdout.writelines(
            difflib.unified_diff(old_text.splitlines(True), text.splitlines(True), f"a/{shown}", f"b/{shown}")
        )
        print(
            f"rr sets lock: {len(plan.added)} added, {len(plan.changed)} changed, {len(plan.removed)} removed"
            + ("" if args.allow_removals or not plan.removed else " (kept)")
            + "; pass --write to write it",
            file=sys.stderr,
        )
        return 0
    out = args.out or path
    if args.out and not os.path.isabs(out):
        out = os.path.join(_root(), out)
    if plan.removed and not args.allow_removals:
        print(
            f"rr sets lock: {len(plan.removed)} entr(y/ies) would be removed (a case missing from a target that ran, "
            "or no claim selects it): kept; pass --allow-removals after checking the run was complete",
            file=sys.stderr,
        )
    if text != old_text or not os.path.exists(out):
        rr_lock.write_lock(out, plan.lock)
        print(f"rr sets lock: wrote {report._shown(out)} ({len(plan.lock)} locked case(s))", file=sys.stderr)
    else:
        print(f"rr sets lock: {report._shown(out)} is up to date", file=sys.stderr)
    return 0


def _sets_show(matrix: Matrix, entity: str) -> int:
    att = matrix.attribution
    assert att is not None
    if entity not in att.members:
        print(f"rr sets show: {entity} is not a user need, requirement or mitigation of the model", file=sys.stderr)
        return 2
    members = att.members_of(entity)
    verdict = matrix.verdicts[entity]
    print(f"{entity}: {verdict.status} · {report.set_line(members) or 'no members'}")
    for m in members:
        fields = [m.state, m.name, m.via, m.selector, m.level or "-", m.reason or "-"]
        print("\t".join(_tsv(f) for f in fields))
    return 0


def cmd_check_report(args: argparse.Namespace) -> int:
    from rules_requirements import checkreport

    try:
        doc = checkreport.load_report(_path(args.report))
    except checkreport.AmbiguousReportError as exc:
        print(f"CHECK-REPORT: {exc}", file=sys.stderr)
        return 1
    except checkreport.ReportError as exc:
        print(f"rr check-report: {exc}", file=sys.stderr)
        return 2
    problems = checkreport.check_report(doc)
    for problem in problems:
        print(f"CHECK-REPORT: {problem}", file=sys.stderr)
    if problems:
        print(f"rr check-report: {args.report}: {len(problems)} problem(s)", file=sys.stderr)
        return 1
    print(f"rr check-report: OK: {checkreport.summarize(doc)}; no case is owned twice")
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    from rules_requirements.server.workspace import Workspace, WorkspaceError

    ws = Workspace(
        root=_path(args.root) if args.root else _root(), model_paths=[_path(p) for p in args.model], scan=False
    )
    try:
        changes = ws.diff(args.old, args.new)
    except WorkspaceError as exc:
        print(f"rr diff: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps([c.to_dict() for c in changes], indent=2))
    else:
        from rules_requirements.diff import render_text, summarize

        sys.stdout.write(render_text(changes))
        s = summarize(changes)
        print(f"{s['added']} added, {s['removed']} removed, {s['modified']} modified", file=sys.stderr)
    return 1 if (changes and args.exit_code) else 0


def cmd_serve(args: argparse.Namespace) -> int:
    import time

    from rules_requirements.agents.llm import default_llm, llm_status
    from rules_requirements.server.app import Api, serve
    from rules_requirements.server.workspace import Workspace

    # Relative model and evidence paths are relative to the repository root
    # (the workspace under `bazel run`): the editor must write the real source
    # files, never a runfiles copy.
    root = _path(args.root) if args.root else _root()
    ws = Workspace(
        root=root,
        model_paths=[p if os.path.isabs(p) else os.path.join(root, p) for p in args.model],
        evidence_paths=[p if os.path.isabs(p) else os.path.join(root, p) for p in args.evidence],
        current_build=_kv(args.current_build),
        scan=not args.no_scan,
        author=args.author,
        lanes=_serve_lanes(args.lane_targets, root),
    )
    snap = ws.snapshot()
    llm = default_llm(enabled=not args.no_llm, model=args.agent_model, effort=args.agent_effort)
    api = Api(ws, llm=llm, llm_enabled=not args.no_llm, author=args.author)
    status = llm_status(llm, not args.no_llm)
    print(
        f"rr serve: {len(ws.files())} model file(s), {len(snap.model.ids())} entities, "
        f"{len(snap.matrix.evidence.cases)} test case(s); agents: "
        + (status["name"] if status["available"] else f"deterministic only ({status['reason']})"),
        file=sys.stderr,
    )
    token = args.token
    from rules_requirements.server.app import needs_token

    if needs_token(args.host) and not token:
        import secrets

        token = secrets.token_urlsafe(24)
        print(
            f"rr serve: binding to {args.host} exposes the editor beyond this machine; generated a token",
            file=sys.stderr,
        )
    httpd = serve(
        api,
        host=args.host,
        port=args.port,
        token=token,
        allowed_hosts=set(args.allow_host or ()),
        ready=lambda url: print(f"rr serve: open {url}" + (f"#token={token}" if token else ""), file=sys.stderr),
    )
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        httpd.shutdown()
    return 0


def _serve_lanes(specs: list[str] | None, root: str) -> dict[str, list[str]]:
    """``--lane-targets NAME=FILE``: each lane's targets, one label per line
    (``#`` comments and blank lines skipped), as ``rr report --lane-targets``
    reads them."""
    lanes: dict[str, list[str]] = {}
    for name, path in _kv(specs).items():
        full = path if os.path.isabs(path) else os.path.join(root, path)
        try:
            with open(full, encoding="utf-8") as fh:
                lines = [line.strip() for line in fh]
        except OSError as exc:
            raise SystemExit(f"rr serve: --lane-targets {name}: cannot read {path}: {exc}") from None
        lanes[name] = [line for line in lines if line and not line.startswith("#")]
    return lanes


def cmd_wrap(args: argparse.Namespace) -> int:
    from rules_requirements.hooks import wrap

    return wrap.main(args.rest, prog="rr wrap")


def cmd_case(args: argparse.Namespace) -> int:
    from rules_requirements.hooks.ids import E_MULTIPLE
    from rules_requirements.hooks.junit_writer import JUnitWriter, source_file

    out = _path(args.out) if args.out else os.environ.get("XML_OUTPUT_FILE", "")
    if not out:
        print("rr case: no --out and no $XML_OUTPUT_FILE", file=sys.stderr)
        return 2
    if args.requirement and len(args.requirement) > 1:
        print(
            f"rr case: --requirement given {len(args.requirement)} times ({', '.join(args.requirement)}); "
            f"a test case verifies at most one requirement [{E_MULTIPLE}]",
            file=sys.stderr,
        )
        return 2
    try:
        artifact = _kv(args.artifact)
    except SystemExit as exc:  # a malformed --artifact: the case cannot be recorded
        print(f"rr case: {exc}", file=sys.stderr)
        return 2
    target = os.environ.get("TEST_TARGET", "")
    suite = args.suite or (target.rsplit(":", 1)[-1] if target else "") or "rr"
    writer = JUnitWriter(suite, file=source_file(args.file))
    try:
        writer.add(
            args.name,
            args.requirement[0] if args.requirement else None,
            status=args.status,
            message=args.message,
            duration=args.duration,
            level=args.level,
            artifact=artifact,
            classname=args.classname,
        )
        writer.write(out, append=True)
    except (ValueError, TypeError, SyntaxError, OSError) as exc:
        print(f"rr case: {exc}", file=sys.stderr)
        return 2
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="rr", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    def model_arg(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--model", "--requirements", nargs="+", default=[DEFAULT_MODEL], help="model files/directories")

    def lock_args(sp: argparse.ArgumentParser) -> None:
        group = sp.add_mutually_exclusive_group()
        group.add_argument(
            "--sets-lock", default="", metavar="PATH", help="read this verification-set lock, not config.sets_lock"
        )
        group.add_argument(
            "--no-lock", action="store_true", help="read no verification-set lock (the sets are not pinned)"
        )

    def evidence_args(sp: argparse.ArgumentParser, ingestors: bool = True) -> None:
        sp.add_argument("--evidence", nargs="*", default=["bazel-testlogs"], help="evidence files/dirs/globs")
        if ingestors:
            sp.add_argument("--format", action="append", help="only use these ingestors (repeatable)")
        else:
            sp.set_defaults(format=None)
        sp.add_argument("--ingestor", action="append", help="load an extra ingestor, module:attr")

    def scan_args(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--root", default="", help="source tree to scan (default: workspace)")
        sp.add_argument("--include", action="append", help="glob of files to scan (repeatable)")
        sp.add_argument("--exclude", action="append", help="glob of files to skip (repeatable)")
        sp.add_argument("--files", nargs="+", help="scan exactly these files (relative to --root)")

    v = sub.add_parser("validate", help="validate the model")
    v.add_argument("model", nargs="*", default=[DEFAULT_MODEL])
    v.add_argument("--strict", action="store_true", help="treat warnings as errors")
    v.add_argument("--format", choices=["text", "json"], default="text")
    v.add_argument(
        "--known-targets",
        default="",
        metavar="FILE",
        help="labels of every test target, one per line (`bazel query 'tests(//...)'`); "
        "a claim, config.variants entry or lock target naming any other label is an unknown-target error",
    )
    v.add_argument("--sets-lock", default="", metavar="PATH", help="check this lock instead of config.sets_lock")
    v.add_argument(
        "--junit",
        default="",
        metavar="PATH",
        help="also write one JUnit case per check family (rr.validate::shape, ::references, ::coverage-rules, "
        "::claims, ::lock); default $XML_OUTPUT_FILE when set (under `bazel test`)",
    )
    v.set_defaults(func=cmd_validate)

    s = sub.add_parser("scan", aliases=["check-annotations"], help="check source annotations")
    model_arg(s)
    scan_args(s)
    s.add_argument("--list", action="store_true", help="print every annotation found")
    s.add_argument("--json", default="", help="write annotations as JSON")
    s.add_argument("--ignore-model-errors", action="store_true")
    s.add_argument("--strict", action="store_true", help="treat annotation warnings (multi-verifies-*) as errors")
    s.set_defaults(func=cmd_scan)

    for name in ("report", "aggregate"):
        r = sub.add_parser(name, help="build the traceability report" + (" (alias)" if name != "report" else ""))
        model_arg(r)
        r.add_argument("--evidence", "--junit", nargs="*", default=["bazel-testlogs"], help="evidence files/dirs/globs")
        r.add_argument("--format", action="append", help="only use these ingestors (repeatable)")
        r.add_argument("--ingestor", action="append", help="load an extra ingestor, module:attr")
        r.add_argument("--html", default="")
        r.add_argument("--json", default="")
        r.add_argument("--md", default="")
        r.add_argument("--out", action="append", help="output file; format from extension (repeatable)")
        r.add_argument("--queue-out", default="", help="write the gaps as a JSON work queue")
        r.add_argument("--title", default="")
        r.add_argument("--strict", action="store_true", help="treat model and attribution warnings as errors")
        r.add_argument("--fail-on", choices=["none", "failed", "unverified", "gaps"], default="none")
        r.add_argument("--pyramid-policy", choices=["off", "warn", "error"], default="warn")
        r.add_argument(
            "--current-build",
            action="append",
            metavar="KEY=VALUE",
            help="current artifact identity; mismatching evidence is stale",
        )
        r.add_argument("--scan", action="store_true", help="also scan sources for implementation annotations")
        lock_args(r)
        r.add_argument("--lane", default="", metavar="NAME", help="stamp the report with this lane (e.g. software)")
        r.add_argument(
            "--lane-targets",
            default="",
            metavar="FILE",
            help="the targets this lane runs, one label per line: not-run members of other targets read "
            "'out of lane' and their gaps stay out of --queue-out (verdicts never change)",
        )
        r.add_argument(
            "--on-attribution-error",
            choices=["fail", "warn"],
            default="fail",
            help="a quarantined test case (several ids, several claimants, one test code with several owners) "
            "makes rr report exit 3 after writing the reports (fail, the default); warn keeps the exit status "
            "(the cases still count for nobody, and every entity they name is INVALID)",
        )
        scan_args(r)
        r.set_defaults(func=cmd_report)

    g = sub.add_parser("graph", help="export the trace graph")
    model_arg(g)
    g.add_argument("--format", choices=["dot", "mermaid", "json", "svg"], default="mermaid")
    g.add_argument("--evidence", nargs="*", default=[], help="color nodes by status")
    g.add_argument("--methods", action="store_true", help="include test methods")
    g.add_argument(
        "--cases",
        action="store_true",
        help="add a node per owned test case, with one edge from its one owner (needs --evidence)",
    )
    g.add_argument("--out", default="-")
    g.set_defaults(func=cmd_graph)

    i = sub.add_parser("ingest", help="print the test cases parsed from evidence")
    i.add_argument("paths", nargs="+")
    i.add_argument("--format", action="append")
    i.add_argument("--ingestor", action="append")
    i.set_defaults(func=cmd_ingest)

    c = sub.add_parser("cases", help="list every test case key in the evidence (no model needed)")
    c.add_argument("--evidence", nargs="*", default=["bazel-testlogs"], help="evidence files/dirs/globs")
    c.add_argument("--target", action="append", help="only this target (repeatable)")
    c.add_argument("--json", action="store_true", help="print JSON instead of tab-separated lines")
    c.add_argument("--format", action="append", help="only use these ingestors (repeatable)")
    c.add_argument("--ingestor", action="append", help="load an extra ingestor, module:attr")
    c.set_defaults(func=cmd_cases)

    mg = sub.add_parser("migrate", help="move to one requirement per test case: worksheet and codemod")
    msub = mg.add_subparsers(dest="migrate_command", required=True)
    mp = msub.add_parser("plan", help="write the attribution worksheet: evidence counting toward 2+ entities")
    model_arg(mp)
    mp.add_argument("--evidence", nargs="*", default=["bazel-testlogs"], help="evidence files/dirs/globs")
    mp.add_argument("--format", action="append", help="only use these ingestors (repeatable)")
    mp.add_argument("--ingestor", action="append", help="load an extra ingestor, module:attr")
    mp.add_argument("--out", default="", help="worksheet to write (.rrplan, YAML); stdout if no output is given")
    mp.add_argument("--json", default="", help="also write the worksheet as JSON")
    mp.add_argument("--md", default="", help="also write the worksheet as Markdown")
    mp.add_argument("--merge", default="", help="carry the decisions of an earlier worksheet over")
    mp.set_defaults(func=cmd_migrate_plan)
    ma = msub.add_parser("apply", help="rewrite test tags to the owners decided on a worksheet")
    ma.add_argument("worksheet", help="the decided .rrplan (or .json) worksheet")
    ma.add_argument(
        "--stage",
        choices=["tags", "model"],
        required=True,
        help="tags: split multi-id pytest/unittest tags; model: write an explicit selector for every current "
        "owner into the model (the owner table must stay unchanged under attribution: model)",
    )
    ma.add_argument(
        "--compress",
        action="store_true",
        help="--stage model: a '*' glob where it selects exactly the entity's cases (none skipped, no overlap)",
    )
    ma.add_argument(
        "--evidence",
        nargs="*",
        default=[],
        help="--stage model: the evidence the owners come from (default: the worksheet's inputs)",
    )
    ma.add_argument("--ingestor", action="append", help="load an extra ingestor, module:attr")
    ma.set_defaults(format=None)
    ma.add_argument("--root", default="", help="source tree to rewrite (default: workspace)")
    ma.add_argument("--only", action="append", help="only rewrite files below this path (repeatable)")
    ma.add_argument(
        "--unassigned",
        choices=["refuse", "drop"],
        default="refuse",
        help="a multi-id test without a decision: leave its file unchanged (refuse) or drop its tags",
    )
    ma.add_argument("--line-length", type=int, default=88, help="black's line length, for lines that need wrapping")
    ma.add_argument(
        "--partial",
        action="store_true",
        help="when files are refused or decided cases not found, still write each rewritten file that holds no "
        "such case and is not linked to a refused file or one holding such a case (default: write nothing). Two "
        "files are linked when one star-imports the other or passes it around as a module, or imports, "
        "references or subclasses a class, a test-named name or a name not defined at its top level; importing "
        "a plain helper function or constant does not link them. A file that cannot be read, a class whose base "
        "cannot be resolved, and an import call whose module cannot be named are linked to the files refused "
        "with them",
    )
    ma.add_argument(
        "--dry-run",
        action="store_true",
        help="print a diff instead of writing; the collection check still runs (exit 1 when apply would refuse)",
    )
    ma.add_argument("--model", "--requirements", nargs="+", default=[], help="check the decided owners exist")
    ma.add_argument(
        "--no-collect-check",
        action="store_true",
        help="skip the dynamic collection check (run pytest --collect-only before and after, and refuse unless "
        "every test keeps exactly the ids it should; it runs whenever anything would be written, with --partial "
        "and --dry-run too): write on the static guards alone, which are best-effort and cannot see tests pytest "
        "collects dynamically",
    )
    ma.add_argument(
        "--trust-main-guard",
        action="store_true",
        help="leave out of the static guards the code only an 'if __name__ == \"__main__\":' block runs (its body "
        "and the module-level functions only it reaches), which pytest does not run when it imports the module "
        "(default: judge that code like any other). The exclusion is best-effort: static analysis cannot prove "
        "what Python runs at import, so only use it together with a definitive check -- the collection check, or "
        "rr migrate verify against fresh test evidence before merging",
    )
    ma.add_argument(
        "--python",
        default="",
        help="the interpreter whose pytest and project dependencies collect the tests for the collection check "
        "(default: the interpreter running rr)",
    )
    ma.add_argument(
        "--pytest-args",
        default="",
        help="extra arguments for the collection check's pytest, one shell-quoted string (e.g. "
        '"-c pytest.ini --rootdir . -p myplugin", "--ignore=scripts"). Both collections run from --root with no '
        "path argument, so the project's ini (testpaths included) picks what is collected; name paths here to "
        "collect others. With --strict-markers, pass '-p rules_requirements.hooks.pytest_plugin' if the project "
        "loads rr's plugin that way",
    )
    ma.set_defaults(func=cmd_migrate_apply)
    mv = msub.add_parser(
        "verify", help="check test evidence from after apply against the worksheet (the check for Bazel projects)"
    )
    mv.add_argument("--worksheet", required=True, help="the decided .rrplan (or .json) worksheet")
    mv.add_argument(
        "--evidence", nargs="+", required=True, help="evidence from after the rewrite (files/dirs/globs, e.g. CI's)"
    )
    mv.add_argument(
        "--baseline",
        nargs="+",
        default=[],
        help="evidence from before the rewrite: every undecided case must keep its ids, and no case may disappear",
    )
    mv.add_argument(
        "--allow-missing",
        action="store_true",
        help="a case with no result is a warning, not an error, when its target has no result file at all in the "
        "evidence (a HITL or manual target CI does not run); a case missing from a target that ran stays an error. "
        "A target with any result file ran: a test.xml with no testcase (pytest collected nothing) or only Bazel's "
        "synthetic whole-run result included",
    )
    mv.add_argument(
        "--model",
        "--requirements",
        nargs="+",
        default=[],
        help="check the decided owners exist, and the verified_by owner of decided cases that declare no id",
    )
    mv.add_argument("--format", action="append", help="only use these ingestors (repeatable)")
    mv.add_argument("--ingestor", action="append", help="load an extra ingestor, module:attr")
    mv.set_defaults(func=cmd_migrate_verify)

    at = sub.add_parser("attribution", help="print who owns each test case (and why), or check that nothing is wrong")
    model_arg(at)
    evidence_args(at, ingestors=False)
    lock_args(at)
    at.add_argument("--target", action="append", help="only this target (repeatable)")
    at.add_argument("--unowned", action="store_true", help="only cases no entity owns (quarantined ones included)")
    at.add_argument("--format", dest="output", choices=["tsv", "json"], default="tsv")
    at.add_argument(
        "--check",
        action="store_true",
        help="exit 1 on any quarantine, missing case, lock drift or error-level attribution issue",
    )
    at.add_argument(
        "--suggest", action="store_true", help="print the selector to add for each tag-owned or unclaimed-tag case"
    )
    at.set_defaults(func=cmd_attribution)

    st = sub.add_parser("sets", help="the verification-set lock: write it, check it, show one set")
    ssub = st.add_subparsers(dest="sets_command", required=True)
    for name, text in (
        ("lock", "compute the lock from the evidence; print the diff, or --write it"),
        ("check", "exit 1 when the lock disagrees with the evidence (missing cases, unlocked members, owner changes)"),
        ("show", "print one entity's verification set"),
    ):
        sp = ssub.add_parser(name, help=text)
        if name == "show":
            sp.add_argument("entity", help="a user need, requirement or mitigation id")
        model_arg(sp)
        evidence_args(sp)
        sp.add_argument("--sets-lock", default="", metavar="PATH", help="the lock to read (default: config.sets_lock)")
        if name == "lock":
            sp.add_argument("--write", action="store_true", help="write the lock (default: print the diff)")
            sp.add_argument(
                "--allow-removals",
                action="store_true",
                help="drop entries the evidence no longer has (default: keep them, so a crashed or filtered run "
                "never shrinks a set silently)",
            )
            sp.add_argument(
                "--out", default="", metavar="PATH", help="write here instead (relative: to the workspace root)"
            )
        sp.set_defaults(func=cmd_sets)

    cr = sub.add_parser(
        "check-report",
        help="re-prove from a JSON report alone that no test case is owned by two entities",
    )
    cr.add_argument("report", help="a JSON report (rules_requirements/report/v2)")
    cr.set_defaults(func=cmd_check_report)

    d = sub.add_parser("diff", help="semantic diff of the model between two git refs")
    model_arg(d)
    d.add_argument("old", help="git ref (e.g. main, v1.0, HEAD~3)")
    d.add_argument("new", nargs="?", default="WORKTREE", help="git ref, or WORKTREE (default)")
    d.add_argument("--root", default="", help="repository root (default: workspace)")
    d.add_argument("--json", action="store_true")
    d.add_argument("--exit-code", action="store_true", help="exit 1 when the model changed")
    d.set_defaults(func=cmd_diff)

    sv = sub.add_parser("serve", help="interactive web editor with tracing, versioning and agents")
    model_arg(sv)
    sv.add_argument("--evidence", nargs="*", default=[], help="test evidence (e.g. bazel-testlogs)")
    sv.add_argument("--root", default="", help="repository root (default: workspace)")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8080)
    sv.add_argument("--token", default="", help="require this bearer token on API requests")
    sv.add_argument("--allow-host", action="append", help="extra Host header value to accept (e.g. behind a proxy)")
    sv.add_argument("--author", default="", help='default author for edits and commits, "Name <email>"')
    sv.add_argument("--current-build", action="append", metavar="KEY=VALUE")
    sv.add_argument(
        "--lane-targets",
        action="append",
        metavar="NAME=FILE",
        help="the targets lane NAME runs, one label per line: the case ledger can filter by lane",
    )
    sv.add_argument("--no-scan", action="store_true", help="skip the source annotation scan")
    sv.add_argument("--no-llm", action="store_true", help="disable LLM-backed agent workflows")
    sv.add_argument("--agent-model", default="", help="Claude model for agents (default claude-opus-5-5)")
    sv.add_argument("--agent-effort", default="", help="effort for agent requests (default high)")
    sv.set_defaults(func=cmd_serve)

    c = sub.add_parser(
        "case",
        help="append one test case to a JUnit file (shell and ad-hoc harnesses)",
        description="Append one test case to the JUnit file at --out (default $XML_OUTPUT_FILE), "
        "creating it if needed. Exits 0 whatever the case's status.",
    )
    c.add_argument("--out", default="", help="JUnit file to append to (default: $XML_OUTPUT_FILE)")
    c.add_argument("--name", required=True, help="the case's name")
    c.add_argument("--status", choices=["passed", "failed", "error", "skipped"], default="passed")
    c.add_argument("--classname", default="", help="the case's classname (default: the suite)")
    c.add_argument("--suite", default="", help="suite to append to (default: the name part of $TEST_TARGET, else rr)")
    c.add_argument("--requirement", action="append", metavar="ID", help="the ONE requirement id the case verifies")
    c.add_argument("--level", default="", help="the verification level the case provides")
    c.add_argument("--artifact", action="append", metavar="KEY=VALUE", help="artifact identity (repeatable)")
    c.add_argument("--message", default="", help="failure, error or skip message")
    c.add_argument("--duration", type=float, default=0.0, help="seconds")
    c.add_argument("--file", default="", help="source file of the test code, recorded as rr.file")
    c.set_defaults(func=cmd_case)

    w = sub.add_parser("wrap", help="run a test binary and emit traceability JUnit", add_help=False)
    w.add_argument("rest", nargs=argparse.REMAINDER)
    w.set_defaults(func=cmd_wrap)
    return p


# Options whose value is a command line of its own: a value starting with
# "-" (--pytest-args "--ignore=x") would read to argparse as an option.
_ARGLINE_OPTIONS = ("--pytest-args",)


def _join_argline_values(argv: list[str]) -> list[str]:
    """``["--pytest-args", "-x"]`` -> ``["--pytest-args=-x"]``, so argparse
    takes a dash-leading value as the option's value.

    Only ``rr migrate apply`` has an argline option, so every other
    subcommand's argv is returned untouched: a value elsewhere that happens to
    equal one of these option names is never rewritten."""
    if argv[:2] != ["migrate", "apply"]:
        return list(argv)
    out: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--":
            out.extend(argv[i:])
            break
        if a in _ARGLINE_OPTIONS and i + 1 < len(argv):
            out.append(f"{a}={argv[i + 1]}")
            i += 2
            continue
        out.append(a)
        i += 1
    return out


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["wrap"]:
        # Everything after `wrap` is wrap's own: argparse.REMAINDER would
        # refuse a leading option (`rr wrap --junit-xml ...`, `--help`), and
        # the wrapped command's argv is passed through untouched.
        return cmd_wrap(argparse.Namespace(rest=argv[1:]))
    args = build_parser().parse_args(_join_argline_values(argv))
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
