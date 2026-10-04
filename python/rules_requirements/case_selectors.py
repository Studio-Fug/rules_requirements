# SPDX-License-Identifier: AGPL-3.0-or-later
"""Case selectors: which per-case results of a target a claim names.

A ``verified_by`` item ``{target, cases: [...]}`` lists selectors. The
grammar is deliberately closed — ``*`` is the only wildcard — because it makes
the question "can two claims ever select the same case?" exactly decidable
(:func:`witness`), so ``shared-case`` is a static error with a concrete
example instead of a report-time surprise.

* A selector matches the **whole** canonical case path
  (:mod:`rules_requirements.case_keys`), case-sensitively.
* ``*`` matches any string, the empty string, ``::``, ``/`` and blanks
  included; ``**`` is the same as ``*``.
* ``\\*`` is a literal star and ``\\\\`` a literal backslash; any other
  backslash is a ``bad-selector``.
* ``?``, ``[`` and ``]`` are literals: pytest ids such as
  ``test_x[exc0-False]`` contain them (unlike :mod:`fnmatch`, where
  ``test_x[*]`` would not match ``test_x[a]``).
* A selector is never empty, never padded with blanks, and never the
  synthetic path ``[target]`` (claim a target's single synthetic result with
  ``whole: true``). Attribution never lets a selector match a synthetic or
  target-scope result.
* A selector is in Unicode NFC, like every case path, and never ends with an
  ``[rr:ID]`` name tag (ingest strips those from case names). Either would
  never match: it would read as a missing case forever, and two spellings of
  one case would compare as two cases.

The module is named ``case_selectors`` rather than ``selectors``: the latter
would shadow the standard library module (imported by :mod:`subprocess` and
:mod:`asyncio`) whenever the package directory itself is on ``sys.path``.
"""

from __future__ import annotations

import functools
import re
import unicodedata
from typing import Optional, Tuple

STAR: None = None
"""The wildcard in a token sequence (every other token is one character)."""

Token = Optional[str]
Tokens = Tuple[Token, ...]

SYNTHETIC_PATH = "[target]"  # == case_keys.SYNTHETIC_PATH (not imported: no ingest dependency here)
_NAME_TAG = re.compile(r"\[rr:[^\]]*\]")  # == case_keys._NAME_TAG, which case_path strips from names


class BadSelector(ValueError):  # noqa: N818 - named after the bad-selector rule
    """``pattern`` is not a selector (rule ``bad-selector``)."""


@functools.lru_cache(maxsize=4096)
def tokens(pattern: str) -> Tokens:
    """``pattern`` as one-character literals and :data:`STAR` (runs of
    ``*`` collapse into one); raises :class:`BadSelector` on a bad escape."""
    out: list[Token] = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "\\":
            if i + 1 >= len(pattern) or pattern[i + 1] not in "*\\":
                raise BadSelector(f"{pattern!r}: '\\' must be followed by '*' or '\\' (offset {i})")
            out.append(pattern[i + 1])
            i += 2
        elif ch == "*":
            if not out or out[-1] is not STAR:
                out.append(STAR)
            i += 1
        else:
            out.append(ch)
            i += 1
    return tuple(out)


def check(pattern: str) -> None:
    """Raise :class:`BadSelector` unless ``pattern`` is a valid selector."""
    if not isinstance(pattern, str):
        raise BadSelector(f"{pattern!r} is not a string")
    if not pattern:
        raise BadSelector("empty selector (use '*' for every case of the target)")
    if pattern != pattern.strip():
        raise BadSelector(f"{pattern!r}: leading or trailing blanks (case paths are stripped, so it would never match)")
    nfc = unicodedata.normalize("NFC", pattern)
    if pattern != nfc:
        raise BadSelector(f"{pattern!r}: not in Unicode NFC (case paths are, so it would never match); write {nfc!r}")
    toks = tokens(pattern)
    # What follows the last '*' and the last '::' after it is the end of the
    # case name (a classname keeps its tags; names never hold one).
    tail = "".join(t for t in toks[_last_star(toks) + 1 :] if t is not STAR).rpartition("::")[2]
    if _NAME_TAG.search(tail):
        raise BadSelector(f"{pattern!r}: holds an [rr:ID] name tag, which ingest strips from case names; leave it out")
    if pattern == SYNTHETIC_PATH:
        raise BadSelector(
            f"{pattern!r} is the synthetic result of a target without per-case results; claim it with whole: true"
        )


