# SPDX-License-Identifier: AGPL-3.0-or-later
"""``rr migrate apply --stage tags``: split multi-id test tags, per a worksheet.

Python tests declare what they verify with ``@pytest.mark.rr`` /
``@pytest.mark.requirements`` (on a function, a class, or as a module or class
``pytestmark``) and ``@rr.verifies`` (function or class). Ids from every scope
accumulate today, so a module-level ``pytestmark`` naming two ids makes every
test in the module count toward both.

Given the owners decided on an attribution worksheet
(:mod:`rules_requirements.migrate`), this codemod rewrites the declarations so
each test names exactly its decided owner:

* a scope declaration (module or class) whose tests all went to one owner is
  narrowed to that one id, keeping its ``level`` / ``artifact``;
* otherwise it is removed, and each test under it gets its own single-id
  declaration (same spelling, same ``level`` / ``artifact``);
* a test decided ``none`` loses its tags.

It works on the syntax tree (:mod:`ast`) and edits whole source lines, so
everything else in the file — comments, formatting, other markers — is left
as it was. Every rewrite is checked before it is written: the new file is
parsed again and each test's effective ``(ids, level, artifact)`` must be
exactly the intended one, or the file is left untouched. New lines are
rendered the way black formats them, so running black afterwards changes
nothing.

A file is refused — reported, never half-migrated — when a test it would
change is undecided (``?``, unless ``unassigned="drop"``), when the
parametrizations of one test were decided differently (split those by hand
with ``pytest.param(..., marks=...)``), or when a declaration is not a literal
the codemod can read.
"""

from __future__ import annotations

import ast
import os
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Optional, Union

from rules_requirements.case_keys import CaseKey
from rules_requirements.util import dedupe

MARKER_NAMES = ("rr", "requirements")
_SKIP_DIRS = {"node_modules", "__pycache__", "site-packages", "_vendor"}
_FuncDef = Union[ast.FunctionDef, ast.AsyncFunctionDef]

# What a test declares: ids (accumulated), level (nearest wins), artifact.
Trace = tuple[tuple[str, ...], str, tuple[tuple[str, str], ...]]


class Unsupported(Exception):  # noqa: N818 - reads as "raise Unsupported(why)"
    """A declaration the codemod cannot rewrite safely."""


@dataclass
class Decl:
    """One ``rr`` declaration in the source."""

    scope: str  # "module" | "class" | "function"
    holder: ast.AST  # the Module / ClassDef / FunctionDef it applies to
    call: ast.Call
    callee: str  # source spelling, e.g. "pytest.mark.requirements"
    ids: list[str]
    level: str
    artifact: dict[str, str]
    site: str  # "decorator" | "pytestmark"
    stmt: Optional[ast.AST] = None  # the pytestmark assignment


@dataclass
class TestFn:
    __test__ = False

    node: _FuncDef
    classes: tuple[ast.ClassDef, ...]  # outermost first

    @property
    def qualname(self) -> str:
        return ".".join([c.name for c in self.classes] + [self.node.name])


@dataclass
class FileResult:
    path: str  # relative to the root
    status: str  # "changed" | "unchanged" | "refused"
    reasons: list[str] = field(default_factory=list)
    changes: list[str] = field(default_factory=list)  # "test_x: PR-1, PR-2 -> PR-1"
    old_text: str = ""
    new_text: str = ""


@dataclass
class ApplyResult:
    files: list[FileResult] = field(default_factory=list)
    unresolved: list[tuple[CaseKey, str]] = field(default_factory=list)  # no Python test found
    untagged: list[CaseKey] = field(default_factory=list)  # decided, but the test carries no tag

    @property
    def refused(self) -> list[FileResult]:
        return [f for f in self.files if f.status == "refused"]

    @property
    def changed(self) -> list[FileResult]:
        return [f for f in self.files if f.status == "changed"]


# --------------------------------------------------------------------------- #
# Reading a test file                                                         #
# --------------------------------------------------------------------------- #


