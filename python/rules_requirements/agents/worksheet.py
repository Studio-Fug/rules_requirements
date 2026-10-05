# SPDX-License-Identifier: AGPL-3.0-or-later
"""Agent proposals of case owners, written into the attribution worksheet.

An agent never edits ownership: it neither changes a ``verified_by`` /
``validated_by`` claim nor a test's tag. What it may do is *propose* one
owner (or ``none``) for a test case, as the ``proposed`` and ``reason`` of
that case in a ``.rrplan`` worksheet (:mod:`rules_requirements.migrate`),
next to ``proposed_by``. A person then decides — writes ``owner:`` — and the
decision reaches the model through ``rr migrate apply`` or an editor save,
both of which pass the one-owner checks. ``owner`` is never written here.
"""

from __future__ import annotations

import os
from typing import Any, Iterable, Mapping

from rules_requirements import migrate
from rules_requirements.case_keys import CaseKey

DEFAULT = "attribution.rrplan"
AGENT = "rr-agent/assign_cases"


class WorksheetPathError(ValueError):
    pass


def resolve(root: str, rel: str, model_paths: Iterable[str] = ()) -> str:
    """The absolute path of worksheet ``rel`` (relative to ``root``), refusing
    anything outside ``root``, not a ``.rrplan`` / ``.json`` file, or a file
    the model loader would read as part of the model (``model_paths``,
    relative to ``root`` or absolute): a worksheet there would corrupt it."""
    rel = (rel or DEFAULT).strip()
    if not rel.endswith((".rrplan", ".json")):
        raise WorksheetPathError(f"{rel}: a worksheet is a .rrplan (or .json) file")
    base = os.path.realpath(root)
    path = os.path.realpath(os.path.join(base, rel))
    if not path.startswith(base + os.sep) or ".git" in os.path.relpath(path, base).split(os.sep):
        raise WorksheetPathError(f"{rel} is outside the workspace")
    for model_path in model_paths:
        if _model_would_read(os.path.realpath(os.path.join(base, model_path)), path):
            raise WorksheetPathError(
                f"{rel} would be read as part of the model ({model_path}); put the worksheet outside the model "
                "directories, or name it .rrplan"
            )
    return path


def _model_would_read(model_path: str, path: str) -> bool:
    """Whether :func:`~rules_requirements.model.model_files` over
    ``model_path`` would load ``path`` (both absolute, resolved)."""
    if path == model_path:
        return True
    if not path.startswith(model_path.rstrip(os.sep) + os.sep) or not path.endswith((".yaml", ".yml", ".json")):
        return False
    folders = os.path.relpath(path, model_path).split(os.sep)[:-1]
    return not any(f.startswith(".") for f in folders)


def _group_of(path: str) -> str:
    prefix, sep, _ = path.partition("::")
    return prefix if sep else ""


def propose(
    doc: dict[str, Any],
    key: CaseKey,
    owner: str,
    reason: str,
    *,
    candidates: Iterable[str] = (),
    status: str = "",
    by: str = AGENT,
) -> dict[str, Any]:
    """Record the proposal ``key`` -> ``owner`` (an id or ``none``) in ``doc``;
    returns the case entry. A decided ``owner`` is never touched."""
    if not owner or any(ch in owner for ch in ", \t\n"):
        raise ValueError(f"{owner!r}: a proposal names exactly one owner, or none")
    groups = doc.setdefault("groups", [])
    entry: dict[str, Any] | None = None
    for g in groups:
        if isinstance(g, dict) and g.get("target") == key.target:
            for c in g.get("cases", []) or []:
                if isinstance(c, dict) and c.get("path") == key.path:
                    entry = c
                    break
        if entry is not None:
            break
    if entry is None:
        group_name = _group_of(key.path)
        group = next(
            (
                g
                for g in groups
                if isinstance(g, dict) and g.get("target") == key.target and g.get("group", "") == group_name
            ),
            None,
        )
        counts = sorted({*candidates, *([owner] if owner != migrate.NONE else [])})
        if group is None:
            group = {"target": key.target, "group": group_name, "counts_toward": counts, "owner": migrate.OPEN}
            group["cases"] = []
            groups.append(group)
        entry = {"path": key.path}
        if status:
            entry["status"] = status
        if counts != list(group.get("counts_toward") or []):
            entry["counts_toward"] = counts
        group.setdefault("cases", []).append(entry)
    entry["proposed"] = owner
    entry["reason"] = reason
    entry["proposed_by"] = by
    return entry


def record(
    root: str, rel: str, proposals: Iterable[Mapping[str, Any]], by: str = AGENT, model_paths: Iterable[str] = ()
) -> tuple[str, int]:
    """Write ``proposals`` (``{case, owner, rationale, candidates, status}``)
    into the worksheet at ``rel`` (created when absent; never a file of the
    model at ``model_paths``); returns its path relative to ``root`` and the
    number recorded."""
    path = resolve(root, rel, model_paths)
    if os.path.exists(path):
        doc = migrate.load_worksheet(path)
    else:
        doc = {"schema": migrate.SCHEMA, "summary": {}, "targets": [], "groups": []}
    n = 0
    for p in proposals:
        propose(
            doc,
            CaseKey.parse(str(p["case"])),
            str(p["owner"]),
            str(p.get("rationale", "")),
            candidates=p.get("candidates", ()) or (),
            status=str(p.get("status", "")),
            by=by,
        )
        n += 1
    old = doc.get("summary") or {}
    doc["summary"] = migrate.summary(doc, **{k: old[k] for k in ("units", "attributed") if k in old})
    text = migrate.render_json(doc) if path.endswith(".json") else migrate.render_yaml(doc)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.rr-tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    os.replace(tmp, path)
    return os.path.relpath(path, os.path.realpath(root)).replace(os.sep, "/"), n


__all__ = ["AGENT", "DEFAULT", "WorksheetPathError", "propose", "record", "resolve"]
