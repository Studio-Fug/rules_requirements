# SPDX-License-Identifier: AGPL-3.0-or-later
"""Surgical edits of model files: change one object, leave the rest alone.

Model files are hand-written and reviewed in diffs, often with comments and
section banners. Rewriting a whole file from the parsed model would destroy
all of that, so these functions locate one entity's text span using the YAML
parser's marks and splice a freshly rendered entity into exactly that span.

* :func:`render_entity` — canonical YAML for one entity (stable field order,
  flow lists for id lists, folded scalars for long prose).
* :func:`update_entity`, :func:`insert_entity`, :func:`delete_entity` —
  text-in/text-out edits of one file.
* :func:`entity_to_dict` / :func:`dict_to_entity` — the plain-data form used by
  the web API and the agents.
"""

from __future__ import annotations

import json
import re
import textwrap
from dataclasses import dataclass
from typing import Any, Mapping

from rules_requirements import config as cfg
from rules_requirements._vendor import yaml
from rules_requirements.model import FIELDS, Entity, Location, Note, VerifiedBy, parse_entity

# --------------------------------------------------------------------------- #
# Plain-data conversion                                                       #
# --------------------------------------------------------------------------- #


def entity_to_dict(ent: Entity) -> dict[str, Any]:
    """The entity as plain data, in canonical field order, omitting empties."""
    out: dict[str, Any] = {}
    for key in FIELDS[ent.kind]:
        value = getattr(ent, key, None)
        if key == "notes":
            value = [_note_dict(n) for n in ent.notes]
        elif key == "verified_by":
            value = [v.target if not v.level else {"target": v.target, "level": v.level} for v in value or ()]
        elif isinstance(value, tuple):
            value = list(value)
        if value in (None, "", [], ()):
            continue
        out[key] = value
    return out


def _note_dict(n: Note) -> dict[str, str]:
    d = {"id": n.id, "text": n.text, "kind": n.kind, "status": n.status, "author": n.author, "created": n.created}
    return {k: v for k, v in d.items() if v and not (k == "kind" and v == "comment") and not (k == "status" and v == "open")}


def dict_to_entity(kind: str, data: Mapping[str, Any], location: Location | None = None) -> tuple[Entity | None, list[str]]:
    """Parse plain data into an entity; returns (entity, problems)."""
    errors: list[str] = []
    unknown: list[str] = []
    ent = parse_entity(kind, dict(data), location or Location(), errors, unknown)
    return ent, errors + unknown


# --------------------------------------------------------------------------- #
# Rendering                                                                   #
# --------------------------------------------------------------------------- #

# Conservative "safe as a plain scalar" test: no leading indicator, no ": " or
# " #" sequences, no flow indicators.
_PLAIN = re.compile(r"^[A-Za-z0-9_(/.][A-Za-z0-9_ .,/()+'°×:@-]*$")
_RESERVED = re.compile(
    r"^(?:y|n|yes|no|on|off|true|false|null|~|[-+]?[\d._]+(?:e[-+]?\d+)?|0x[0-9a-f]+|0o[0-7]+|\d{4}-\d\d?-\d\d?.*|\d+(?::\d+)+(?:\.\d*)?)$",
    re.I,
)
WIDTH = 80


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    text = str(value)
    if _PLAIN.match(text) and not _RESERVED.match(text) and not text.endswith((" ", ":")) and ": " not in text and " #" not in text:
        return text
    return json.dumps(text, ensure_ascii=False)


def _flow_list(items: list[Any]) -> str:
    parts = []
    for item in items:
        if isinstance(item, Mapping):
            parts.append("{" + ", ".join(f"{k}: {_scalar(v)}" for k, v in item.items()) + "}")
        else:
            parts.append(_scalar(item))
    return "[" + ", ".join(parts) + "]"


