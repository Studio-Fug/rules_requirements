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
with ``pytest.param(..., marks=...)``), when a declaration is not a literal
the codemod can read, or when a change would reach a test the static view
cannot see: tests a class inherits from another class (of the file or of
another module), tests defined inside an ``if``/``try``/``with``/loop block,
tests bound by an assignment or an import, and decided cases of the module
the file does not define (inherited from another module, or generated). The
internal check can only vouch for the tests it sees, so those are refused up
front.

Across files, every Python file under the root is indexed for what it imports
and subclasses: a file whose changed classes or tests another file imports or
subclasses is refused, together with that file. Last, each decided case's
attribution is derived again from the rewritten sources and compared with the
worksheet. The caller writes all or nothing (:meth:`ApplyResult.to_write`).
"""

from __future__ import annotations

import ast
import io
import os
import tokenize
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Optional, Union

from rules_requirements.case_keys import CaseKey
from rules_requirements.util import dedupe

MARKER_NAMES = ("rr", "requirements")
_SKIP_DIRS = {"node_modules", "__pycache__", "site-packages", "_vendor"}
_FuncDef = Union[ast.FunctionDef, ast.AsyncFunctionDef]
# Statements whose bodies pytest still collects tests from, but the codemod does not follow.
_COMPOUND = tuple(
    getattr(ast, name)
    for name in ("If", "For", "AsyncFor", "While", "Try", "TryStar", "With", "AsyncWith", "Match")
    if hasattr(ast, name)
)

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

    @property
    def is_verifies(self) -> bool:
        """``@rr.verifies``: an attribute, not a pytest marker. On a class it
        applies to that class's own tests only, never to a nested class's."""
        return _dotted(self.call.func).rpartition(".")[2] not in MARKER_NAMES


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
    unresolved: list[tuple[CaseKey, str]] = field(default_factory=list)  # no Python test to rewrite
    # A Python module of the case was scanned, but it does not define the test:
    # the codemod cannot vouch for it (it leaves the scopes around it alone).
    unmatched: list[tuple[CaseKey, str]] = field(default_factory=list)
    untagged: list[CaseKey] = field(default_factory=list)  # decided, but the test declares no id
    outside: list[CaseKey] = field(default_factory=list)  # with ``only``: no scanned module
    # Decided cases whose attribution, re-derived from the rewritten sources,
    # differs from the worksheet: nothing may be written.
    mismatched: list[tuple[CaseKey, str]] = field(default_factory=list)
    unmatched_files: set[str] = field(default_factory=set)  # the files holding the unmatched cases
    # Files that import (or subclass) tests or test classes of each other, both ways.
    depends: dict[str, set[str]] = field(default_factory=dict)

    @property
    def refused(self) -> list[FileResult]:
        return [f for f in self.files if f.status == "refused"]

    @property
    def changed(self) -> list[FileResult]:
        """The files rewritten in memory; :meth:`to_write` says which may be written."""
        return [f for f in self.files if f.status == "changed"]

    @property
    def blocked(self) -> bool:
        """Something was refused, unmatched or mismatched: the run is not clean."""
        return bool(self.refused or self.unmatched or self.mismatched)

    def to_write(self, partial: bool = False) -> list[FileResult]:
        """The changed files to write. All or nothing by default: nothing
        when anything was refused or unmatched. With ``partial``, the changed
        files that neither import from nor are imported by a refused file (or
        one holding an unmatched case). Never anything when a rewritten
        source does not give a decided case its owner."""
        if self.mismatched:
            return []
        if not self.blocked:
            return self.changed
        if not partial:
            return []
        bad = {f.path for f in self.refused} | self.unmatched_files
        return [f for f in self.changed if f.path not in bad and not self.depends.get(f.path, set()) & bad]


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
        self.classes: dict[int, tuple[ast.ClassDef, ...]] = {}  # id(class) -> its chain, outermost first
        # Where the static view is incomplete. ``scope_blind``: holders (module,
        # class, test function) whose declarations also reach tests the codemod
        # cannot see, so changing them is refused. ``trace_blind``: classes whose
        # tests' static traces may miss declarations (inherited ones).
        self.scope_blind: dict[int, str] = {}
        self.trace_blind: dict[int, str] = {}
        self._hidden_calls: set[int] = set()
        self._collect(self.tree, "module", ())
        self._inheritance()
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
        defs = {s.name: s for s in body if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef))}
        for stmt in body:
            if isinstance(stmt, ast.Assign) and [_dotted(t) for t in stmt.targets] == ["pytestmark"]:
                values = stmt.value.elts if isinstance(stmt.value, (ast.List, ast.Tuple)) else [stmt.value]
                for v in values:
                    if self.is_decl(v):
                        self._decl(scope, holder, v, "pytestmark", stmt)  # type: ignore[arg-type]
            elif isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.ImportFrom)):
                self._binding(stmt, defs, classes)
            elif isinstance(stmt, ast.ClassDef):
                self.classes[id(stmt)] = classes + (stmt,)
                self._decorators(stmt, "class")
                self._collect(stmt, "class", classes + (stmt,))
            elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._decorators(stmt, "function")
                if stmt.name.startswith("test"):
                    self.tests.append(TestFn(stmt, classes))
            elif isinstance(stmt, _COMPOUND):
                self._hidden(stmt, classes)

    def _hidden(self, stmt: ast.stmt, classes: tuple[ast.ClassDef, ...]) -> None:
        """Tests (and test classes) defined inside an ``if``/``try``/``with``/
        loop block: pytest collects them, the codemod does not follow them. The
        declarations of every scope around them must not change."""
        kind = type(stmt).__name__.lower().replace("async", "")
        todo: list[ast.AST] = [stmt]
        while todo:
            node = todo.pop()
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    self._hidden_calls.update(id(d) for d in child.decorator_list)
                    if isinstance(child, ast.ClassDef) or child.name.startswith("test"):
                        why = (
                            f"line {child.lineno}: {child.name} is defined inside a {kind} block, where the codemod "
                            "cannot follow it; migrate this file by hand"
                        )
                        for holder in (self.tree, *classes):
                            self.scope_blind.setdefault(id(holder), why)
                    if isinstance(child, ast.ClassDef):
                        self._hidden_calls.update(id(n) for n in ast.walk(child) if isinstance(n, ast.Call))
                elif not isinstance(child, ast.Lambda):
                    todo.append(child)

    def _binding(self, stmt: ast.stmt, defs: Mapping[str, ast.AST], classes: tuple[ast.ClassDef, ...]) -> None:
        """A test bound by an assignment or an import (``test_b = test_a``,
        ``from helpers import test_shared``, a class attribute): pytest
        collects it, the codemod does not see it. The declarations of every
        scope around it must not change, nor those of a function it aliases."""
        if isinstance(stmt, ast.ImportFrom):
            names = [a.asname or a.name for a in stmt.names]
            how = "an import"
        else:
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]  # type: ignore[attr-defined]
            names = [
                n.id for t in targets for n in ast.walk(t) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)
            ]
            how = "an assignment"
        for name in names:
            if not name.startswith(("test", "Test", "*")):
                continue
            what = "a star import, which may bring tests" if name == "*" else f"{name} is bound by {how}, not a def"
            why = (
                f"line {stmt.lineno}: {what}: pytest collects it as a test the codemod cannot see; "
                "migrate this file by hand"
            )
            for holder in (self.tree, *classes):
                self.scope_blind.setdefault(id(holder), why)
            value = getattr(stmt, "value", None)
            for n in ast.walk(value) if value is not None else ():
                if isinstance(n, ast.Name) and n.id in defs:
                    self.scope_blind.setdefault(id(defs[n.id]), why)

    def mark_inherited(self, sub: ast.ClassDef, why: str) -> None:
        """``sub`` inherits tests or declarations the static view does not
        follow: distrust its traces, and change nothing that reaches its tests."""
        self.trace_blind.setdefault(id(sub), why)
        for holder in (self.tree, *self.classes[id(sub)], *(t.node for t in self.tests_under(sub))):
            self.scope_blind.setdefault(id(holder), why)

    def _inheritance(self) -> None:
        """A test class subclassing another class of this file inherits its
        tests and (pytest >= 7.2) its markers: refuse to change anything they
        share, and distrust the subclass's static traces."""
        by_name: dict[str, ast.ClassDef] = {}
        for chain in self.classes.values():
            by_name.setdefault(".".join(c.name for c in chain), chain[-1])
        bases: dict[int, list[ast.ClassDef]] = {
            cid: [b for b in (by_name.get(_dotted(e)) for e in chain[-1].bases) if b is not None]
            for cid, chain in self.classes.items()
        }
        for cid, chain in self.classes.items():
            sub = chain[-1]
            ancestors: list[ast.ClassDef] = []
            todo = list(bases[cid])
            while todo:
                b = todo.pop()
                if all(b is not a for a in ancestors) and b is not sub:
                    ancestors.append(b)
                    todo.extend(bases[id(b)])
            if not any(self.tests_under(a) or any(d.holder is a for d in self.decls) for a in ancestors):
                continue
            why = (
                f"line {sub.lineno}: class {sub.name} inherits tests or declarations from "
                f"{', '.join(a.name for a in ancestors)} in this file; the codemod does not follow inheritance, "
                "migrate this file by hand"
            )
            self.trace_blind.setdefault(id(sub), why)
            for c in (sub, *ancestors):
                for holder in (self.tree, *self.classes[id(c)], *(t.node for t in self.tests_under(c))):
                    self.scope_blind.setdefault(id(holder), why)

    def blind_class(self, test: TestFn) -> Optional[str]:
        """Why ``test``'s static trace cannot be trusted, if it cannot."""
        for c in test.classes:
            if id(c) in self.trace_blind:
                return self.trace_blind[id(c)]
        return None

    def mark_unseen(self, classes: list[str], why: str) -> None:
        """A decided case of this module whose test is not defined here
        (inherited from another module, or generated): the declarations of
        the scopes it sits in reach it unseen, and a class of that path may
        inherit declarations too."""
        found = self.resolve_classes(classes)
        for holder in (self.tree, *found):
            self.scope_blind.setdefault(id(holder), why)
        if found and len(found) == len(classes):
            self.trace_blind.setdefault(id(found[-1]), why)

    def resolve_classes(self, names: list[str]) -> list[ast.ClassDef]:
        """The classes of this file along a ``Outer.Inner`` path, as far as they exist."""
        out: list[ast.ClassDef] = []
        holder: ast.AST = self.tree
        for name in names:
            nxt = None
            for stmt in getattr(holder, "body", []):
                if isinstance(stmt, ast.ClassDef) and stmt.name == name:
                    nxt = stmt
            if nxt is None:
                break
            out.append(nxt)
            holder = nxt
        return out

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
                if id(node) in self._hidden_calls:
                    self.problems.append(
                        f"line {node.lineno}: a declaration inside an if/try/with/loop block, where the codemod "
                        "cannot follow it"
                    )
                else:
                    self.problems.append(
                        f"line {node.lineno}: a declaration outside a decorator or pytestmark (e.g. pytest.param marks)"
                    )

    # -- semantics -------------------------------------------------------- #

    @staticmethod
    def applies(d: Decl, test: TestFn) -> bool:
        """Whether ``d`` reaches ``test`` as the pytest plugin resolves it:
        markers reach every test below their scope; ``@rr.verifies`` on a
        class only the tests of that class itself (``item.cls``)."""
        if d.holder is test.node:
            return True
        if d.scope == "module":
            return True
        if d.is_verifies and d.scope == "class":
            return bool(test.classes) and test.classes[-1] is d.holder
        return any(c is d.holder for c in test.classes)

    def chain(self, test: TestFn, decls: Iterable[Decl] | None = None) -> list[list[Decl]]:
        """The declarations applying to ``test``, nearest scope first."""
        pool = [d for d in (self.decls if decls is None else decls) if self.applies(d, test)]
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
        return [t for t in self.tests if t.node is holder or any(c is holder for c in t.classes)]

    def reached_by(self, d: Decl) -> list[TestFn]:
        """The tests ``d`` applies to."""
        return [t for t in self.tests_under(d.holder) if self.applies(d, t)]

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


