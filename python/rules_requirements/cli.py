# SPDX-License-Identifier: AGPL-3.0-or-later
"""``rr`` — the rules_requirements command line.

::

    rr validate  requirements/                    # shape, references, coverage rules
    rr scan      --model requirements/ --root .   # source annotations -> unknown ids fail
    rr report    --model requirements/ --evidence bazel-testlogs \\
                 --html report.html --json report.json --md report.md
    rr graph     --model requirements/ --format mermaid
    rr ingest    bazel-testlogs                   # debug: show parsed test cases

Under ``bazel run``, relative paths resolve against the workspace root.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from rules_requirements import annotations as rr_annotations
from rules_requirements import graph, ingest, report
from rules_requirements.model import Model, read_model
from rules_requirements.trace import FAILED, UNVERIFIED, build_matrix
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
    failed = [v.id for v in reqs if v.status == FAILED]
    unverified = [v.id for v in reqs if v.status == UNVERIFIED]
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


def cmd_wrap(args: argparse.Namespace) -> int:
    from rules_requirements.hooks import wrap

    return wrap.main(args.rest)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="rr", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    def model_arg(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--model", "--requirements", nargs="+", default=[DEFAULT_MODEL], help="model files/directories")

    def scan_args(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--root", default="", help="source tree to scan (default: workspace)")
        sp.add_argument("--include", action="append", help="glob of files to scan (repeatable)")
        sp.add_argument("--exclude", action="append", help="glob of files to skip (repeatable)")

    v = sub.add_parser("validate", help="validate the model")
    v.add_argument("model", nargs="*", default=[DEFAULT_MODEL])
    v.add_argument("--strict", action="store_true", help="treat warnings as errors")
    v.add_argument("--format", choices=["text", "json"], default="text")
    v.set_defaults(func=cmd_validate)

    s = sub.add_parser("scan", aliases=["check-annotations"], help="check source annotations")
    model_arg(s)
    scan_args(s)
    s.add_argument("--list", action="store_true", help="print every annotation found")
    s.add_argument("--files", nargs="+", help="scan exactly these files (relative to --root)")
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

    w = sub.add_parser("wrap", help="run a test binary and emit traceability JUnit", add_help=False)
    w.add_argument("rest", nargs=argparse.REMAINDER)
    w.set_defaults(func=cmd_wrap)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
