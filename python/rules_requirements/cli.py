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
    rr migrate   plan --model requirements/ --evidence bazel-testlogs --out attribution.rrplan

Under ``bazel run``, relative paths resolve against the workspace root.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from typing import Any, Iterable, Mapping

from rules_requirements import __version__, graph, ingest, report
from rules_requirements import annotations as rr_annotations
from rules_requirements.model import Model, read_model
from rules_requirements.trace import FAILED, UNVERIFIED, build_matrix
from rules_requirements.util import natural_key
from rules_requirements.validate import validate

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


def _load(paths: list[str], strict: bool = False, quiet: bool = False) -> tuple[Model, bool]:
    """Read + validate; print issues. Returns (model, ok)."""
    resolved = [_path(p) for p in paths]
    for p in resolved:
        if not os.path.exists(p):
            print(f"rr: model path not found: {p}", file=sys.stderr)
            return Model(), False
    model, warnings = read_model(resolved, root=_root())
    issues = validate(model, strict=strict)
    for w in warnings:
        print(f"warning: [unknown-field] {w}", file=sys.stderr)
    errors = 0
    for issue in issues:
        errors += issue.severity == "error"
        if not quiet or issue.severity == "error":
            print(str(issue), file=sys.stderr)
    return model, errors == 0


def cmd_validate(args: argparse.Namespace) -> int:
    model, ok = _load(args.model, strict=args.strict)
    if args.format == "json":
        issues = validate(model, strict=args.strict)
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
    if args.list:
        for ref in refs:
            sym = f" [{ref.symbol}]" if ref.symbol else ""
            txt = f" — {ref.text}" if ref.text else ""
            print(f"{ref.path}:{ref.line}: {ref.relation} {', '.join(ref.ids)}{sym}{txt}")
    if args.json:
        _write(args.json, json.dumps([r.to_dict() for r in refs], indent=2) + "\n")
    print(f"scanned: {len(refs)} annotation(s), {sum(len(r.ids) for r in refs)} reference(s)")
    if unknown:
        print(f"{len(unknown)} reference(s) to undefined ids:", file=sys.stderr)
        for ref, rid in unknown:
            print(f"  {ref.path}:{ref.line}: {rid}", file=sys.stderr)
        return 1
    print("all references resolve.")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    for spec in args.ingestor or []:
        ingest.load_ingestor(spec)
    model, ok = _load(args.model, strict=args.strict, quiet=True)
    if not ok:
        print("rr: requirements model is invalid (see above)", file=sys.stderr)
        return 2
    evidence = ingest.collect([_path(p) for p in args.evidence], only=args.format or None)
    refs = _scan(args, model) if args.scan else None
    matrix = build_matrix(model, evidence, current_build=_kv(args.current_build), references=refs)

    outputs = {"html": args.html, "json": args.json, "md": args.md}
    for extra in args.out or []:
        ext = extra.rsplit(".", 1)[-1].lower()
        fmt = {"htm": "html", "markdown": "md"}.get(ext, ext)
        if fmt not in report.FORMATS:
            print(f"rr: cannot infer report format from {extra!r}", file=sys.stderr)
            return 2
        outputs[fmt] = extra
    for fmt, path in outputs.items():
        if path:
            _write(path, report.FORMATS[fmt](matrix, args.title))
    if args.queue_out:
        _write(args.queue_out, json.dumps({"queue": [g.to_dict() for g in matrix.gaps]}, indent=2) + "\n")

    c = matrix.counts()
    print(
        f"evidence: {len(evidence.files)} file(s), {c['test_cases']} test case(s) | "
        f"validation: {c['user_needs_validated']}/{c['user_needs']} needs | "
        f"verification: {c['requirements_verified']}/{c['requirements']} requirements "
        f"({c['requirements_failed']} failed, {c['requirements_unverified']} unverified, "
        f"{c['requirements_under_verified']} under-verified) | "
        f"risks: {c['risks_mitigated']}/{c['risks']} mitigated | gaps: {c['gaps']}",
        file=sys.stderr,
    )
    reqs = matrix.of_kind("requirement")
    # Any FAILED verdict gates: a failing validation test on a need, or a
    # failing effectiveness test on a mitigation, counts as much as a
    # requirement's.
    failed = sorted((v.id for v in matrix.verdicts.values() if v.status == FAILED), key=natural_key)
    unverified = [v.id for v in reqs if v.status == UNVERIFIED]
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
    if violations and args.pyramid_policy != "off":
        print(f"cost-pyramid {args.pyramid_policy}: " + ", ".join(violations), file=sys.stderr)
    if matrix.unknown_evidence:
        print("evidence references undefined ids: " + ", ".join(matrix.unknown_evidence), file=sys.stderr)

    rc = 0
    if args.fail_on in ("failed", "unverified", "gaps") and failed:
        rc = 1
    if args.fail_on in ("unverified", "gaps") and unverified:
        print("UNVERIFIED: " + ", ".join(unverified), file=sys.stderr)
        rc = 1
    if args.fail_on == "gaps" and matrix.gaps:
        rc = 1
    if args.pyramid_policy == "error" and violations:
        rc = 1
    return rc


