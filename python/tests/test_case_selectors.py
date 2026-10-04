# SPDX-License-Identifier: AGPL-3.0-or-later
"""Case selectors: the '*'-only grammar, matching, and the exact overlap witness.

The witness is what makes ``shared-case`` a static error, so it is
cross-checked exhaustively against brute force. Brute force over a bounded
set of strings is *exact* here: if two selectors share any string ``s``, every
character of ``s`` that both match with a star can be deleted (stars match
anything), and every remaining character is a literal of ``p`` or ``q``. So
two selectors overlap iff they share a string of length at most
``literals(p) + literals(q)`` over their literal characters — exactly the
strings enumerated below.
"""

import itertools
import random
import re

import pytest

from rules_requirements import case_selectors as cs
from rules_requirements.case_selectors import STAR, BadSelector, matches, witness


def _regex(pattern: str) -> re.Pattern[str]:
    """An independent reading of the grammar (re, not case_selectors.tokens)."""
    out, i = [], 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "\\":
            out.append(re.escape(pattern[i + 1]))
            i += 2
        else:
            out.append(".*" if ch == "*" else re.escape(ch))
            i += 1
    return re.compile("".join(out), re.S)


def _literal_count(pattern: str) -> int:
    return sum(1 for t in cs.tokens(pattern) if t is not STAR)


def _cross_check(patterns: list[str], alphabet: str) -> int:
    """Every pair of ``patterns``: witness() is not None iff brute force finds
    a common string, and a witness matches both. Returns the pairs checked."""
    longest = 2 * max(_literal_count(p) for p in patterns)
    strings = ["".join(s) for n in range(longest + 1) for s in itertools.product(alphabet, repeat=n)]
    index = {s: k for k, s in enumerate(strings)}
    masks = {}
    for p in patterns:
        rx = _regex(p)
        mask = 0
        for k, s in enumerate(strings):
            hit = rx.fullmatch(s) is not None
            assert matches(p, s) == hit, (p, s)
            if hit:
                mask |= 1 << k
        masks[p] = mask
    pairs = 0
    for p, q in itertools.product(patterns, repeat=2):
        bound = _literal_count(p) + _literal_count(q)
        common = masks[p] & masks[q]
        brute = common != 0
        w = witness(p, q)
        assert (w is not None) == brute, (p, q, w)
        if w is not None:
            assert matches(p, w) and matches(q, w), (p, q, w)
            assert len(w) <= bound
            # a shortest common string (strings are enumerated by length)
            assert len(w) == len(strings[(common & -common).bit_length() - 1]), (p, q, w)
            assert w in index
        pairs += 1
    return pairs


def test_witness_exhaustive_against_brute_force():
    patterns = ["".join(t) for n in range(6) for t in itertools.product("a:*", repeat=n)]
    assert _cross_check(patterns, "a:") >= 20_000


def test_witness_with_escaped_stars_against_brute_force():
    patterns = ["".join(t) for n in range(6) for t in itertools.product(["a", "\\*", "*"], repeat=n)]
    assert _cross_check(patterns, "a*") >= 20_000


def test_witness_random_long_patterns_are_sound():
    rng = random.Random(7)
    alphabet = ["a", "b", ":", " ", "[", "]", "*", "\\*", "\\\\"]
    for _ in range(3000):
        p = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 12)))
        q = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 12)))
        w = witness(p, q)
        if w is not None:
            assert matches(p, w) and matches(q, w), (p, q, w)
            assert witness(q, p) is not None
        else:
            assert witness(q, p) is None


@pytest.mark.parametrize(
    "p, q, want",
    [
        ("clocksync::*", "clocksync::offset*", "clocksync::offset"),
        ("pi.server.tests.test_handler::*", "pi.server.tests.test_session::*", None),
        ("*::test_a*", "*::*_b", "::test_a_b"),
        ("a*b", "b*a", None),
        ("x\\*", "x*", "x*"),
        ("x\\*", "xy", None),
        ("t[exc0-False]", "t[*]", "t[exc0-False]"),
        ("hitl_e2e.flash_boot::*", "hitl_e2e.improv_provision::*", None),
        ("*", "*", ""),
        ("m::t", "m::t", "m::t"),
        ("m::t", "m::u", None),
    ],
)
def test_witness_examples(p, q, want):
    assert witness(p, q) == want
    assert cs.overlaps(p, q) == (want is not None)


def test_grammar():
    assert cs.tokens("a**b") == ("a", STAR, "b")
    assert cs.tokens("a\\*\\\\") == ("a", "*", "\\")
    for bad in ("a\\", "a\\b", "\\?"):
        with pytest.raises(BadSelector, match="must be followed"):
            cs.tokens(bad)
    for bad, why in (("", "empty"), (" a", "blanks"), ("a ", "blanks"), ("[target]", "whole: true"), ("x\\y", "\\\\")):
        with pytest.raises(BadSelector, match=why):
            cs.check(bad)
    cs.check("test_x[exc0-False]")
    cs.check("a b::c > d")
    assert cs.is_literal("t[x]?") and cs.is_literal("a\\*") and not cs.is_literal("a*")
    assert cs.literal_path("a\\*\\\\b") == "a*\\b"
    with pytest.raises(ValueError):
        cs.literal_path("a*")


def test_matching_is_whole_path_and_case_sensitive():
    assert matches("m::*", "m::t") and not matches("m::*", "xm::t") and not matches("M::*", "m::t")
    assert not matches("m::t", "m::tt") and not matches("m::t", "m::")
    # '*' crosses '::', '/', ' > ' and blanks, and matches the empty string
    assert matches("a*z", "a::b/c > d z") and matches("a*", "a")
    # '?', '[' and ']' are literals (unlike fnmatch)
    assert matches("t[*]", "t[a]") and not matches("t[ab]", "ta") and not matches("t?", "tx")
    assert matches("t?", "t?")
    # escapes
    assert matches("x\\*", "x*") and not matches("x\\*", "xy") and matches("a\\\\b", "a\\b")


def test_matching_is_not_exponential():
    path = "a" * 2000
    assert not matches("*a*a*a*a*a*a*a*a*a*a*b", path)
    assert matches("*a*a*a*a*a*a*a*a*a*a*", path)


def test_escape_round_trips():
    rng = random.Random(3)
    for _ in range(500):
        path = "".join(rng.choice("ab*\\[]? :") for _ in range(rng.randint(1, 10)))
        sel = cs.escape(path)
        assert cs.is_literal(sel) and cs.literal_path(sel) == path and matches(sel, path)
