# SPDX-License-Identifier: AGPL-3.0-or-later
"""The in-code traceability API: ``from rules_requirements import rr``.

``@rr.verifies(...)`` marks a test (function, method or ``TestCase`` class)
as verifying entities; ``@rr.implements(...)`` marks production code as
implementing them. Both are no-ops at runtime apart from recording metadata,
and both are recognised by the source scanner — as is the comment form
``# @rr(REQ-1): description``.
"""

from __future__ import annotations

from typing import Any, Callable, TypeVar

from rules_requirements.hooks.junit_writer import JUnitWriter  # noqa: F401 (re-export)

F = TypeVar("F", bound=Callable[..., Any])


def _ids(ids: tuple[Any, ...]) -> list[str]:
    out: list[str] = []
    for i in ids:
        if isinstance(i, (list, tuple, set)):
            out.extend(str(x).strip() for x in i)
        else:
            out.extend(p.strip() for p in str(i).split(","))
    return [i for i in out if i]


def verifies(*ids: Any, level: str = "", artifact: dict[str, str] | None = None) -> Callable[[F], F]:
    """Declare that the decorated test verifies ``ids`` at ``level``."""

    def deco(obj: F) -> F:
        prev = getattr(obj, "__rr__", None) or {}
        obj.__rr__ = {  # type: ignore[attr-defined]
            "ids": list(prev.get("ids", [])) + _ids(ids),
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