def _last_star(toks: Tokens) -> int:
    for i in range(len(toks) - 1, -1, -1):
        if toks[i] is STAR:
            return i
    return -1


def is_literal(pattern: str) -> bool:
    """Whether ``pattern`` names exactly one case path (no unescaped ``*``)."""
    return STAR not in tokens(pattern)


def literal_path(pattern: str) -> str:
    """The case path a literal selector names (escapes resolved)."""
    toks = tokens(pattern)
    if STAR in toks:
        raise ValueError(f"{pattern!r} is not a literal selector")
    return "".join(t for t in toks if t is not None)


def escape(path: str) -> str:
    """The literal selector for exactly ``path``."""
    return path.replace("\\", "\\\\").replace("*", "\\*")


def matches(pattern: str, path: str) -> bool:
    """Whether ``pattern`` matches the whole of ``path``.

    Greedy matching with backtracking to the last star only: O(len(pattern) *
    len(path)) in the worst case, never exponential.
    """
    toks = tokens(pattern)
    n, m = len(toks), len(path)
    i = j = 0
    star_i, star_j = -1, 0
    while j < m:
        if i < n and toks[i] is not STAR and toks[i] == path[j]:
            i += 1
            j += 1
        elif i < n and toks[i] is STAR:
            star_i, star_j = i, j
            i += 1
        elif star_i >= 0:
            star_j += 1
            i, j = star_i + 1, star_j
        else:
            return False
    while i < n and toks[i] is STAR:
        i += 1
    return i == n


def witness(p: str, q: str) -> str | None:
    """A case path both selectors match, or ``None`` if no path can match both.

    Exact for this grammar: a dynamic programme over (position in ``p``,
    position in ``q``), O(len(p) * len(q)). The witness only uses characters the
    selectors spell out, and is a shortest one.
    """
    a, b = tokens(p), tokens(q)
    if STAR not in a or STAR not in b:  # a literal: the only candidate is its own path
        lit, other = (p, q) if STAR not in a else (q, p)
        path = literal_path(lit)
        return path if matches(other, path) else None
    n, m = len(a), len(b)
    # ok[i][j]: a[i:] and b[j:] match a common string; step[i][j] records how.
    # Steps: 0 = a's star matches nothing, 1 = b's star matches nothing,
    # 2 = a's star takes b's literal, 3 = b's star takes a's literal,
    # 4 = both literals agree.
    inf = n + m + 1
    best = [[inf] * (m + 1) for _ in range(n + 1)]
    step = [[-1] * (m + 1) for _ in range(n + 1)]
    best[n][m] = 0
    for i in range(n, -1, -1):
        for j in range(m, -1, -1):
            if i == n and j == m:
                continue
            ta = a[i] if i < n else ""
            tb = b[j] if j < m else ""
            cands: list[tuple[int, int]] = []
            if i < n and ta is STAR:
                cands.append((best[i + 1][j], 0))
                if j < m and tb is not STAR:
                    cands.append((best[i][j + 1] + 1, 2))
            if j < m and tb is STAR:
                cands.append((best[i][j + 1], 1))
                if i < n and ta is not STAR:
                    cands.append((best[i + 1][j] + 1, 3))
            if i < n and j < m and ta is not STAR and tb is not STAR and ta == tb:
                cands.append((best[i + 1][j + 1] + 1, 4))
            for cost, how in cands:
                if cost < best[i][j]:
                    best[i][j], step[i][j] = cost, how
    if best[0][0] >= inf:
        return None
    out: list[str] = []
    i = j = 0
    while (i, j) != (n, m):
        how = step[i][j]
        if how == 0:
            i += 1
        elif how == 1:
            j += 1
        elif how == 2:
            out.append(b[j] or "")
            j += 1
        elif how == 3:
            out.append(a[i] or "")
            i += 1
        else:
            out.append(a[i] or "")
            i += 1
            j += 1
    return "".join(out)


def overlaps(p: str, q: str) -> bool:
    """Whether some case path matches both selectors."""
    return witness(p, q) is not None