@dataclass
class _Br:
    """A bracketed expression black may split: ``head`` + items + ``closing``.

    ``collection`` (a list/tuple/set/dict literal): black explodes its body,
    one item per line, rather than keeping it on one indented line as it
    does for call arguments. ``magic``: the source ends it with a trailing
    comma, which keeps black from joining it. ``one_tuple``: ``(x,)``.
    """

    head: str
    items: list[Union[str, _Br]]
    closing: str
    collection: bool = False
    magic: bool = False
    one_tuple: bool = False

    def flat(self) -> str:
        body = ", ".join(_flat(i) for i in self.items)
        return f"{self.head}{body}{',' if self.one_tuple else ''}{self.closing}"

    def forced(self) -> bool:
        """Black cannot join it onto one line (a magic trailing comma, here or inside)."""
        return self.magic or any(isinstance(i, _Br) and i.forced() for i in self.items)


_Part = Union[str, _Br]


def _flat(part: _Part) -> str:
    return part if isinstance(part, str) else part.flat()


def _layout(part: _Part, indent: str, tail: str, line_length: int) -> list[str]:
    """``part`` as black lays it out at ``indent``, followed by ``tail``: on
    one line if it fits; else split at its brackets — a call's arguments on
    one indented line when they fit there, otherwise (and always for a
    collection) one per line with a trailing comma. A lone item gets no comma
    (black adds none where there is no delimiter); each line still too long
    is split the same way."""
    one = f"{indent}{_flat(part)}{tail}"
    if isinstance(part, str) or not part.items or (len(one) <= line_length and not part.forced()):
        return [one]
    inner = indent + "    "
    if not part.collection and not part.magic and not any(isinstance(i, _Br) and i.forced() for i in part.items):
        body = inner + ", ".join(_flat(i) for i in part.items)
        if len(body) <= line_length:
            return [f"{indent}{part.head}", body, f"{indent}{part.closing}{tail}"]
    if len(part.items) == 1 and not part.magic and not part.one_tuple:
        middle = _layout(part.items[0], inner, "", line_length)
    else:
        middle = [line for item in part.items for line in _layout(item, inner, ",", line_length)]
    return [f"{indent}{part.head}", *middle, f"{indent}{part.closing}{tail}"]


