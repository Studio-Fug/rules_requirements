# SPDX-License-Identifier: AGPL-3.0-or-later
"""Semantic diff of two models: which entities were added, removed or changed,
field by field — the "what changed in the system definition" view of a branch,
independent of how the YAML happens to be laid out."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from rules_requirements.edit import entity_to_dict
from rules_requirements.model import Model
from rules_requirements.util import natural_key

ADDED, REMOVED, MODIFIED = "added", "removed", "modified"


@dataclass
class EntityChange:
    id: str
    kind: str
    change: str  # added | removed | modified
    title: str = ""
    fields: dict[str, tuple[Any, Any]] = field(default_factory=dict)  # name -> (old, new)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "change": self.change,
            "title": self.title,
            "fields": {k: {"old": o, "new": n} for k, (o, n) in self.fields.items()},
        }


def diff_models(old: Model, new: Model, ignore: tuple[str, ...] = ()) -> list[EntityChange]:
    """Entity-level changes from ``old`` to ``new`` (sorted by kind, then id).

    ``ignore`` lists field names to leave out of the comparison (e.g. ``notes``).
    """
    changes: list[EntityChange] = []
    old_ids = {e.id: e for e in old.entities()}
    new_ids = {e.id: e for e in new.entities()}
    for eid in sorted(old_ids.keys() | new_ids.keys(), key=natural_key):
        a, b = old_ids.get(eid), new_ids.get(eid)
        if a is None and b is not None:
            changes.append(EntityChange(eid, b.kind, ADDED, b.title, {k: (None, v) for k, v in _d(b, ignore).items()}))
        elif b is None and a is not None:
            changes.append(EntityChange(eid, a.kind, REMOVED, a.title, {k: (v, None) for k, v in _d(a, ignore).items()}))
        elif a is not None and b is not None:
            da, db = _d(a, ignore), _d(b, ignore)
            fields = {k: (da.get(k), db.get(k)) for k in list(da) + [k for k in db if k not in da] if da.get(k) != db.get(k)}
            if a.kind != b.kind:
                fields["kind"] = (a.kind, b.kind)
            if fields:
                changes.append(EntityChange(eid, b.kind, MODIFIED, b.title, fields))
    order = {"user_need": 0, "requirement": 1, "risk": 2, "mitigation": 3, "test_method": 4}
    changes.sort(key=lambda c: (order.get(c.kind, 9), natural_key(c.id)))
    return changes


def _d(ent: Any, ignore: tuple[str, ...]) -> dict[str, Any]:
    return {k: v for k, v in entity_to_dict(ent).items() if k not in ignore}


def summarize(changes: list[EntityChange]) -> dict[str, int]:
    out = {ADDED: 0, REMOVED: 0, MODIFIED: 0}
    for c in changes:
        out[c.change] += 1
    return out


def render_text(changes: list[EntityChange]) -> str:
    """A compact, human-readable change list."""
    lines = []
    for c in changes:
        mark = {ADDED: "+", REMOVED: "-", MODIFIED: "~"}[c.change]
        lines.append(f"{mark} {c.id} ({c.kind}) {c.title}")
        if c.change == MODIFIED:
            for name, (o, n) in c.fields.items():
                lines.append(f"    {name}: {o!r} -> {n!r}")
    return "\n".join(lines) + ("\n" if lines else "")