def _text_block(key: str, text: str, indent: str) -> list[str]:
    """``key: value`` for prose: plain if short, folded if long, literal if multi-line."""
    one_line = f"{indent}{key}: {_scalar(text)}"
    if "\n" not in text and len(one_line) <= WIDTH:
        return [one_line]
    body_indent = indent + "  "
    if "\n" in text:
        lines = [f"{indent}{key}: |-"]
        lines += [(body_indent + ln) if ln else "" for ln in text.split("\n")]
        return lines
    if "  " in text or text != text.strip():
        return [one_line]  # folding would not round-trip; keep it quoted
    wrapped = textwrap.wrap(text, width=WIDTH - len(body_indent), break_long_words=False, break_on_hyphens=False)
    return [f"{indent}{key}: >-"] + [body_indent + ln for ln in wrapped]


_PROSE = ("description", "rationale", "procedure", "residual", "hazardous_situation", "harm", "hazard", "title")


def render_field(key: str, value: Any, pad: str) -> list[str]:
    """Lines for one ``key: value`` field, each prefixed with ``pad``."""
    if key in _PROSE:
        return _text_block(key, str(value), pad)
    if key == "notes":
        lines = [f"{pad}notes:"]
        for note in value:
            note = {"text": note} if isinstance(note, str) else dict(note)
            first = True
            for nk in ("id", "text", "kind", "status", "author", "created"):
                if not note.get(nk):
                    continue
                prefix = f"{pad}  - " if first else f"{pad}    "
                block = _text_block(nk, str(note[nk]), "")
                lines.append(prefix + block[0])
                lines += [f"{pad}    {ln}" if ln else "" for ln in block[1:]]
                first = False
        return lines
    if isinstance(value, (list, tuple)):
        flow = f"{pad}{key}: {_flow_list(list(value))}"
        if len(flow) <= WIDTH:
            return [flow]
        return [f"{pad}{key}:"] + [f"{pad}  - {_flow_list([v])[1:-1]}" for v in value]
    return [f"{pad}{key}: {_scalar(value)}"]


def _dash(lines: list[str], indent: int) -> list[str]:
    """Turn the first line (padded with indent + 2) into a ``- `` list item line."""
    if lines:
        lines = [" " * indent + "- " + lines[0][indent + 2 :]] + lines[1:]
    return lines


def render_entity(kind: str, data: Mapping[str, Any], indent: int = 0, list_item: bool = True, with_kind: bool = False) -> str:
    """Canonical YAML text for one entity (ends with a newline).

    ``list_item`` renders it as a ``- id: ...`` sequence entry whose dash sits
    at column ``indent``; otherwise as a top-level mapping (one-object files).
    """
    fields = [k for k in FIELDS[kind] if k in data and data[k] not in (None, "", [], ())]
    extra = [k for k in data if k not in FIELDS[kind] and k != "kind"]
    if extra:
        raise ValueError(f"unknown field(s) for {kind}: {', '.join(extra)}")
    pad = " " * (indent + 2) if list_item else ""
    lines: list[str] = []
    if with_kind:
        lines.append(f"{pad}kind: {kind}")
    for key in fields:
        lines += render_field(key, data[key], pad)
    if list_item:
        lines = _dash(lines, indent)
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Locating entities in text                                                   #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Span:
    start: int  # offset of the first character of the entity's first line
    end: int  # offset just past the entity's last line (incl. its newline)
    indent: int  # column of the "- " (list items) or 0 (one-object documents)
    list_item: bool
    kind: str
    section_end: int = -1  # end of the enclosing sequence's last item


def _line_start(text: str, pos: int) -> int:
    return text.rfind("\n", 0, pos) + 1


def _content_end(text: str, pos: int) -> int:
    """End of the last non-blank line at or before ``pos``, incl. its newline."""
    i = pos
    while i > 0 and text[i - 1] in " \t\r\n":
        i -= 1
    nl = text.find("\n", i)
    return len(text) if nl == -1 else nl + 1


def _node_end(node: yaml.Node) -> int:
    """Offset just past the node's last content (excluding trailing blank lines)."""
    if isinstance(node, yaml.MappingNode) and node.value and node.flow_style is not True:
        return max(_node_end(node.value[-1][0]), _node_end(node.value[-1][1]))
    if isinstance(node, yaml.SequenceNode) and node.value and node.flow_style is not True:
        return _node_end(node.value[-1])
    return int(node.end_mark.index)


