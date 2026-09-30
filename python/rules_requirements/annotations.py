# SPDX-License-Identifier: AGPL-3.0-or-later
"""Find traceability annotations in source code.

The universal form is a comment (or docstring) tag::

    # @rr(REQ-0001): Implements isolated access to secure data
    class SecureStore: ...

    // @rr.verifies(REQ-0002, REQ-0003)
    TEST(Parser, RejectsEmpty) { ... }

``@rr(...)`` means *implements* in production code and *verifies* in test
files (``test_*.py``, ``*_test.*``, ``tests/`` ...); ``@rr.implements`` /
``@rr.verifies`` state it explicitly. The language hooks are recognised too:
``@pytest.mark.rr(...)`` / ``@pytest.mark.requirements(...)``,
``rr::verifies!(...)`` (Rust) and ``RR_VERIFIES(...)`` (googletest).

Each :class:`Reference` records the ids, the relation, the location, the
trailing description and — when a definition follows the tag — the symbol it
annotates (``class SecureStore``, ``Parser.RejectsEmpty``), which is what lets
a reviewer browse from a requirement to the exact code that implements it.
"""

from __future__ import annotations

import fnmatch
import os
import re
import subprocess
from dataclasses import dataclass
from typing import Iterable

from rules_requirements.config import Config
from rules_requirements.model import Model

IMPLEMENTS, VERIFIES = "implements", "verifies"

_TAGS = [
    # (regex with group 'ids', relation or None for the path-based default,
    #  whether the tag sits *inside* the body it annotates)
    (re.compile(r"@rr\.(?P<rel>implements|verifies)\((?P<ids>[^)]*)\)"), None, False),
    (re.compile(r"@rr\((?P<ids>[^)]*)\)"), None, False),
    (re.compile(r"\bmark\.(?:rr|requirements)\((?P<ids>[^)]*)\)"), VERIFIES, False),
    (re.compile(r"\brr::verifies!\s*\((?P<ids>[^)]*)\)"), VERIFIES, True),
    (re.compile(r"\bRR_VERIFIES\s*\((?P<ids>[^)]*)\)"), VERIFIES, True),
]
_DESCRIPTION = re.compile(r"^\s*:\s*(?P<text>.+?)\s*(?:\*/|-->|\"\"\"|''')?\s*$")

_DEF = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:pub(?:\([^)]*\))?\s+)?(?:(?:async|static|abstract|final|public|private|protected|inline|virtual|constexpr)\s+)*"
    r"(?P<kw>def|class|fn|struct|enum|trait|impl|func|function|interface|type|module|mod|namespace)\s+"
    r"(?P<name>[A-Za-z_][\w:]*)"
)
_GTEST = re.compile(r"^\s*(?:TEST|TEST_F|TEST_P|TYPED_TEST)\s*\(\s*(?P<suite>\w+)\s*,\s*(?P<name>\w+)")
# Lines to look past while searching for the annotated definition (attributes,
# decorators, comments); a blank line ends the search.
_SKIPPABLE = re.compile(r"^\s*(?:#\[|@|//|#(?!\w)|\*|/\*)")

DEFAULT_EXTENSIONS = (
    ".py", ".pyi", ".rs", ".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx",
    ".go", ".java", ".kt", ".swift", ".m", ".mm", ".js", ".jsx", ".ts", ".tsx", ".mjs",
    ".cs", ".rb", ".sh", ".bzl", ".proto", ".v", ".sv", ".vhd", ".vue", ".svelte",
)  # fmt: skip
DEFAULT_NAMES = ("BUILD", "BUILD.bazel", "MODULE.bazel")
DEFAULT_EXCLUDES = (
    ".git/*", "*/node_modules/*", "node_modules/*", "bazel-*", "*/_vendor/*",
    "third_party/*", "*/third_party/*", ".venv/*", "*/.venv/*",
)  # fmt: skip
_TEST_PATH = re.compile(
    r"(^|/)(tests?|testing|spec|__tests__)/|(^|/)test_[^/]*$|_test\.[^/]+$|\.test\.[^/]+$|_tests?\.[^/]+$|\.spec\.[^/]+$"
)


@dataclass(frozen=True)
class Reference:
    ids: tuple[str, ...]
    relation: str  # implements | verifies
    path: str
    line: int
    text: str = ""  # trailing description after "):"
    symbol: str = ""  # annotated definition, if one follows

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "ids": list(self.ids),
            "relation": self.relation,
            "path": self.path,
            "line": self.line,
        }
        if self.text:
            out["text"] = self.text
        if self.symbol:
            out["symbol"] = self.symbol
        return out