def cmd_graph(args: argparse.Namespace) -> int:
    model, ok = _load(args.model, quiet=True)
    if not ok:
        return 2
    statuses: dict[str, str] = {}
    if args.evidence:
        matrix = build_matrix(model, ingest.collect([_path(p) for p in args.evidence]))
        statuses = {k: v.status for k, v in matrix.verdicts.items()}
    nodes, edges = graph.build(model, statuses, include_methods=args.methods)
    render = {"dot": graph.to_dot, "mermaid": graph.to_mermaid, "json": graph.to_json, "svg": graph.to_svg}
    _write(args.out, render[args.format](nodes, edges))
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    for spec in args.ingestor or []:
        ingest.load_ingestor(spec)
    ev = ingest.collect([_path(p) for p in args.paths], only=args.format or None)
    rows: list[dict[str, Any]] = [
        {
            "name": c.name,
            "classname": c.classname,
            "status": c.status,
            "requirements": list(c.requirements),
            "level": c.level,
            "artifact": c.artifact,
            "target": c.target,
            "source": c.source,
        }
        for c in ev.cases
    ]
    print(json.dumps({"files": ev.files, "cases": rows}, indent=2))
    return 0


def _collect_evidence(args: argparse.Namespace, command: str) -> ingest.Evidence | None:
    """The evidence named by ``--evidence``; None (after saying why) when it holds no evidence at all.

    A path that contributes no evidence file is a warning: a mistyped path
    must not read as "this target has no results".
    """
    for spec in getattr(args, "ingestor", None) or []:
        ingest.load_ingestor(spec)
    paths = [_path(p) for p in args.evidence]
    ev = ingest.collect(paths, only=args.format or None)
    used = set(ev.files)
    for given, path in zip(args.evidence, paths):
        if not any(f in used for f in ingest.expand([path])):
            print(f"warning: [no-evidence] {given}: no evidence file found there", file=sys.stderr)
    if not ev.files:
        print(f"rr {command}: no evidence found in {' '.join(args.evidence) or '(nothing given)'}", file=sys.stderr)
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

    model, ok = _load(args.model, quiet=True)
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


def cmd_migrate_apply(args: argparse.Namespace) -> int:
    import difflib

    from rules_requirements import migrate, tag_codemod

    try:
        doc = migrate.load_worksheet(_path(args.worksheet))
    except migrate.WorksheetError as exc:
        print(f"rr migrate: {exc}", file=sys.stderr)
        return 2
    model = None
    model_paths = list(args.model)
    if not model_paths:
        # The model the worksheet was planned with, when it is still there.
        planned = (doc.get("inputs") or {}).get("model") if isinstance(doc.get("inputs"), dict) else None
        if isinstance(planned, list) and planned and all(os.path.exists(_path(str(p))) for p in planned):
            model_paths = [str(p) for p in planned]
            print(f"rr migrate: checking owners against the planned model: {' '.join(model_paths)}", file=sys.stderr)
    if model_paths:
        model, ok = _load(model_paths, quiet=True)
        if not ok:
            print("rr: requirements model is invalid (see above)", file=sys.stderr)
            return 2
    problems = migrate.check_worksheet(doc, model)
    for problem in problems:
        print(f"rr migrate: {args.worksheet}: {problem}", file=sys.stderr)
    if problems:
        return 2
    root = _path(args.root) if args.root else _root()
    decided = migrate.decisions(doc)
    res = tag_codemod.apply_tags(
        decided,
        root,
        only=args.only or (),
        unassigned=args.unassigned,
        line_length=args.line_length,
        files_of=migrate.case_files(doc),
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

    def scan_args(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--root", default="", help="source tree to scan (default: workspace)")
        sp.add_argument("--include", action="append", help="glob of files to scan (repeatable)")
        sp.add_argument("--exclude", action="append", help="glob of files to skip (repeatable)")
        sp.add_argument("--files", nargs="+", help="scan exactly these files (relative to --root)")

    v = sub.add_parser("validate", help="validate the model")
    v.add_argument("model", nargs="*", default=[DEFAULT_MODEL])
    v.add_argument("--strict", action="store_true", help="treat warnings as errors")
    v.add_argument("--format", choices=["text", "json"], default="text")
    v.set_defaults(func=cmd_validate)

    s = sub.add_parser("scan", aliases=["check-annotations"], help="check source annotations")
    model_arg(s)
    scan_args(s)
    s.add_argument("--list", action="store_true", help="print every annotation found")
    s.add_argument("--json", default="", help="write annotations as JSON")
    s.add_argument("--ignore-model-errors", action="store_true")
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
        r.add_argument("--strict", action="store_true")
        r.add_argument("--fail-on", choices=["none", "failed", "unverified", "gaps"], default="none")
        r.add_argument("--pyramid-policy", choices=["off", "warn", "error"], default="warn")
        r.add_argument(
            "--current-build",
            action="append",
            metavar="KEY=VALUE",
            help="current artifact identity; mismatching evidence is stale",
        )
        r.add_argument("--scan", action="store_true", help="also scan sources for implementation annotations")
        scan_args(r)
        r.set_defaults(func=cmd_report)

    g = sub.add_parser("graph", help="export the trace graph")
    model_arg(g)
    g.add_argument("--format", choices=["dot", "mermaid", "json", "svg"], default="mermaid")
    g.add_argument("--evidence", nargs="*", default=[], help="color nodes by status")
    g.add_argument("--methods", action="store_true", help="include test methods")
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
    ma.add_argument("--stage", choices=["tags"], required=True, help="tags: split multi-id pytest/unittest tags")
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
