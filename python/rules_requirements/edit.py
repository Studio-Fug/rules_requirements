# SPDX-License-Identifier: AGPL-3.0-or-later
"""Surgical edits of model files: change one object, leave the rest alone.

Model files are hand-written and reviewed in diffs, often with comments and
section banners. Rewriting a whole file from the parsed model would destroy
all of that, so these functions locate one entity's text span using the YAML
parser's marks and splice freshly rendered text into exactly that span —
field by field, so unchanged fields keep their original text byte for byte.

Every rendered scalar and block is re-parsed to confirm it round-trips (with a
double-quoted fallback), and :func:`verify` re-parses the whole edited file to
confirm that the edit changed exactly what was intended. Layouts that cannot be
spliced safely — flow-style sections (``requirements: [{...}, {...}]``) and
JSON files — are refused with :class:`EditError` instead of being rewritten.

* :func:`render_entity` — canonical YAML for one entity.
* :func:`update_entity`, :func:`insert_entity`, :func:`delete_entity` —
  text-in/text-out edits of one file.
* :func:`verify` — the post-edit check used by the web editor for every write.
* :func:`entity_to_dict` / :func:`dict_to_entity` — the plain-data form used by
  the web API and the agents.
"""

from __future__ import annotations

import re
import textwrap
from dataclasses import dataclass
from typing import Any, Mapping

from rules_requirements import config as cfg
from rules_requirements._vendor import yaml
from rules_requirements.model import (
    FIELDS,
    Entity,
    Location,
    Model,
    Note,
    VerifiedBy,
    _LineLoader,
    parse_documents,
    parse_entity,
)


class EditError(ValueError):
    """An edit that cannot be applied safely to this file's layout."""


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
    return {
        k: v for k, v in d.items() if v and not (k == "kind" and v == "comment") and not (k == "status" and v == "open")
    }


def dict_to_entity(
    kind: str, data: Mapping[str, Any], location: Location | None = None
) -> tuple[Entity | None, list[str]]:
    """Parse plain data into an entity; returns (entity, problems)."""
    errors: list[str] = []
    unknown: list[str] = []
    ent = parse_entity(kind, dict(data), location or Location(), errors, unknown)
    return ent, errors + unknown


def normalize(kind: str, data: Mapping[str, Any]) -> dict[str, Any]:
    """``data`` as the model would read it back (canonical plain form)."""
    ent, problems = dict_to_entity(kind, {k: v for k, v in data.items() if k != "kind"})
    if ent is None or problems:
        raise EditError("; ".join(problems) or "invalid entity")
    return entity_to_dict(ent)


# --------------------------------------------------------------------------- #
# Rendering                                                                   #
# --------------------------------------------------------------------------- #

WIDTH = 80
_STR_TAG = "tag:yaml.org,2002:str"
_RESOLVER = yaml.resolver.Resolver()
# Conservative "safe as a plain scalar" character test; the resolver check
# additionally rejects anything YAML would read as a non-string (numbers in any
# base, booleans, null, timestamps, .nan / .inf, ...).
_PLAIN = re.compile(r"^[A-Za-z0-9_(/.][A-Za-z0-9_ .,/()+'°×:@-]*$")


def _plain_ok(text: str) -> bool:
    return (
        bool(_PLAIN.match(text))
        and not text.endswith((" ", ":"))
        and ": " not in text
        and " #" not in text
        and _RESOLVER.resolve(yaml.ScalarNode, text, (True, False)) == _STR_TAG
    )