def is_test_path(path: str) -> bool:
    return bool(_TEST_PATH.search(path.replace("\\", "/")))


def _def_name(line: str) -> str:
    g = _GTEST.match(line)
    if g:
        return f"{g.group('suite')}.{g.group('name')}"
    d = _DEF.match(line)
    if d:
        return f"{d.group('kw')} {d.group('name').rstrip(':')}"
    return ""


def _symbol(lines: list[str], index: int, start: int, inside: bool) -> str:
    """The definition annotated by a tag at ``lines[index][start:]``."""
    if inside:  # in-body hook: the nearest enclosing definition above
        for line in reversed(lines[max(0, index - 40) : index]):
            name = _def_name(line)
            if name:
                return name
        return ""
    before = lines[index][:start]
    if before.strip() and not _SKIPPABLE.match(before):
        return _def_name(before)  # trailing tag on the definition line itself
    for line in lines[index + 1 : index + 8]:
        name = _def_name(line)
        if name:
            return name
        if not line.strip() or not _SKIPPABLE.match(line):
            return ""
    return ""


def extract(text: str, path: str, config: Config) -> list[Reference]:
    """All references in one file's text."""
    id_re = config.any_id_regex()
    default_rel = VERIFIES if is_test_path(path) else IMPLEMENTS
    extra = [(re.compile(p), None, False) for p in config.annotation_patterns]
    lines = text.splitlines()
    refs: list[Reference] = []
    for index, line in enumerate(lines):
        if "rr" not in line and "RR_" not in line and "requirements" not in line and not extra:
            continue
        taken: list[tuple[int, int]] = []
        for regex, fixed_rel, inside in _TAGS + extra:
            for m in regex.finditer(line):
                if any(a <= m.start() < b for a, b in taken):
                    continue  # already consumed by a more specific tag
                taken.append((m.start(), m.end()))
                body = m.group("ids") if "ids" in regex.groupindex else m.group(1)
                ids = tuple(dict.fromkeys(id_re.findall(body)))
                if not ids:
                    continue
                rel = fixed_rel or (m.groupdict().get("rel") or default_rel)
                desc = _DESCRIPTION.match(line[m.end() :])
                refs.append(
                    Reference(
                        ids=ids,
                        relation=rel,
                        path=path,
                        line=index + 1,
                        text=desc.group("text") if desc else "",
                        symbol=_symbol(lines, index, m.start(), inside),
                    )
                )
    return refs


def _git_files(root: str) -> list[str] | None:
    try:
        out = subprocess.run(
            ["git", "-C", root, "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            check=True,
            capture_output=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    return sorted(p for p in out.decode("utf-8", "replace").split("\0") if p)


def _walk_files(root: str) -> list[str]:
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith((".", "bazel-")) and d != "node_modules")
        rel = os.path.relpath(dirpath, root)
        for name in filenames:
            out.append(name if rel == "." else os.path.join(rel, name).replace(os.sep, "/"))
    return sorted(out)


def candidate_files(
    root: str,
    include: Iterable[str] = (),
    exclude: Iterable[str] = (),
) -> list[str]:
    """Files under ``root`` to scan (git-aware when ``root`` is a checkout)."""
    files = _git_files(root)
    if files is None:
        files = _walk_files(root)
    include = list(include)
    excludes = list(DEFAULT_EXCLUDES) + list(exclude)
    out = []
    for rel in files:
        base = rel.rsplit("/", 1)[-1]
        if include:
            if not any(fnmatch.fnmatch(rel, pat) for pat in include):
                continue
        elif not (base.endswith(DEFAULT_EXTENSIONS) or base in DEFAULT_NAMES):
            continue
        if any(fnmatch.fnmatch(rel, pat) for pat in excludes):
            continue
        out.append(rel)
    return out


def scan(
    root: str,
    config: Config,
    include: Iterable[str] = (),
    exclude: Iterable[str] = (),
    files: Iterable[str] | None = None,
) -> list[Reference]:
    """Scan ``root`` (or the given ``files``, relative to it) for references."""
    refs: list[Reference] = []
    for rel in files if files is not None else candidate_files(root, include, exclude):
        full = os.path.join(root, rel)
        try:
            with open(full, encoding="utf-8", errors="strict") as fh:
                text = fh.read()
        except (OSError, UnicodeDecodeError):
            continue
        refs.extend(extract(text, rel.replace(os.sep, "/"), config))
    return refs


def unknown_references(refs: Iterable[Reference], model: Model) -> list[tuple[Reference, str]]:
    """(reference, id) for every referenced id the model does not define."""
    known = model.ids()
    return [(ref, rid) for ref in refs for rid in ref.ids if rid not in known]
