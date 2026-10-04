# SPDX-License-Identifier: AGPL-3.0-or-later
"""One test case, one requirement: id checks shared by every hook.

A test case counts toward at most one requirement. The hooks only ever
*declare* ids: which requirement a case verifies is decided by attribution
alone, never by a hook. Hooks that still accept several ids per case (the
pre-0.2 list forms) keep recording all of them in 0.3, with a
:class:`MultipleRequirementsWarning`; the evidence then names several ids, so
attribution quarantines the case and it counts for none of them. The
single-id APIs reject anything that is not exactly one well-formed id.

Error codes printed by the hooks:

======== ==========================================================
RR-E101  One case names more than one id.
RR-E102  A raw ``requirement`` property bypassed the single-id API.
RR-E103  ``RR_VERIFIES`` was called outside a running test.
RR-E104  Malformed id: a comma, whitespace, or empty.
======== ==========================================================
"""

from __future__ import annotations

import re
import warnings
from typing import Any

from rules_requirements.util import dedupe

E_MULTIPLE = "RR-E101"
E_RAW_PROPERTY = "RR-E102"
E_OUTSIDE_TEST = "RR-E103"
E_MALFORMED = "RR-E104"

_SEPARATORS = re.compile(r"[,\s]+")  # what separates ids, so what makes a string not ONE id


class MultipleRequirementsWarning(DeprecationWarning):
    """A test case declares more than one requirement id (deprecated since 0.2).

    The ids are still all recorded, and attribution quarantines the case (from
    0.3): it verifies none of them, and every requirement it names reads
    INVALID. Filter on this class to silence or
    escalate the deprecation, e.g. ``-W error::rules_requirements.hooks.ids.MultipleRequirementsWarning``.
    """


def split_ids(value: Any) -> list[str]:
    """Every distinct id in ``value``: a string, or a list, tuple or set of them.

    A string is split on commas and whitespace, as ingest splits a declared
    ``requirement`` value from 0.3: ``"REQ-1 REQ-2"`` names two ids (before
    0.3 it was read as one malformed id). Empty parts are dropped.
    """
    if isinstance(value, (list, tuple, set, frozenset)):
        parts = [p for v in value for p in split_ids(v)]
    else:
        parts = [p for p in _SEPARATORS.split(str(value).strip()) if p]
    return dedupe(parts)


def check_id(value: Any, where: str = "requirement") -> str:
    """``value`` if it is exactly one well-formed id, else ValueError.

    A list or tuple is RR-E101 (one case, one requirement); a string with a
    comma or whitespace in it, or an empty one, is RR-E104. Anything else is
    a TypeError.
    """
    if isinstance(value, (list, tuple, set, frozenset)):
        raise ValueError(
            f"{where}: expected ONE requirement id, got {list(value)!r}; "
            f"a test case verifies at most one requirement [{E_MULTIPLE}]"
        )
    if not isinstance(value, str):
        raise TypeError(f"{where}: expected a requirement id string, got {type(value).__name__}")
    if not value.strip() or _SEPARATORS.search(value):
        raise ValueError(
            f"{where}: {value!r} is not ONE requirement id (no commas, whitespace or empty ids) [{E_MALFORMED}]"
        )
    return value


def multiple_warning(subject: str, ids: list[str]) -> MultipleRequirementsWarning:
    """The warning for ``subject`` declaring several ``ids`` for one test case."""
    return MultipleRequirementsWarning(
        f"rr: {subject} names {', '.join(ids)}; a test case verifies at most one requirement "
        f"[{E_MULTIPLE}]. Multi-id declarations are deprecated: every id is still recorded, so the case is "
        "quarantined and counts for none of them (each reads INVALID), and 0.4 rejects it. "
        "Split the test, or keep one id."
    )


def warn_multiple(subject: str, ids: list[str], stacklevel: int = 2) -> None:
    """Warn that ``subject`` declares several ``ids`` for one test case.

    ``stacklevel`` is as for :func:`warnings.warn` called by the caller of
    ``warn_multiple`` (2: the caller's caller).
    """
    warnings.warn(multiple_warning(subject, ids), stacklevel=stacklevel + 1)