def _span(node: ast.AST) -> tuple[int, int, int, int]:
    """``(line, col, end line, end col)`` of a node that has a position (1-based lines, byte columns)."""
    line = int(getattr(node, "lineno", 1))
    end_line = getattr(node, "end_lineno", None) or line
    return line, int(getattr(node, "col_offset", 0)), int(end_line), int(getattr(node, "end_col_offset", None) or 0)


def _trailing_comma(text: str, node: ast.AST, last: Optional[ast.AST]) -> bool:
    """Whether the source has a comma between ``last`` (the final child) and the end of ``node``."""
    if last is None:
        return False
    lines = text.splitlines(keepends=True)
    _, _, start_line, start_col = _span(last)
    _, _, end_line, end_col = _span(node)
    chunk = lines[start_line - 1 : end_line]
    if not chunk:
        return False
    if len(chunk) == 1:
        between = chunk[0][_col(chunk[0], start_col) : _col(chunk[0], end_col)]
    else:
        between = chunk[0][_col(chunk[0], start_col) :] + "".join(chunk[1:-1]) + chunk[-1][: _col(chunk[-1], end_col)]
    return "," in between  # (comments inside a re-rendered span are refused beforehand)


def _expr(text: str, node: ast.AST, line: int) -> _Part:
    """An expression as a :class:`_Br` tree black can split, from its source.

    Calls and collection literals are taken apart; anything else must sit on
    one line."""
    if isinstance(node, ast.Call):
        func = ast.get_source_segment(text, node.func)
        if func is None or "\n" in func:
            raise Unsupported(f"line {line}: a multi-line callee")
        children = sorted([*node.args, *node.keywords], key=lambda n: _span(n)[:2])
        args = [_keyword(text, c, line) if isinstance(c, ast.keyword) else _expr(text, c, line) for c in children]
        return _Br(func + "(", args, ")", magic=_trailing_comma(text, node, children[-1] if children else None))
    seg = ast.get_source_segment(text, node) or ""
    brackets = {ast.List: "[]", ast.Set: "{}", ast.Dict: "{}", ast.Tuple: "()"}
    pair = brackets.get(type(node))
    if pair and seg.startswith(pair[0]) and seg.endswith(pair[1]):
        if isinstance(node, ast.Dict):
            items: list[_Part] = []
            for k, v in zip(node.keys, node.values):
                value = _flat(_expr(text, v, line))
                items.append(f"**{value}" if k is None else f"{_flat(_expr(text, k, line))}: {value}")
            last: Optional[ast.AST] = node.values[-1] if node.values else None
        else:
            elts = node.elts  # type: ignore[attr-defined]
            items = [_expr(text, e, line) for e in elts]
            last = elts[-1] if elts else None
        one_tuple = isinstance(node, ast.Tuple) and len(items) == 1
        magic = not one_tuple and _trailing_comma(text, node, last)
        return _Br(pair[0], items, pair[1], collection=True, magic=magic, one_tuple=one_tuple)
    if "\n" in seg or not seg:
        raise Unsupported(f"line {line}: a multi-line expression the codemod cannot lay out")
    return seg