def _dotted(node: ast.AST) -> str:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return ""
    parts.append(node.id)
    return ".".join(reversed(parts))


class TestFile:
    """The ``rr`` declarations and the tests of one Python source file."""

    __test__ = False

    def __init__(self, text: str, path: str = "<string>") -> None:
        self.text = text
        self.path = path
        self.lines = text.splitlines(keepends=True)
        self.tree = ast.parse(text, filename=path)
        self._aliases()
        self.decls: list[Decl] = []
        self.tests: list[TestFn] = []
        self.problems: list[str] = []
        self._collect(self.tree, "module", ())
        self._unsupported()

    # -- names ------------------------------------------------------------ #

    def _aliases(self) -> None:
        self.pytest_names: set[str] = set()
        self.mark_names: set[str] = set()
        self.rr_names: set[str] = {"rules_requirements.rr"}
        self.verifies_names: set[str] = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.name == "pytest":
                        self.pytest_names.add(a.asname or "pytest")
                    elif a.name == "rules_requirements.rr" and a.asname:
                        self.rr_names.add(a.asname)
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                for a in node.names:
                    if node.module == "pytest" and a.name == "mark":
                        self.mark_names.add(a.asname or "mark")
                    elif node.module == "rules_requirements" and a.name == "rr":
                        self.rr_names.add(a.asname or "rr")
                    elif node.module == "rules_requirements.rr" and a.name == "verifies":
                        self.verifies_names.add(a.asname or "verifies")

    def is_decl(self, node: ast.AST) -> bool:
        if not isinstance(node, ast.Call):
            return False
        name = _dotted(node.func)
        head, _, last = name.rpartition(".")
        if last in MARKER_NAMES:
            base, _, mark = head.rpartition(".")
            return (mark == "mark" and base in self.pytest_names) or head in self.mark_names
        if last == "verifies":
            return head in self.rr_names or (not head and name in self.verifies_names)
        return False

    # -- structure -------------------------------------------------------- #

    def _collect(self, holder: ast.AST, scope: str, classes: tuple[ast.ClassDef, ...]) -> None:
        body: list[ast.stmt] = getattr(holder, "body", [])
        for stmt in body:
            if isinstance(stmt, ast.Assign) and [_dotted(t) for t in stmt.targets] == ["pytestmark"]:
                values = stmt.value.elts if isinstance(stmt.value, (ast.List, ast.Tuple)) else [stmt.value]
                for v in values:
                    if self.is_decl(v):
                        self._decl(scope, holder, v, "pytestmark", stmt)  # type: ignore[arg-type]
            elif isinstance(stmt, ast.ClassDef):
                self._decorators(stmt, "class")
                self._collect(stmt, "class", classes + (stmt,))
            elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._decorators(stmt, "function")
                if stmt.name.startswith("test"):
                    self.tests.append(TestFn(stmt, classes))

    def _decorators(self, node: ast.AST, scope: str) -> None:
        for deco in getattr(node, "decorator_list", []):
            if self.is_decl(deco):
                self._decl(scope, node, deco, "decorator", None)

    def _decl(self, scope: str, holder: ast.AST, call: ast.Call, site: str, stmt: Optional[ast.stmt]) -> None:
        line = call.lineno
        ids: list[str] = []
        for arg in call.args:
            items = arg.elts if isinstance(arg, (ast.List, ast.Tuple, ast.Set)) else [arg]
            for item in items:
                if not (isinstance(item, ast.Constant) and isinstance(item.value, str)):
                    self.problems.append(f"line {line}: an id that is not a string literal")
                    return
                ids.extend(p.strip() for p in item.value.split(","))
        level, artifact = "", {}
        for kw in call.keywords:
            if kw.arg == "level":
                if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, (str, type(None))):
                    level = (kw.value.value or "").strip().lower()
                else:
                    self.problems.append(f"line {line}: a level that is not a string literal")
                    return
            elif kw.arg == "artifact":
                try:
                    raw = ast.literal_eval(kw.value)
                except (ValueError, TypeError, SyntaxError):
                    raw = None
                if not isinstance(raw, dict):
                    self.problems.append(f"line {line}: an artifact that is not a dict literal")
                    return
                artifact = {str(k): str(v) for k, v in raw.items()}
            elif kw.arg is None:
                self.problems.append(f"line {line}: **kwargs in a declaration")
                return
        callee = ast.get_source_segment(self.text, call.func) or _dotted(call.func)
        self.decls.append(Decl(scope, holder, call, callee, dedupe(ids), level, artifact, site, stmt))

    def _unsupported(self) -> None:
        known = {id(d.call) for d in self.decls}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call) and self.is_decl(node) and id(node) not in known:
                self.problems.append(
                    f"line {node.lineno}: a declaration outside a decorator or pytestmark (e.g. pytest.param marks)"
                )

    # -- semantics -------------------------------------------------------- #

    def chain(self, test: TestFn, decls: Iterable[Decl] | None = None) -> list[list[Decl]]:
        """The declarations applying to ``test``, nearest scope first."""
        pool = list(self.decls if decls is None else decls)
        holders: list[ast.AST] = [test.node, *reversed(test.classes), self.tree]
        return [[d for d in pool if d.holder is h] for h in holders]

    def trace(self, test: TestFn, decls: Iterable[Decl] | None = None) -> Trace:
        """``(ids, level, artifact)`` as the pytest plugin resolves them."""
        ids: list[str] = []
        level = ""
        artifact: dict[str, str] = {}
        for scope in self.chain(test, decls):
            levels = {d.level for d in scope if d.level}
            if len(levels) > 1:
                raise Unsupported(f"{test.qualname}: one scope declares different levels ({', '.join(sorted(levels))})")
            for d in scope:
                ids.extend(d.ids)
                level = level or d.level
            for d in scope:
                for k, v in d.artifact.items():
                    artifact.setdefault(k, v)
        return tuple(dedupe(ids)), level, tuple(sorted(artifact.items()))

    def tests_under(self, holder: ast.AST) -> list[TestFn]:
        if holder is self.tree:
            return list(self.tests)
        return [t for t in self.tests if t.node is holder or holder in t.classes]

    def find(self, classes: list[str], name: str) -> Optional[TestFn]:
        found = None
        for t in self.tests:  # the last definition wins, as at runtime
            if t.node.name == name and [c.name for c in t.classes] == classes:
                found = t
        return found


