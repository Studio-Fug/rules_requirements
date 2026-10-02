# SPDX-License-Identifier: AGPL-3.0-or-later
"""One test case, one requirement: id checks shared by every hook.

A test case counts toward at most one requirement. Hooks that still accept
several ids per case (the pre-0.2 list forms) keep recording all of them in
0.2, but warn with :class:`MultipleRequirementsWarning`; the single-id APIs
reject anything that is not exactly one well-formed id.

Error codes printed by the hooks:

======== ==========================================================
RR-E101  One case names more than one id.
RR-E104  Malformed id: a comma, whitespace, or empty.
======== ==========================================================
"""

from __future__ import annotations

import re
import warnings
from typing import Any

from rules_requirements.util import dedupe

E_MULTIPLE = "RR-E101"
E_MALFORMED = "RR-E104"

_SEPARATORS = re.compile(r"[,\s]+")


class MultipleRequirementsWarning(DeprecationWarning):
    """A test case declares more than one requirement id (deprecated in 0.2).

    The ids are still recorded as before. Filter on this class to silence or
    escalate the deprecation, e.g. ``-W error::rules_requirements.hooks.ids.MultipleRequirementsWarning``.
    """


def split_ids(value: Any) -> list[str]:
    """Every distinct id in ``value``: a string (split on commas and whitespace)
    or a list, tuple or set of them. Empty parts are dropped."""
    if isinstance(value, (list, tuple, set, frozenset)):
        parts = [p for v in value for p in split_ids(v)]
    else:
        parts = [p for p in _SEPARATORS.split(str(value)) if p]
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
        f"[{E_MULTIPLE}]. Every id is still recorded for now, but multi-id declarations are deprecated: "
        "from 0.3 such a case counts for no requirement, and 0.4 rejects it. Split the test, or keep one id."
    )


def warn_multiple(subject: str, ids: list[str], stacklevel: int = 2) -> None:
    """Warn that ``subject`` declares several ``ids`` for one test case.

    ``stacklevel`` is as for :func:`warnings.warn` called by the caller of
    ``warn_multiple`` (2: the caller's caller).
    """
    warnings.warn(multiple_warning(subject, ids), stacklevel=stacklevel + 1)