def _keyword(text: str, kw: ast.keyword, line: int) -> _Part:
    value = _expr(text, kw.value, line)
    prefix = "**" if kw.arg is None else f"{kw.arg}="
    if isinstance(value, str):
        return prefix + value
    return _Br(prefix + value.head, value.items, value.closing, value.collection, value.magic, value.one_tuple)


def _artifact_part(artifact: Mapping[str, str]) -> _Br:
    return _Br("artifact={", [f"{_quote(k)}: {_quote(v)}" for k, v in artifact.items()], "}", collection=True)


def _call_args(
    text: str, decl: Decl, ids: list[str], level: str | None, artifact: dict[str, str] | None
) -> list[_Part]:
    args: list[_Part] = [_quote(i) for i in ids]
    for kw in decl.call.keywords:
        if kw.arg == "level" and level is not None:
            continue
        if kw.arg == "artifact" and artifact is not None:
            continue
        args.append(_keyword(text, kw, decl.call.lineno))
    if level:
        args.append(f"level={_quote(level)}")
    if artifact:
        args.append(_artifact_part(artifact))
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
        tests = tf.reached_by(d)
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
    for t in tf.tests:
        why = tf.blind_class(t)
        if why and t.qualname in owners:
            raise Unsupported(why)
    for d in tf.decls:
        if id(d) in new and id(d.holder) in tf.scope_blind:
            raise Unsupported(tf.scope_blind[id(d.holder)])
    for node_id in added:
        if node_id in tf.scope_blind:
            raise Unsupported(tf.scope_blind[node_id])

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
            _no_comments_inside(tf, d.call)
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
    if fresh:
        args: list[_Part] = [_quote(i) for i in repl.ids]
        if repl.level:
            args.append(f"level={_quote(repl.level)}")
        if repl.artifact:
            args.append(_artifact_part(repl.artifact))
        magic = False
    else:
        args = _call_args(text, old, repl.ids, None, None)
        magic = _trailing_comma(text, old.call, _last_arg(old.call))
    return _layout(_Br(f"{prefix}{old.callee}(", args, ")", magic=magic), indent, "", line_length)