# --------------------------------------------------------------------------- #
# Rendering (the way black formats it)                                        #
# --------------------------------------------------------------------------- #


def _quote(value: str) -> str:
    if '"' in value and "'" not in value:
        return "'" + value.replace("\\", "\\\\") + "'"
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _bracketed(head: str, items: list[str], tail: str, indent: str, line_length: int) -> list[str]:
    """``head + items + tail`` as black lays it out: one line, the items on one
    indented line, or one item per line with a magic trailing comma."""
    one = f"{indent}{head}{', '.join(items)}{tail}"
    if len(one) <= line_length or not items:
        return [one]
    inner = indent + "    "
    body = inner + ", ".join(items)
    if len(body) <= line_length and all("\n" not in i for i in items):
        return [f"{indent}{head}", body, f"{indent}{tail}"]
    return [f"{indent}{head}", *(f"{inner}{i}," for i in items), f"{indent}{tail}"]


def _call_text(text: str, decl: Decl, ids: list[str], level: str | None, artifact: dict[str, str] | None) -> str:
    """The declaration's call with ``ids`` and, when given, a new level/artifact."""
    return decl.callee + "(" + ", ".join(_call_args(text, decl, ids, level, artifact)) + ")"


def _call_args(text: str, decl: Decl, ids: list[str], level: str | None, artifact: dict[str, str] | None) -> list[str]:
    args = [_quote(i) for i in ids]
    for kw in decl.call.keywords:
        if kw.arg == "level" and level is not None:
            continue
        if kw.arg == "artifact" and artifact is not None:
            continue
        seg = ast.get_source_segment(text, kw)
        if seg is None or "\n" in seg:
            raise Unsupported(f"line {decl.call.lineno}: a multi-line keyword argument")
        args.append(seg)
    if level:
        args.append(f"level={_quote(level)}")
    if artifact:
        args.append("artifact={" + ", ".join(f"{_quote(k)}: {_quote(v)}" for k, v in artifact.items()) + "}")
    return args