def _scalar_value(node: yaml.Node) -> str:
    return str(node.value) if isinstance(node, yaml.ScalarNode) else ""


def locate(text: str, entity_id: str) -> Span | None:
    """Find ``entity_id``'s span in ``text`` (section list item or whole document)."""
    for doc in yaml.compose_all(text, Loader=yaml.SafeLoader):
        if not isinstance(doc, yaml.MappingNode):
            continue
        keys = {_scalar_value(k): v for k, v in doc.value}
        if "kind" in keys and _scalar_value(keys.get("id", yaml.ScalarNode("", ""))) == entity_id:
            kind = cfg.KIND_BY_SECTION.get(_scalar_value(keys["kind"]), _scalar_value(keys["kind"]))
            start = _line_start(text, doc.start_mark.index)
            return Span(start, _content_end(text, _node_end(doc)), 0, False, kind)
        for section, seq in keys.items():
            kind = cfg.KIND_BY_SECTION.get(section)
            if kind is None or not isinstance(seq, yaml.SequenceNode):
                continue
            for item in seq.value:
                if not isinstance(item, yaml.MappingNode):
                    continue
                ids = [_scalar_value(v) for k, v in item.value if _scalar_value(k) == "id"]
                if ids and ids[0] == entity_id:
                    start = _line_start(text, item.start_mark.index)
                    dash = text.find("-", start, item.start_mark.index)
                    indent = (dash - start) if dash != -1 else max(0, item.start_mark.column - 2)
                    end = _content_end(text, _node_end(item))
                    return Span(start, end, indent, True, kind, _content_end(text, _node_end(seq)))
    return None


def _entity_node(text: str, entity_id: str) -> yaml.MappingNode | None:
    for doc in yaml.compose_all(text, Loader=yaml.SafeLoader):
        if not isinstance(doc, yaml.MappingNode):
            continue
        keys = {_scalar_value(k): v for k, v in doc.value}
        if "kind" in keys and "id" in keys and _scalar_value(keys["id"]) == entity_id:
            return doc
        for section, seq in keys.items():
            if section in cfg.KIND_BY_SECTION and isinstance(seq, yaml.SequenceNode):
                for item in seq.value:
                    if isinstance(item, yaml.MappingNode) and any(
                        _scalar_value(k) == "id" and _scalar_value(v) == entity_id for k, v in item.value
                    ):
                        return item
    return None


def update_entity(text: str, entity_id: str, data: Mapping[str, Any]) -> str:
    """Change ``entity_id`` to ``data`` with the smallest possible text change.

    Fields whose value is unchanged keep their original text (formatting,
    comments); changed fields are re-rendered in place; removed fields are
    dropped; new fields are appended after the last existing one. Flow-style
    items (``- {id: ..., title: ...}``) are re-rendered as a whole.
    """
    span = locate(text, entity_id)
    node = _entity_node(text, entity_id)
    if span is None or node is None:
        raise KeyError(f"{entity_id} is not defined in this file")
    kind = span.kind
    old_ent, _ = dict_to_entity(kind, _node_data(node))
    new_ent, problems = dict_to_entity(kind, {k: v for k, v in data.items() if k != "kind"})
    if new_ent is None or problems:
        raise ValueError("; ".join(problems) or "invalid entity")
    before = entity_to_dict(old_ent) if old_ent else {}
    after = entity_to_dict(new_ent)
    if before == after:
        return text
    pad = " " * (span.indent + 2) if span.list_item else ""
    if node.flow_style:
        rendered = render_entity(kind, after, indent=span.indent, list_item=True)
        return text[: span.start] + rendered + text[span.end :]
    edits: list[tuple[int, int, str]] = []
    present: set[str] = set()
    last_end = span.start
    for idx, (knode, vnode) in enumerate(node.value):
        key = _scalar_value(knode)
        present.add(key)
        first_of_item = span.list_item and idx == 0
        kstart = span.start if first_of_item else _line_start(text, knode.start_mark.index)
        vend = _content_end(text, max(_node_end(vnode), int(knode.end_mark.index)))
        last_end = max(last_end, vend)
        if key == "kind" or before.get(key) == after.get(key):
            continue
        if key not in after:
            if first_of_item:  # the "- " line cannot go: re-render the whole item
                rendered = render_entity(kind, after, indent=span.indent, list_item=True)
                return text[: span.start] + rendered + text[span.end :]
            edits.append((kstart, vend, ""))
            continue
        lines = render_field(key, after[key], pad)
        if first_of_item:
            lines = _dash(lines, span.indent)
        edits.append((kstart, vend, "\n".join(lines) + "\n"))
    new_keys = [k for k in FIELDS[kind] if k in after and k not in present]
    if new_keys:
        lines = [ln for k in new_keys for ln in render_field(k, after[k], pad)]
        edits.append((last_end, last_end, "\n".join(lines) + "\n"))
    for start, end, repl in sorted(edits, reverse=True):
        text = text[:start] + repl + text[end:]
    return text