def _last_arg(call: ast.Call) -> Optional[ast.AST]:
    children = [*call.args, *call.keywords]
    return max(children, key=lambda n: _span(n)[2:]) if children else None


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
    _no_comments_inside(tf, stmt)
    value = stmt.value
    is_seq = isinstance(value, (ast.List, ast.Tuple))
    elements = value.elts if isinstance(value, (ast.List, ast.Tuple)) else [value]
    mine = {id(d.call): d for d in decls}
    items: list[_Part] = []
    for el in elements:
        d = mine.get(id(el))
        if d is not None and id(d) in new:
            repl = new[id(d)]
            if repl is not None:
                magic = _trailing_comma(text, d.call, _last_arg(d.call))
                items.append(_Br(f"{d.callee}(", _call_args(text, d, repl.ids, None, None), ")", magic=magic))
        else:
            items.append(_expr(text, el, first + 1))
    if not items:
        # Remove the statement and the comment block directly above it.
        top = first
        while top > 0 and lines[top - 1].strip().startswith("#") and _indent(lines[top - 1]) == head:
            top -= 1
        ed.drop(top, last)
        return
    indent = head
    if is_seq:
        assert isinstance(value, (ast.List, ast.Tuple))
        opening, closing = ("[", "]") if isinstance(value, ast.List) else ("(", ")")
        one_tuple = isinstance(value, ast.Tuple) and len(items) == 1
        magic = not one_tuple and _trailing_comma(text, value, value.elts[-1] if value.elts else None)
        whole = _Br(f"pytestmark = {opening}", items, closing, collection=True, magic=magic, one_tuple=one_tuple)
        rendered = _layout(whole, indent, "", line_length)
    else:
        (item,) = items
        if isinstance(item, _Br):
            item = _Br("pytestmark = " + item.head, item.items, item.closing, magic=item.magic)
            rendered = _layout(item, indent, "", line_length)
        else:
            rendered = [f"{indent}pytestmark = {item}"]
    if tail.strip():
        rendered[-1] += tail
    ed.put(first, last, rendered)


def _source_between(tf: TestFile, line: int, col: int, node: ast.AST) -> str:
    """The source from ``(line, col)`` up to the end of ``node``."""
    _, _, end_line, end_col = _span(node)
    lines = tf.lines[line - 1 : end_line]
    if len(lines) == 1:
        return lines[0][_col(lines[0], col) : _col(lines[0], end_col)]
    return lines[0][_col(lines[0], col) :] + "".join(lines[1:-1]) + lines[-1][: _col(lines[-1], end_col)]


def _no_comments_inside(tf: TestFile, node: ast.AST) -> None:
    """Refuse to re-render a statement or call that holds comments: they would be lost."""
    first, col, last, _ = _span(node)
    if first == last:
        return
    segment = _source_between(tf, first, col, node)
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(segment).readline))
    except (tokenize.TokenError, SyntaxError):
        return
    if any(tok.type == tokenize.COMMENT for tok in tokens):
        raise Unsupported(f"line {first}: a multi-line declaration with comments inside (they would be lost)")


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


def _read(path: str) -> str:
    """A source file's text, its line endings as they are (CRLF stays CRLF)."""
    with open(path, encoding="utf-8", newline="") as fh:
        return fh.read()


def _bound_names(stmt: ast.stmt) -> list[str]:
    """The names a module-level statement binds (looking into if/try/with
    blocks, not into function or class bodies)."""
    out: list[str] = []
    todo: list[ast.AST] = [stmt]
    while todo:
        node = todo.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.append(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            out.extend((a.asname or a.name).split(".")[0] for a in node.names if a.name != "*")
        elif not isinstance(node, ast.Lambda):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                out.append(node.id)
            todo.extend(ast.iter_child_nodes(node))
    return out


def _segments(text: str) -> dict[str, list[str]]:
    """Module-level name -> the source of the statements binding it (decorators included)."""
    tree = ast.parse(text)
    lines = text.splitlines(keepends=True)
    out: dict[str, list[str]] = {}
    for stmt in tree.body:
        first = min([stmt.lineno, *(d.lineno for d in getattr(stmt, "decorator_list", []))])
        seg = "".join(lines[first - 1 : stmt.end_lineno or stmt.lineno])
        for name in _bound_names(stmt):
            out.setdefault(name, []).append(seg)
    return out


def _changed_names(old: str, new: str) -> set[str]:
    """Module-level names whose statements a rewrite changed or removed."""
    a, b = _segments(old), _segments(new)
    return {n for n in set(a) | set(b) if a.get(n) != b.get(n)}


def _plain_class(stmt: Optional[ast.stmt]) -> bool:
    """A class whose subclasses inherit no tests and no declarations from it."""
    if not isinstance(stmt, ast.ClassDef) or stmt.decorator_list:
        return False
    if any(_dotted(b) != "object" for b in stmt.bases) or stmt.keywords:
        return False
    for node in ast.walk(stmt):
        if node is stmt:
            continue
        if isinstance(node, ast.ClassDef):
            return False
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            return False
        if isinstance(node, ast.ImportFrom) or (
            isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Store)
            and (node.id == "pytestmark" or node.id.startswith(("test", "Test")))
        ):
            return False
    return True