# --------------------------------------------------------------------------- #
# Line edits                                                                  #
# --------------------------------------------------------------------------- #


class _Edits:
    """Whole-line edits on a source file: replace, delete, insert-before."""

    def __init__(self, lines: list[str]) -> None:
        self.lines = lines
        self.replace: dict[int, list[str]] = {}  # first line (0-based) -> new lines
        self.consumed: set[int] = set()  # lines covered by a replacement
        self.deleted: set[int] = set()
        self.insert: dict[int, list[str]] = {}

    def _span(self, first: int, last: int) -> None:
        for i in range(first, last + 1):
            if i in self.consumed or i in self.deleted or i in self.replace:
                raise Unsupported(f"line {i + 1}: overlapping edits")

    def put(self, first: int, last: int, new: list[str]) -> None:
        self._span(first, last)
        self.replace[first] = new
        self.consumed.update(range(first + 1, last + 1))

    def drop(self, first: int, last: int) -> None:
        self._span(first, last)
        self.deleted.update(range(first, last + 1))

    def add_before(self, line: int, new: list[str]) -> None:
        self.insert.setdefault(line, []).extend(new)

    def render(self) -> str:
        n = len(self.lines)
        deleted = set(self.deleted)
        # Deleting a statement must not pile up blank lines: two runs of
        # blanks that meet become the longer of the two.
        i = 0
        while i < n:
            if i in deleted and i not in self.insert:
                j = i
                while j + 1 < n and j + 1 in deleted and j + 1 not in self.insert:
                    j += 1
                above = _blank_run(self.lines, deleted, i - 1, -1)
                below = _blank_run(self.lines, deleted, j + 1, 1)
                if above and below:
                    deleted.update(above[: min(len(above), len(below))])
                i = j + 1
            else:
                i += 1
        out: list[str] = []
        for i, line in enumerate(self.lines):
            out.extend(s + "\n" for s in self.insert.get(i, []))
            if i in self.replace:
                out.extend(s + "\n" for s in self.replace[i])
            elif i not in deleted and i not in self.consumed:
                out.append(line)
        out.extend(s + "\n" for s in self.insert.get(len(self.lines), []))
        return "".join(out)


def _blank_run(lines: list[str], deleted: set[int], start: int, step: int) -> list[int]:
    run = []
    i = start
    while 0 <= i < len(lines) and (i in deleted or not lines[i].strip()):
        if i not in deleted:
            run.append(i)
        i += step
    return run


