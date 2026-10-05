# SPDX-License-Identifier: AGPL-3.0-or-later
"""The in-code traceability API: ``from rules_requirements import rr``.

``@rr.verifies(...)`` marks a test (function, method or ``TestCase`` class)
as verifying entities; ``@rr.implements(...)`` marks production code as
implementing them. Both are no-ops at runtime apart from recording metadata,
and both are recognised by the source scanner — as is the comment form
``# @rr(REQ-1): description``.
"""

from __future__ import annotations

import re
from typing import Any, Callable, TypeVar

from rules_requirements.hooks.ids import split_ids, warn_multiple
from rules_requirements.hooks.junit_writer import JUnitWriter  # noqa: F401 (re-export)

F = TypeVar("F", bound=Callable[..., Any])


def _ids(ids: tuple[Any, ...]) -> list[str]:
    out: list[str] = []
    for i in ids:
        if isinstance(i, (list, tuple, set)):
            out.extend(str(x).strip() for x in i)
        else:
            out.extend(re.split(r"[,\s]+", str(i)))
    return [i for i in out if i]


def verifies(*ids: Any, level: str = "", artifact: dict[str, str] | None = None) -> Callable[[F], F]:
    """Declare that the decorated test verifies the ONE requirement ``ids[0]`` at ``level``.

    The id is a declared tag; which requirement the case verifies is decided
    by attribution. The nearest declaration wins: a method's decorator
    replaces its class's, and a subclass's replaces its base class's (the
    level and artifact keys are still inherited when the nearer declaration
    does not set them).

    A test case verifies at most one requirement. Several ids — extra
    arguments, a comma or whitespace list, or stacked decorators naming
    different ids — are deprecated: every id is still recorded, with a
    :class:`~rules_requirements.hooks.ids.MultipleRequirementsWarning`, so
    attribution quarantines the case and it counts for none of them.
    """

    def deco(obj: F) -> F:
        prev = getattr(obj, "__rr__", None) or {}
        new = _ids(ids)
        named = split_ids(new)
        # Only the object's own declaration (a stacked decorator) is the same
        # scope: a subclass's ids replace its base class's (nearest wins).
        try:
            own = vars(obj).get("__rr__") or {}
        except TypeError:  # no __dict__
            own = prev
        own_ids = list(own.get("own_ids", own.get("ids", [])))
        before = split_ids(own_ids)
        subject = f"rr.verifies on {getattr(obj, '__qualname__', obj)!r}"
        if len(named) > 1:
            warn_multiple(subject, named)
        elif before and named and named[0] not in before:
            warn_multiple(f"{subject} (stacked decorators)", before + named)
        obj.__rr__ = {  # type: ignore[attr-defined]
            "ids": own_ids + new,  # this object's own declaration only
            "own_ids": own_ids + new,  # kept for readers of 0.2's key
            "level": (level or prev.get("level", "")).lower(),
            "artifact": {**(prev.get("artifact") or {}), **(artifact or {})},
        }
        return obj

    return deco


def implements(*ids: Any) -> Callable[[F], F]:
    """Declare that the decorated function/class implements ``ids``."""

    def deco(obj: F) -> F:
        obj.__rr_implements__ = _ids(ids)  # type: ignore[attr-defined]
        return obj

    return deco


def unittest_main(module: str = "__main__") -> None:
    """``unittest.main()`` replacement that writes traceability JUnit."""
    from rules_requirements.hooks.unittest import main

    raise SystemExit(main(module))