class _Index:
    """Every scanned Python file's imports and base classes, resolved to the
    other scanned files they name: who imports or subclasses what.

    ``from m import C [as D]``, ``import m [as n]`` + ``n.C`` and relative
    imports resolve by module path (a scanned file whose module path ends
    with the imported one, or the other way round); a base class named by
    nothing the file imports or defines falls back to every scanned file
    defining a module-level class of that name."""

    def __init__(self, texts: Mapping[str, str]) -> None:
        self.parts: dict[str, list[str]] = {}
        self.trees: dict[str, ast.Module] = {}
        for rel, text in texts.items():
            try:
                self.trees[rel] = ast.parse(text)
            except (SyntaxError, ValueError):
                continue
            self.parts[rel] = _module_parts(rel)
        self.by_last: dict[str, list[str]] = {}
        for rel, parts in self.parts.items():
            if parts:
                self.by_last.setdefault(parts[-1], []).append(rel)
        self.top: dict[str, dict[str, ast.stmt]] = {
            rel: {name: stmt for stmt in tree.body for name in _bound_names(stmt)} for rel, tree in self.trees.items()
        }
        self.refs: dict[str, dict[str, set[str]]] = {}  # importer -> imported file -> names used ("*": any)
        self.binds: dict[str, dict[str, list[tuple[str, str]]]] = {}  # importer -> local name -> (file, name)
        # file -> (line, class, base spelling, [(file, name)]) for bases in other scanned files
        self.bases: dict[str, list[tuple[int, str, str, list[tuple[str, str]]]]] = {}
        for rel in self.trees:
            self._scan(rel)
        self.importers: dict[str, dict[str, set[str]]] = {}  # imported file -> importer -> names
        for g, refs in self.refs.items():
            for f, names in refs.items():
                self.importers.setdefault(f, {})[g] = names

    def resolve(self, mod: list[str], rel: str, level: int) -> list[str]:
        if level:
            parts = self.parts.get(rel, [])
            pkg = parts if rel.endswith("__init__.py") else parts[:-1]
            if level - 1 > len(pkg):
                return []
            want = pkg[: len(pkg) - (level - 1)] + mod
            return [r for r in self.by_last.get(want[-1], []) if self.parts[r] == want] if want else []
        if not mod:
            return []
        out = []
        for r in self.by_last.get(mod[-1], []):
            p = self.parts[r]
            if p[-len(mod) :] == mod or mod[-len(p) :] == p:
                out.append(r)
        return out

    def _scan(self, rel: str) -> None:
        tree = self.trees[rel]
        refs: dict[str, set[str]] = {}
        binds: dict[str, list[tuple[str, str]]] = {}
        aliases: dict[str, list[str]] = {}  # local module spelling -> files

        def add(f: str, name: str) -> None:
            if f != rel:
                refs.setdefault(f, set()).add(name)

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    mod = a.name.split(".")
                    if a.asname:
                        aliases[a.asname] = self.resolve(mod, rel, 0)
                    else:
                        for i in range(1, len(mod) + 1):
                            aliases[".".join(mod[:i])] = self.resolve(mod[:i], rel, 0)
            elif isinstance(node, ast.ImportFrom):
                base = node.module.split(".") if node.module else []
                files = self.resolve(base, rel, node.level)
                for a in node.names:
                    if a.name == "*":
                        for f in files:
                            add(f, "*")
                        continue
                    for f in files:
                        add(f, a.name)
                    binds.setdefault(a.asname or a.name, []).extend((f, a.name) for f in files if f != rel)
                    sub = self.resolve(base + [a.name], rel, node.level)
                    if sub:
                        aliases[a.asname or a.name] = sub
        values = {id(n.value) for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                head, _, attr = _dotted(node).rpartition(".")
                for f in aliases.get(head, ()):
                    add(f, attr)
            elif isinstance(node, ast.Name) and node.id in aliases and id(node) not in values:
                for f in aliases[node.id]:
                    add(f, "*")  # the module itself is passed around

        local = {
            n.name if not isinstance(n, ast.Name) else n.id
            for n in ast.walk(tree)
            if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            or (isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store))
        }
        bases = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for b in node.bases:
                spelled = _dotted(b.value if isinstance(b, ast.Subscript) else b)
                targets = self._base(rel, spelled, binds, aliases, local) if spelled else []
                for f, name in targets:
                    add(f, name)
                if targets:
                    bases.append((node.lineno, node.name, spelled, targets))
        self.refs[rel] = refs
        self.binds[rel] = binds
        self.bases[rel] = bases

    def _base(
        self,
        rel: str,
        spelled: str,
        binds: Mapping[str, list[tuple[str, str]]],
        aliases: Mapping[str, list[str]],
        local: set[str],
    ) -> list[tuple[str, str]]:
        parts = spelled.split(".")
        if parts[0] in binds or parts[0] in aliases:
            out = list(binds.get(parts[0], ()))
            for i in range(len(parts) - 1, 0, -1):
                head = ".".join(parts[:i])
                if head in aliases:
                    out.extend((f, parts[i]) for f in aliases[head])
                    break
            return out
        if parts[0] in local:
            return []  # this file's own class: see TestFile._inheritance
        return [
            (f, parts[-1]) for f, top in self.top.items() if f != rel and isinstance(top.get(parts[-1]), ast.ClassDef)
        ]

    def inherited(self, rel: str) -> list[tuple[int, str, str]]:
        """``(line, class, why)`` for the classes of ``rel`` that inherit from
        a class of another scanned file with tests or declarations."""
        out = []
        for line, name, spelled, targets in self.bases.get(rel, []):
            where = sorted({f for f, n in targets if not _plain_class(self.top.get(f, {}).get(n))})
            if where:
                out.append(
                    (
                        line,
                        name,
                        f"line {line}: class {name} inherits from {spelled} ({', '.join(where)}), which has tests "
                        "or declarations; the codemod does not follow inheritance across modules, migrate this "
                        "file by hand",
                    )
                )
        return out

    def depends(self) -> dict[str, set[str]]:
        """Files that import tests or classes from each other (both ways)."""
        out: dict[str, set[str]] = {}
        for g, refs in self.refs.items():
            for f, names in refs.items():
                top = self.top.get(f, {})
                if any(
                    n == "*" or n not in top or isinstance(top[n], ast.ClassDef) or n.startswith(("test", "Test"))
                    for n in names
                ):
                    out.setdefault(g, set()).add(f)
                    out.setdefault(f, set()).add(g)
        return out