def _indent(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


# --------------------------------------------------------------------------- #
# The rewrite                                                                 #
# --------------------------------------------------------------------------- #


def rewrite(tf: TestFile, owners: Mapping[str, Optional[str]], line_length: int = 88) -> tuple[str, list[str]]:
    """Rewrite ``tf`` so each test in ``owners`` (qualname -> one id, or None
    for none) declares exactly that id; tests not in ``owners`` keep their
    trace. Returns ``(new text, change descriptions)``; raises
    :class:`Unsupported` when that cannot be done safely.
    """
    if tf.problems:
        raise Unsupported("; ".join(tf.problems))
    before = {t.qualname: tf.trace(t) for t in tf.tests}
    want: dict[str, Trace] = {}
    for t in tf.tests:
        ids, level, artifact = before[t.qualname]
        if t.qualname in owners:
            owner = owners[t.qualname]
            want[t.qualname] = ((owner,), level, artifact) if owner else ((), "", ())
        elif len(ids) > 1:
            raise Unsupported(f"{t.qualname}: names {', '.join(ids)} and has no decided owner")
        else:
            want[t.qualname] = before[t.qualname]

    def owner_of(t: TestFn) -> Optional[str]:
        ids = want[t.qualname][0]
        return ids[0] if ids else None

    # 1. Scope declarations (module, class): narrow to the one id all their
    #    tests went to, or remove.
    new: dict[int, Optional[Decl]] = {}  # id(decl) -> its replacement (None: removed)
    for d in tf.decls:
        if d.scope == "function" or not d.ids:
            continue
        tests = tf.tests_under(d.holder)
        if not tests:
            continue
        owners_here = {owner_of(t) for t in tests}
        only = owners_here.pop() if len(owners_here) == 1 else None
        if only is not None:
            if d.ids != [only]:
                new[id(d)] = _with(d, ids=[only])
        else:
            new[id(d)] = None

    def current(ds: Iterable[Decl]) -> list[Decl]:
        out = []
        for d in ds:
            if id(d) in new:
                repl = new[id(d)]
                if repl is not None:
                    out.append(repl)
            else:
                out.append(d)
        return out

    # 2. Each test's own declarations: keep, narrow, or replace them with one.
    added: dict[int, Decl] = {}  # id(test node) -> a new decorator
    changes = []
    for t in tf.tests:
        target = want[t.qualname]
        own = [d for d in tf.decls if d.holder is t.node]
        rest = [d for d in tf.decls if d.holder is not t.node]
        if not _matches(tf.trace(t, current(rest) + current(own)), target):
            for d in own:
                if not target[0]:
                    new[id(d)] = None
                elif d.ids and d.ids != list(target[0]):
                    new[id(d)] = _with(d, ids=list(target[0]))
            if not _matches(tf.trace(t, current(rest) + current(own)), target):
                for d in own:
                    new[id(d)] = None
                if target[0]:
                    inherited = tf.trace(t, current(rest))
                    model = _template(t, own, tf.chain(t))
                    added[id(t.node)] = _new_decl(model, t, target, inherited)
        if before[t.qualname] != target:
            was = ", ".join(before[t.qualname][0]) or "-"
            changes.append(f"{t.qualname}: {was} -> {', '.join(target[0]) or 'none'}")
    if not changes:
        return tf.text, []

    text = _apply(tf, new, added, line_length)
    text = _drop_unused_imports(text, _names(tf.tree))
    check = TestFile(text, tf.path)
    for t in check.tests:
        if t.qualname in want and not _matches(check.trace(t), want[t.qualname]):
            raise Unsupported(f"{t.qualname}: the rewrite would not give {want[t.qualname]} (internal check)")
    if {t.qualname for t in check.tests} != {t.qualname for t in tf.tests}:
        raise Unsupported("the rewrite would change the set of tests (internal check)")
    return text, changes


def _matches(trace: Trace, target: Trace) -> bool:
    """A test meant to verify nothing only needs to lose its ids (a level-only
    declaration may still apply to it); any other must match exactly."""
    return not trace[0] if not target[0] else trace == target


def _with(d: Decl, ids: list[str]) -> Decl:
    return Decl(d.scope, d.holder, d.call, d.callee, ids, d.level, d.artifact, d.site, d.stmt)


def _template(t: TestFn, own: list[Decl], chain: list[list[Decl]]) -> Decl:
    """The declaration whose spelling a new per-test declaration copies."""
    for d in own + [d for scope in chain[1:] for d in scope]:
        if d.ids:
            return d
    raise Unsupported(f"{t.qualname}: nothing to copy a declaration from")


def _new_decl(model: Decl, t: TestFn, target: Trace, inherited: Trace) -> Decl:
    ids, level, artifact = target
    want_art = dict(artifact)
    have_art = dict(inherited[2])
    art = {k: v for k, v in want_art.items() if have_art.get(k) != v}
    d = Decl("function", t.node, model.call, model.callee, list(ids), "", art, "decorator")
    d.level = level if level and inherited[1] != level else ""
    return d


def _apply(tf: TestFile, new: dict[int, Optional[Decl]], added: dict[int, Decl], line_length: int) -> str:
    text, lines = tf.text, tf.lines
    ed = _Edits(lines)
    by_stmt: dict[int, list[Decl]] = {}
    for d in tf.decls:
        if d.site == "pytestmark" and d.stmt is not None:
            by_stmt.setdefault(id(d.stmt), []).append(d)
    done: set[int] = set()
    for d in tf.decls:
        if id(d) not in new:
            continue
        repl = new[id(d)]
        if d.site == "decorator":
            first, last = d.call.lineno - 1, (d.call.end_lineno or d.call.lineno) - 1
            head = lines[first][: _col(lines[first], d.call.col_offset)]
            tail = lines[last][_col(lines[last], d.call.end_col_offset or 0) :].rstrip("\n")
            if head.strip() != "@" or (tail.strip() and not tail.strip().startswith("#")):
                raise Unsupported(f"line {first + 1}: a decorator that does not stand on its own lines")
            if repl is None:
                ed.drop(first, last)
            else:
                rendered = _render_decl(text, d, repl, _indent(lines[first]), "@", line_length)
                if tail.strip():
                    rendered[-1] += tail
                ed.put(first, last, rendered)
        elif d.stmt is not None and id(d.stmt) not in done:
            done.add(id(d.stmt))
            _rewrite_pytestmark(tf, ed, d.stmt, by_stmt[id(d.stmt)], new, line_length)
    for t in tf.tests:
        if id(t.node) in added:
            d = added[id(t.node)]
            first = (t.node.decorator_list[0].lineno - 1) if t.node.decorator_list else t.node.lineno - 1
            # A decorator expression starts after its "@" on the same line.
            indent = _indent(lines[first])
            ed.add_before(first, _render_decl(text, d, d, indent, "@", line_length, fresh=True))
    return ed.render()


def _render_decl(
    text: str, old: Decl, repl: Decl, indent: str, prefix: str, line_length: int, fresh: bool = False
) -> list[str]:
    level = repl.level if fresh else None
    artifact = repl.artifact if fresh else None
    if fresh:
        args = [_quote(i) for i in repl.ids]
        if level:
            args.append(f"level={_quote(level)}")
        if artifact:
            args.append("artifact={" + ", ".join(f"{_quote(k)}: {_quote(v)}" for k, v in artifact.items()) + "}")
    else:
        args = _call_args(text, old, repl.ids, level, artifact)
    return _bracketed(f"{prefix}{old.callee}(", args, ")", indent, line_length)


def _rewrite_pytestmark(
    tf: TestFile, ed: _Edits, stmt: ast.AST, decls: list[Decl], new: dict[int, Optional[Decl]], line_length: int
) -> None:
    assert isinstance(stmt, ast.Assign)
    lines, text = tf.lines, tf.text
    first, last = stmt.lineno - 1, (stmt.end_lineno or stmt.lineno) - 1
    head = lines[first][: _col(lines[first], stmt.col_offset)]
    tail = lines[last][_col(lines[last], stmt.end_col_offset or 0) :].rstrip("\n")
    if head.strip() or (tail.strip() and not tail.strip().startswith("#")):
        raise Unsupported(f"line {first + 1}: a pytestmark that shares its line with other statements")
    value = stmt.value
    is_seq = isinstance(value, (ast.List, ast.Tuple))
    elements = value.elts if isinstance(value, (ast.List, ast.Tuple)) else [value]
    mine = {id(d.call): d for d in decls}
    items: list[str] = []
    for el in elements:
        d = mine.get(id(el))
        if d is not None and id(d) in new:
            repl = new[id(d)]
            if repl is not None:
                items.append(_call_text(text, d, repl.ids, None, None))
        else:
            seg = ast.get_source_segment(text, el)
            if seg is None or "\n" in seg:
                raise Unsupported(f"line {first + 1}: a multi-line pytestmark element")
            items.append(seg)
    if not items:
        # Remove the statement and the comment block directly above it.
        top = first
        while top > 0 and lines[top - 1].strip().startswith("#") and _indent(lines[top - 1]) == head:
            top -= 1
        ed.drop(top, last)
        return
    indent = head
    if is_seq:
        opening, closing = ("[", "]") if isinstance(value, ast.List) else ("(", ")")
        if not isinstance(value, ast.List) and len(items) == 1:
            items[0] += ","
        rendered = _bracketed(f"pytestmark = {opening}", items, closing, indent, line_length)
    else:
        rendered = [f"{indent}pytestmark = {items[0]}"]
        if len(rendered[0]) > line_length:
            d = decls[0]
            repl = new.get(id(d)) or d
            rendered = _render_decl(text, d, repl, indent, "pytestmark = ", line_length)
    if tail.strip():
        rendered[-1] += tail
    ed.put(first, last, rendered)


def _col(line: str, byte_offset: int) -> int:
    """ast column offsets count UTF-8 bytes; turn one into a str index."""
    return len(line.encode("utf-8")[:byte_offset].decode("utf-8", errors="ignore"))


def _names(tree: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}


def _drop_unused_imports(text: str, used_before: set[str]) -> str:
    """Remove ``import pytest`` / ``from rules_requirements import rr`` when the
    rewrite took away their last use."""
    tree = ast.parse(text)
    used = _names(tree)
    lines = text.splitlines(keepends=True)
    ed = _Edits(lines)
    changed = False
    for stmt in tree.body:
        names: list[str] = []
        if isinstance(stmt, ast.Import) and [a.name for a in stmt.names] == ["pytest"]:
            names = [stmt.names[0].asname or "pytest"]
        elif (
            isinstance(stmt, ast.ImportFrom)
            and stmt.module == "rules_requirements"
            and [a.name for a in stmt.names] == ["rr"]
        ):
            names = [stmt.names[0].asname or "rr"]
        if names and set(names) <= used_before and not set(names) & used and stmt.lineno == stmt.end_lineno:
            line = lines[stmt.lineno - 1]
            if line.strip().startswith(("import", "from")) and ";" not in line and "#" not in line:
                ed.drop(stmt.lineno - 1, stmt.lineno - 1)
                changed = True
    return ed.render() if changed else text


# --------------------------------------------------------------------------- #
# Applying a worksheet                                                        #
# --------------------------------------------------------------------------- #


def python_files(root: str, only: Iterable[str] = ()) -> list[str]:
    """Candidate test sources under ``root`` (relative paths), optionally only below ``only`` prefixes."""
    prefixes = [p.strip("/") for p in only if p.strip("/")]
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root).replace(os.sep, "/")
        rel_dir = "" if rel_dir == "." else rel_dir
        dirnames[:] = sorted(
            d for d in dirnames if not d.startswith((".", "bazel-")) and d not in _SKIP_DIRS and "venv" not in d
        )
        for name in sorted(filenames):
            if not name.endswith(".py"):
                continue
            rel = f"{rel_dir}/{name}" if rel_dir else name
            if prefixes and not any(rel == p or rel.startswith(p + "/") for p in prefixes):
                continue
            out.append(rel)
    return out