def _dq(text: str) -> str:
    """A YAML double-quoted scalar that reads back exactly as ``text``."""
    out = ['"']
    for ch in text:
        o = ord(ch)
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\r":
            out.append("\\r")
        elif o < 0x20 or o == 0x7F or 0x80 <= o < 0xA0 or o in (0x2028, 0x2029, 0xFEFF, 0xFFFE, 0xFFFF):
            out.append(f"\\x{o:02x}" if o <= 0xFF else f"\\u{o:04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    text = str(value)
    return text if _plain_ok(text) else _dq(text)


def _reads_back(snippet: str, expected: Any) -> bool:
    try:
        return bool(yaml.safe_load(snippet) == expected)
    except yaml.YAMLError:
        return False


def _flow_list(items: list[Any]) -> str:
    parts = []
    for item in items:
        if isinstance(item, Mapping):
            parts.append("{" + ", ".join(f"{k}: {_scalar(v)}" for k, v in item.items()) + "}")
        else:
            parts.append(_scalar(item))
    return "[" + ", ".join(parts) + "]"


def _text_block(key: str, text: str, indent: str) -> list[str]:
    """``key: value`` for prose: plain if short, folded if long, literal if multi-line.

    Each candidate style is re-parsed; the first that reads back exactly wins,
    with a double-quoted scalar as the always-correct fallback.
    """
    body = indent + "  "
    candidates: list[list[str]] = [[f"{indent}{key}: {_scalar(text)}"]]
    if "\n" in text:
        candidates.insert(0, [f"{indent}{key}: |-"] + [(body + ln) if ln else "" for ln in text.split("\n")])
    elif len(candidates[0][0]) > WIDTH:
        wrapped = textwrap.wrap(
            text,
            width=max(20, WIDTH - len(body)),
            break_long_words=False,
            break_on_hyphens=False,
            expand_tabs=False,
            replace_whitespace=False,
        )
        candidates.insert(0, [f"{indent}{key}: >-"] + [body + ln for ln in wrapped])
    for lines in candidates:
        snippet = "\n".join(ln[len(indent) :] if ln.startswith(indent) else ln for ln in lines) + "\n"
        if _reads_back(snippet, {key: text}):
            return lines
    return [f"{indent}{key}: {_dq(text)}"]


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
        items = list(value)
        flow = f"{pad}{key}: {_flow_list(items)}"
        if len(flow) <= WIDTH and _reads_back(f"{key}: {_flow_list(items)}\n", {key: items}):
            return [flow]
        out = [f"{pad}{key}:"]
        for item in items:
            if isinstance(item, Mapping):
                out.append(f"{pad}  - {{" + ", ".join(f"{k}: {_dq(str(v))}" for k, v in item.items()) + "}")
            else:
                out.append(f"{pad}  - {_scalar(item)}")
        return out
    return [f"{pad}{key}: {_scalar(value)}"]


def render_entity(
    kind: str,
    data: Mapping[str, Any],
    indent: int = 0,
    list_item: bool = True,
    with_kind: bool = False,
    prefix: str = "",
    pad: str | None = None,
) -> str:
    """Canonical YAML text for one entity (ends with a newline).

    ``list_item`` renders it as a sequence entry: ``prefix`` (default ``"- "``
    at column ``indent``) starts the first line and every field is indented
    with ``pad`` (default: to the column after the dash). Otherwise it is
    rendered as a top-level mapping (one-object files).
    """
    fields = [k for k in FIELDS[kind] if k in data and data[k] not in (None, "", [], ())]
    extra = [k for k in data if k not in FIELDS[kind] and k != "kind"]
    if extra:
        raise ValueError(f"unknown field(s) for {kind}: {', '.join(extra)}")
    if list_item:
        prefix = prefix or (" " * indent + "- ")
        pad = pad if pad is not None else " " * len(prefix)
    else:
        prefix, pad = "", ""
    lines: list[str] = []
    if with_kind:
        lines.append(f"{pad}kind: {kind}")
    for key in fields:
        lines += render_field(key, data[key], pad)
    if list_item and lines:
        lines[0] = prefix + lines[0][len(pad) :]
    return "\n".join(lines) + "\n"


def render_file(kind: str, data: Mapping[str, Any]) -> str:
    """A one-object model file for a new entity."""
    return render_entity(kind, data, list_item=False, with_kind=True)


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
    prefix: str = ""  # first-line text before the first key, e.g. "  - "
    pad: str = ""  # indentation of the entity's other keys
    flow: bool = False  # inside a flow collection: not editable in place


def _line_start(text: str, pos: int) -> int:
    return text.rfind("\n", 0, pos) + 1


def _line_end(text: str, pos: int) -> int:
    nl = text.find("\n", pos)
    return len(text) if nl == -1 else nl + 1


def _content_end(text: str, pos: int) -> int:
    """End of the last non-blank line at or before ``pos``, incl. its newline."""
    i = pos
    while i > 0 and text[i - 1] in " \t\r\n":
        i -= 1
    return _line_end(text, i)


def _node_end(node: yaml.Node) -> int:
    """Offset just past the node's last content (excluding trailing blank lines)."""
    if isinstance(node, yaml.MappingNode) and node.value and node.flow_style is not True:
        return max(_node_end(node.value[-1][0]), _node_end(node.value[-1][1]))
    if isinstance(node, yaml.SequenceNode) and node.value and node.flow_style is not True:
        return _node_end(node.value[-1])
    return int(node.end_mark.index)


def _scalar_value(node: yaml.Node) -> str:
    return str(node.value) if isinstance(node, yaml.ScalarNode) else ""


def _compose(text: str) -> list[yaml.Node]:
    try:
        return [d for d in yaml.compose_all(text, Loader=yaml.SafeLoader) if d is not None]
    except yaml.YAMLError as exc:
        raise EditError(f"the file is not valid YAML: {exc}") from exc


def _find(text: str, entity_id: str) -> tuple[Span, yaml.MappingNode] | None:
    """The entity's span and (unflattened) mapping node."""
    for doc in _compose(text):
        if not isinstance(doc, yaml.MappingNode):
            continue
        keys = {_scalar_value(k): v for k, v in doc.value}
        if "kind" in keys and _scalar_value(keys.get("id", yaml.ScalarNode("", ""))) == entity_id:
            doc_kind = _scalar_value(keys["kind"])
            kind = cfg.KIND_BY_SECTION.get(doc_kind, doc_kind)
            start = _line_start(text, doc.start_mark.index)
            span = Span(start, _content_end(text, _node_end(doc)), 0, False, kind, flow=bool(doc.flow_style))
            return span, doc
        for section, seq in keys.items():
            section_kind = cfg.KIND_BY_SECTION.get(section)
            if section_kind is None or not isinstance(seq, yaml.SequenceNode):
                continue
            kind = section_kind
            for item in seq.value:
                if not isinstance(item, yaml.MappingNode):
                    continue
                ids = [_scalar_value(v) for k, v in item.value if _scalar_value(k) == "id"]
                if not ids or ids[0] != entity_id:
                    continue
                start = _line_start(text, item.start_mark.index)
                first_key = item.value[0][0] if item.value else item
                dash = text.find("-", start, item.start_mark.index)
                indent = (dash - start) if dash != -1 else max(0, item.start_mark.column - 2)
                end = _content_end(text, _node_end(item))
                flow = bool(seq.flow_style)
                prefix = text[start : first_key.start_mark.index]
                if item.flow_style:
                    # A flow mapping on its own line(s) inside a block list can be
                    # replaced whole; anything else sharing those lines cannot.
                    before = text[start : item.start_mark.index]
                    after = text[item.end_mark.index : _line_end(text, item.end_mark.index)].strip()
                    flow = flow or not re.fullmatch(r"\s*-\s*", before) or bool(after and not after.startswith("#"))
                    end = _line_end(text, item.end_mark.index)
                    prefix = " " * indent + "- "
                pad = " " * first_key.start_mark.column if not item.flow_style else " " * (indent + 2)
                return Span(start, end, indent, True, kind, prefix=prefix, pad=pad, flow=flow), item
    return None


def locate(text: str, entity_id: str) -> Span | None:
    """Find ``entity_id``'s span in ``text`` (section list item or whole document)."""
    found = _find(text, entity_id)
    return found[0] if found else None


def _parse(text: str, rel: str = "<edit>") -> Model:
    try:
        docs = [d for d in yaml.load_all(text, Loader=_LineLoader) if d is not None]
    except yaml.YAMLError as exc:
        raise EditError(f"the edited file would not be valid YAML: {exc}") from exc
    model, _ = parse_documents([(rel, d) for d in docs])
    return model


def _entity_data(text: str, entity_id: str) -> dict[str, Any]:
    ent = _parse(text).get(entity_id)
    return entity_to_dict(ent) if ent is not None else {}


def _refuse_flow(span: Span, entity_id: str) -> None:
    if span.flow:
        raise EditError(
            f"{entity_id} is written in flow style (e.g. `[{{...}}, {{...}}]` or JSON); "
            "the editor cannot change it without rewriting its neighbours — edit the file by hand "
            "or convert it to block style"
        )


# --------------------------------------------------------------------------- #
# Edits                                                                       #
# --------------------------------------------------------------------------- #


def update_entity(text: str, entity_id: str, data: Mapping[str, Any]) -> str:
    """Change ``entity_id`` to ``data`` with the smallest possible text change.

    Fields whose value is unchanged keep their original text (formatting,
    comments); changed fields are re-rendered in place; removed fields are
    dropped; new fields are appended after the last existing one. A flow
    mapping on its own line, or an entity using merge keys (``<<: *base``), is
    re-rendered as a whole.
    """
    found = _find(text, entity_id)
    if found is None:
        raise KeyError(f"{entity_id} is not defined in this file")
    span, node = found
    _refuse_flow(span, entity_id)
    kind = span.kind
    before = _entity_data(text, entity_id)
    after = normalize(kind, data)
    if before == after:
        return text
    merged = any(k.tag == "tag:yaml.org,2002:merge" for k, _ in node.value)
    if span.list_item and (node.flow_style or merged):
        rendered = render_entity(kind, after, indent=span.indent, list_item=True)
        return text[: span.start] + rendered + text[span.end :]
    if not span.list_item and merged:
        return text[: span.start] + render_entity(kind, after, list_item=False, with_kind=True) + text[span.end :]
    pad = span.pad if span.list_item else ""
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
                rendered = render_entity(kind, after, indent=span.indent, list_item=True, prefix=span.prefix, pad=pad)
                return text[: span.start] + rendered + text[span.end :]
            edits.append((kstart, vend, ""))
            continue
        lines = render_field(key, after[key], pad)
        if first_of_item:
            lines[0] = span.prefix + lines[0][len(pad) :]
        edits.append((kstart, vend, "\n".join(lines) + "\n"))
    new_keys = [k for k in FIELDS[kind] if k in after and k not in present]
    if new_keys:
        lines = [ln for k in new_keys for ln in render_field(k, after[k], pad)]
        lead = "" if last_end == 0 or text[last_end - 1 : last_end] == "\n" else "\n"
        edits.append((last_end, last_end, lead + "\n".join(lines) + "\n"))
    for start, end, repl in sorted(edits, reverse=True):
        text = text[:start] + repl + text[end:]
    return text


def delete_entity(text: str, entity_id: str) -> str:
    """Remove ``entity_id`` (and one now-redundant blank line)."""
    found = _find(text, entity_id)
    if found is None:
        raise KeyError(f"{entity_id} is not defined in this file")
    span, _ = found
    _refuse_flow(span, entity_id)
    start, end = span.start, span.end
    if text[end : end + 1] == "\n":
        end += 1  # swallow the blank separator line after it
    elif start >= 2 and text[start - 2 : start] == "\n\n":
        start -= 1
    return text[:start] + text[end:]


def insert_entity(text: str, kind: str, data: Mapping[str, Any]) -> str:
    """Append a new entity to ``kind``'s section in ``text`` (creating it if needed)."""
    section = cfg.SECTIONS[kind]
    body = normalize(kind, data)
    for doc in _compose(text):
        if not isinstance(doc, yaml.MappingNode) or any(_scalar_value(k) == "kind" for k, _ in doc.value):
            continue
        for knode, seq in doc.value:
            if _scalar_value(knode) != section:
                continue
            item_indent = knode.start_mark.column + 2
            if isinstance(seq, yaml.ScalarNode) and seq.tag == "tag:yaml.org,2002:null":
                # `requirements:` (null) — give it a block list
                colon = text.index(":", knode.end_mark.index)
                end = _line_end(text, max(colon, seq.end_mark.index))
                rendered = render_entity(kind, body, indent=item_indent, list_item=True)
                return text[: colon + 1] + "\n" + rendered + text[end:]
            if isinstance(seq, yaml.SequenceNode) and seq.flow_style and not seq.value:
                colon = text.index(":", knode.end_mark.index)  # `requirements: []`
                rendered = render_entity(kind, body, indent=item_indent, list_item=True)
                return text[: colon + 1] + "\n" + rendered + text[_line_end(text, seq.end_mark.index) :]
            if not isinstance(seq, yaml.SequenceNode) or seq.flow_style:
                raise EditError(f"the {section} section is not a block list; add the entity by hand")
            items = [i for i in seq.value if isinstance(i, yaml.MappingNode)]
            last_ids = [_scalar_value(v) for k, v in items[-1].value if _scalar_value(k) == "id"] if items else []
            found = _find(text, last_ids[0]) if last_ids else None
            if found is None:
                raise EditError(f"cannot find where the {section} section ends; add the entity by hand")
            span = found[0]
            rendered = render_entity(kind, body, indent=span.indent, list_item=True, prefix=span.prefix, pad=span.pad)
            end = span.end
            lead = "" if text[end - 1 : end] == "\n" else "\n"
            sep = "\n" if text[end : end + 1] == "\n" or _items_spaced(text, section) else ""
            return text[:end] + lead + sep + rendered + text[end:]
    tail = "" if text.endswith("\n") or not text else "\n"
    lead = "\n" if text.strip() else ""
    return text + tail + lead + f"{section}:\n" + render_entity(kind, body, indent=2, list_item=True)


def _items_spaced(text: str, section: str) -> bool:
    return bool(re.search(rf"^{re.escape(section)}:\n(?:.*\n)*?\n\s*- ", text, re.M))


# --------------------------------------------------------------------------- #
# Verification                                                                #
# --------------------------------------------------------------------------- #


_LOCATION = re.compile(r"^.*?:\d+: ")


def _problems(messages: Any, aliases: Mapping[str, str]) -> list[str]:
    """Problem messages without their (shifting) file:line prefix, with
    renamed ids mapped back to their old names."""
    out = []
    for msg in messages:
        msg = _LOCATION.sub("", msg)
        for new_id, old_id in aliases.items():
            msg = re.sub(rf"(?<![\w-]){re.escape(new_id)}(?![\w-])", old_id, msg)
        out.append(msg)
    return sorted(out)


def verify(
    old_text: str,
    new_text: str,
    expect: Mapping[str, Mapping[str, Any] | None],
    rel: str = "",
    aliases: Mapping[str, str] | None = None,
) -> None:
    """Check that ``new_text`` differs from ``old_text`` exactly as intended.

    ``expect`` maps entity ids to their intended data (``None`` = removed);
    every other entity, the configuration and the project metadata must read
    back unchanged, and the edit must not introduce parse errors or unknown
    fields (``aliases`` maps renamed ids, new -> old, for that comparison).
    Raises :class:`EditError` otherwise — the caller then leaves the file
    untouched.
    """
    aliases = aliases or {}
    old = _parse(old_text, rel) if old_text.strip() else Model()
    new = _parse(new_text, rel)
    before, after = _problems(old.parse_errors, aliases), _problems(new.parse_errors, aliases)
    fresh = [e for e in after if e not in before or after.count(e) > before.count(e)]
    if fresh:
        raise EditError("the edit would introduce model errors: " + "; ".join(fresh[:3]))
    before_u, after_u = _problems(old.unknown_fields, aliases), _problems(new.unknown_fields, aliases)
    if any(u not in before_u or after_u.count(u) > before_u.count(u) for u in after_u):
        raise EditError("the edit would introduce unknown fields")
    old_ents = {e.id: e for e in old.entities()}
    new_ents = {e.id: e for e in new.entities()}
    for eid in sorted(set(old_ents) | set(new_ents) | set(expect)):
        a, b = old_ents.get(eid), new_ents.get(eid)
        if eid in expect:
            want = expect[eid]
            if want is None:
                if b is not None:
                    raise EditError(f"{eid} is still present after deleting it")
                continue
            if b is None:
                raise EditError(f"{eid} is missing after the edit")
            if entity_to_dict(b) != normalize(b.kind, want):
                raise EditError(f"{eid} does not read back as intended after the edit")
        elif a is None or b is None or a.kind != b.kind or entity_to_dict(a) != entity_to_dict(b):
            raise EditError(f"the edit would also change {eid}; refusing to write")
    if old.config != new.config or dict(old.project) != dict(new.project):
        raise EditError("the edit would change the configuration or project metadata; refusing to write")


def verified_by_from(items: Any) -> tuple[VerifiedBy, ...]:
    out = []
    for item in items or []:
        if isinstance(item, str):
            out.append(VerifiedBy(item))
        else:
            out.append(VerifiedBy(str(item["target"]), str(item.get("level", ""))))
    return tuple(out)