def _node_data(node: yaml.MappingNode) -> dict[str, Any]:
    """The plain data of an entity mapping node (without ``kind``)."""
    doc = yaml.SafeLoader("")
    try:
        data = doc.construct_document(node)
    finally:
        doc.dispose()
    return {k: v for k, v in (data or {}).items() if k != "kind"}


def delete_entity(text: str, entity_id: str) -> str:
    """Remove ``entity_id`` (and one now-redundant blank line)."""
    span = locate(text, entity_id)
    if span is None:
        raise KeyError(f"{entity_id} is not defined in this file")
    start, end = span.start, span.end
    if text[end : end + 1] == "\n":
        end += 1  # swallow the blank separator line after it
    elif start >= 2 and text[start - 2 : start] == "\n\n":
        start -= 1
    return text[:start] + text[end:]


def insert_entity(text: str, kind: str, data: Mapping[str, Any]) -> str:
    """Append a new entity to ``kind``'s section in ``text`` (creating it if needed)."""
    section = cfg.SECTIONS[kind]
    last: Span | None = None
    for doc in yaml.compose_all(text, Loader=yaml.SafeLoader):
        if not isinstance(doc, yaml.MappingNode):
            continue
        for k, seq in doc.value:
            if _scalar_value(k) != section or not isinstance(seq, yaml.SequenceNode):
                continue
            if seq.flow_style or not seq.value:
                continue
            ids = [
                _scalar_value(v) for item in seq.value if isinstance(item, yaml.MappingNode) for kk, v in item.value if _scalar_value(kk) == "id"
            ]
            if ids:
                last = locate(text, ids[-1])
    if last is not None:
        # Match the spacing between existing items: blank line if they use one.
        sep = "\n" if text[last.end : last.end + 1] == "\n" or _items_spaced(text, section) else ""
        new = render_entity(kind, data, indent=last.indent, list_item=True)
        return text[: last.end] + sep + new + text[last.end :]
    tail = "" if text.endswith("\n") or not text else "\n"
    lead = "\n" if text.strip() else ""
    return text + tail + lead + f"{section}:\n" + render_entity(kind, data, indent=2, list_item=True)


def _items_spaced(text: str, section: str) -> bool:
    m = re.search(rf"^{re.escape(section)}:\n(?:.*\n)*?\n\s*- ", text, re.M)
    return bool(m)


def render_file(kind: str, data: Mapping[str, Any]) -> str:
    """A one-object model file for a new entity."""
    return render_entity(kind, data, list_item=False, with_kind=True)


def verified_by_from(items: Any) -> tuple[VerifiedBy, ...]:
    out = []
    for item in items or []:
        if isinstance(item, str):
            out.append(VerifiedBy(item))
        else:
            out.append(VerifiedBy(str(item["target"]), str(item.get("level", ""))))
    return tuple(out)