def _module_parts(rel: str) -> list[str]:
    parts = rel[: -len(".py")].split("/")
    if parts[-1] == "__init__":
        parts.pop()
    return parts


def _split_case(path: str) -> Optional[tuple[list[str], str]]:
    """``pkg.mod.Class::test_x[param]`` -> (["pkg", "mod", "Class"], "test_x")."""
    classname, sep, name = path.partition("::")
    if not sep or not classname:
        return None
    base = name.split("[", 1)[0].split(" (", 1)[0].strip()
    return classname.split("."), base


def apply_tags(
    decided: Mapping[CaseKey, str],
    root: str,
    *,
    only: Iterable[str] = (),
    unassigned: str = "refuse",
    line_length: int = 88,
) -> ApplyResult:
    """Rewrite the Python tests under ``root`` per the worksheet's decisions
    (``CaseKey -> id | "none" | "?"``). Nothing is written; the caller writes
    each changed file's ``new_text``."""
    result = ApplyResult()
    files: dict[str, TestFile] = {}
    unreadable: dict[str, str] = {}
    for rel in python_files(root, only):
        full = os.path.join(root, rel)
        try:
            with open(full, encoding="utf-8") as fh:
                text = fh.read()
        except (OSError, UnicodeDecodeError) as exc:
            unreadable[rel] = str(exc)
            continue
        if "mark" not in text and "verifies" not in text:
            continue
        try:
            files[rel] = TestFile(text, rel)
        except SyntaxError as exc:
            unreadable[rel] = f"syntax error: {exc}"

    # Map each decided case to (file, test).
    targets: dict[str, dict[str, set[str]]] = {}  # file -> qualname -> decisions
    keys: dict[tuple[str, str], list[CaseKey]] = {}  # (file, qualname) -> its cases
    for key, decision in sorted(decided.items()):
        split = _split_case(key.path)
        if split is None:
            result.unresolved.append((key, "not a Python test case path"))
            continue
        parts, base = split
        best: list[tuple[str, TestFn]] = []
        best_len = 0
        for rel, tf in files.items():
            mod = _module_parts(rel)
            for k in range(len(mod)):
                suffix = mod[k:]
                if parts[: len(suffix)] != suffix:
                    continue
                test = tf.find(parts[len(suffix) :], base)
                if test is not None:
                    if len(suffix) > best_len:
                        best, best_len = [(rel, test)], len(suffix)
                    elif len(suffix) == best_len:
                        best.append((rel, test))
                break
        if not best:
            result.unresolved.append((key, "no Python test found (another language, or outside the scanned files)"))
            continue
        if len(best) > 1:
            where = ", ".join(rel for rel, _ in best)
            result.unresolved.append((key, f"ambiguous: matches tests in {where}"))
            continue
        rel, test = best[0]
        targets.setdefault(rel, {}).setdefault(test.qualname, set()).add(decision)
        keys.setdefault((rel, test.qualname), []).append(key)

    for rel in sorted(set(files) | set(unreadable)):
        if rel in unreadable:
            if rel in targets:
                result.files.append(FileResult(rel, "refused", [unreadable[rel]]))
            continue
        tf = files[rel]
        planned = targets.get(rel, {})
        try:
            traces = {t.qualname: tf.trace(t) for t in tf.tests}
        except Unsupported as exc:
            if planned:
                result.files.append(FileResult(rel, "refused", [str(exc)]))
            continue
        multi = [q for q, tr in traces.items() if len(tr[0]) > 1]
        if not multi and not planned:
            continue
        owners: dict[str, Optional[str]] = {}
        reasons = []
        for t in tf.tests:
            q = t.qualname
            ids = traces[q][0]
            got = planned.get(q, set())
            if not ids and got:
                # Decided, but carries no tag: its ownership is a model edit.
                result.untagged.extend(keys.get((rel, q), []))
                continue
            if len(ids) < 2 and got <= {*ids, "?"}:
                continue  # one tag at most, and no decision that changes it
            if len(got) > 1:
                reasons.append(
                    f"{q}: its cases were decided differently ({', '.join(sorted(got))}); "
                    "split it by hand with pytest.param(..., marks=...)"
                )
                continue
            decision = next(iter(got)) if got else "?"
            if decision == "?":
                if unassigned == "drop":
                    owners[q] = None
                else:
                    reasons.append(f"{q}: names {', '.join(ids)} and its owner is undecided")
                continue
            owners[q] = None if decision == "none" else decision
        if reasons:
            result.files.append(FileResult(rel, "refused", reasons))
            continue
        if not owners:
            continue
        try:
            new_text, changes = rewrite(tf, owners, line_length=line_length)
        except Unsupported as exc:
            result.files.append(FileResult(rel, "refused", [str(exc)]))
            continue
        status = "changed" if new_text != tf.text else "unchanged"
        result.files.append(FileResult(rel, status, [], changes, tf.text, new_text))
    result.untagged = sorted(set(result.untagged))
    return result