def _involve(result: ApplyResult, index: _Index, changed: Mapping[str, set[str]]) -> None:
    """Refuse every file whose changed module-level names another scanned
    file imports or subclasses, and that file too: pytest collects what it
    imports, and its subclasses inherit tests and markers, where the
    codemod's per-file view does not reach. Names a file re-exports
    propagate."""
    names = {f: set(n) for f, n in changed.items()}
    reasons: dict[str, list[str]] = {}
    work = sorted(names)
    seen: set[tuple[str, str]] = set()
    while work:
        f = work.pop()
        for g, used in sorted(index.importers.get(f, {}).items()):
            hit = sorted(names[f]) if "*" in used else sorted(used & names[f])
            if not hit or (g, f) in seen:
                continue
            seen.add((g, f))
            what = ", ".join(hit)
            reasons.setdefault(f, []).append(
                f"{g} imports or subclasses {what} from this file, whose declarations would change; pytest "
                "collects imported tests and subclasses inherit them, which the codemod does not follow "
                "across modules: migrate these files by hand"
            )
            reasons.setdefault(g, []).append(
                f"imports or subclasses {what} from {f}, whose declarations would change: the tests collected "
                "here would change unseen; migrate these files by hand"
            )
            again = {local for local, src in index.binds.get(g, {}).items() if any(s == f and n in hit for s, n in src)}
            if "*" in used:
                again |= set(hit)
            if again - names.get(g, set()):
                names.setdefault(g, set()).update(again)
                work.append(g)
    if not reasons:
        return
    by_path = {f.path: f for f in result.files}
    for path, why in reasons.items():
        fr = by_path.get(path)
        if fr is None:
            fr = by_path[path] = FileResult(path, "refused")
            result.files.append(fr)
        fr.status = "refused"
        fr.reasons.extend(r for r in why if r not in fr.reasons)
    result.files.sort(key=lambda f: f.path)


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
    the files :meth:`ApplyResult.to_write` returns.

    Every Python file under ``root`` (not only those below ``only``) is
    indexed for what it imports and subclasses: a file whose changed classes
    or tests another file imports or subclasses is refused, with that file.
    Each decided case's attribution is then re-derived from the rewritten
    sources and compared with the worksheet (``mismatched``)."""
    result = ApplyResult()
    only = list(only)
    scanned = python_files(root, only)
    files: dict[str, TestFile] = {}
    unreadable: dict[str, str] = {}
    newline: dict[str, str] = {}
    texts: dict[str, str] = {}

    def read(rel: str) -> Optional[str]:
        if rel not in texts and rel not in unreadable:
            try:
                texts[rel] = _read(os.path.join(root, rel))
            except (OSError, UnicodeDecodeError) as exc:
                unreadable[rel] = str(exc)
        return texts.get(rel)

    everything = python_files(root)
    index = _Index({rel: text for rel in everything for text in [read(rel)] if text is not None})
    result.depends = index.depends()

    def parse(rel: str, require_markers: bool) -> Optional[TestFile]:
        if rel in files:
            return files[rel]
        text = read(rel)
        if text is None:
            return None
        if require_markers and "mark" not in text and "verifies" not in text:
            return None
        if "\r\n" in text and text.count("\r\n") == text.count("\n"):
            # CRLF throughout: edit it as LF, write it back as CRLF.
            newline[rel] = "\r\n"
            text = text.replace("\r\n", "\n")
        try:
            tf = TestFile(text, rel)
        except SyntaxError as exc:
            unreadable[rel] = f"syntax error: {exc}"
            return None
        for line, name, why in index.inherited(rel):
            for chain in tf.classes.values():
                if chain[-1].lineno == line and chain[-1].name == name:
                    tf.mark_inherited(chain[-1], why)
        files[rel] = tf
        return tf

    for rel in scanned:
        parse(rel, require_markers=True)
    modules = [(rel, _module_parts(rel)) for rel in scanned]

    # Map each decided case to (file, test).
    targets: dict[str, dict[str, set[str]]] = {}  # file -> qualname -> decisions
    keys: dict[tuple[str, str], list[CaseKey]] = {}  # (file, qualname) -> its cases
    for key, decision in sorted(decided.items()):
        split = _split_case(key.path)
        if split is None:
            result.unresolved.append((key, "not a Python test case path"))
            continue
        parts, base = split
        # Every scanned module the case path can name, by the length of the
        # module path matched; the longest match is the case's module.
        matches: list[tuple[int, str]] = []
        for rel, mod in modules:
            for k in range(len(mod)):
                suffix = mod[k:]
                if parts[: len(suffix)] == suffix:
                    matches.append((len(suffix), rel))
                    break
        if not matches:
            if only:
                result.outside.append(key)
            else:
                result.unresolved.append((key, "no Python source of this module was scanned (another language?)"))
            continue
        best_len = max(n for n, _ in matches)
        found: list[tuple[str, TestFn]] = []
        missing: list[tuple[str, list[str]]] = []
        for n, rel in matches:
            if n != best_len:
                continue
            tf = parse(rel, require_markers=False)
            test = tf.find(parts[n:], base) if tf is not None else None
            if test is not None:
                found.append((rel, test))
            elif tf is not None:
                missing.append((rel, parts[n:]))
        if len(found) > 1:
            where = ", ".join(rel for rel, _ in found)
            result.unresolved.append((key, f"ambiguous: matches tests in {where}"))
            continue
        if not found:
            if not missing:
                result.unresolved.append((key, "its source could not be read"))
                continue
            for rel, classes in missing:
                name = ".".join([*classes, base])
                why = (
                    f"{name} was decided ({key}) but is not defined in {rel} (inherited from another module, or "
                    "generated?): the declarations around it are left alone; migrate this file by hand"
                )
                files[rel].mark_unseen(classes, why)
                result.unmatched_files.add(rel)
                result.unmatched.append(
                    (key, f"no {name} defined in {rel} (inherited from another module, or generated?)")
                )
            continue
        rel, test = found[0]
        targets.setdefault(rel, {}).setdefault(test.qualname, set()).add(decision)
        keys.setdefault((rel, test.qualname), []).append(key)

    rewritten: dict[str, str] = {}  # LF text of the changed files
    for rel in sorted(set(files) | (set(unreadable) & set(scanned))):
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
            blind = tf.blind_class(t)
            if blind and got - {"?"}:
                # Its static trace may miss inherited declarations.
                if blind not in reasons:
                    reasons.append(blind)
                continue
            if not ids and got and not blind:
                # Decided, but declares no id: its ownership is a model edit.
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
        if status == "changed":
            rewritten[rel] = new_text
        old_text = tf.text
        if newline.get(rel) == "\r\n":
            old_text, new_text = old_text.replace("\n", "\r\n"), new_text.replace("\n", "\r\n")
        result.files.append(FileResult(rel, status, [], changes, old_text, new_text))
    result.untagged = sorted(set(result.untagged))
    _involve(result, index, {rel: _changed_names(files[rel].text, text) for rel, text in rewritten.items()})
    _recheck(result, decided, keys, {f.path: rewritten[f.path] for f in result.changed}, unassigned)
    return result


def _recheck(
    result: ApplyResult,
    decided: Mapping[CaseKey, str],
    keys: Mapping[tuple[str, str], list[CaseKey]],
    rewritten: Mapping[str, str],
    unassigned: str,
) -> None:
    """Re-derive each decided case's attribution from the rewritten sources,
    with the same static resolver, and compare it with the worksheet."""
    untagged = set(result.untagged)
    for rel, text in sorted(rewritten.items()):
        try:
            tf = TestFile(text, rel)
            traces = {t.qualname: tf.trace(t) for t in tf.tests}
        except (SyntaxError, Unsupported) as exc:
            for (path, _), ks in keys.items():
                if path == rel:
                    result.mismatched.extend((k, f"{rel} no longer reads: {exc}") for k in ks)
            continue
        for (path, qualname), ks in sorted(keys.items()):
            if path != rel:
                continue
            for key in ks:
                decision = decided[key]
                if key in untagged or (decision == "?" and unassigned != "drop"):
                    continue
                want: tuple[str, ...] = () if decision in ("none", "?") else (decision,)
                got = traces[qualname][0] if qualname in traces else None
                if got != want and not (decision == "?" and got is not None and len(got) < 2):
                    shown = "no such test" if got is None else ", ".join(got) or "no id"
                    result.mismatched.append(
                        (key, f"the rewritten {rel} gives {shown}, the worksheet {', '.join(want) or 'none'}")
                    )
