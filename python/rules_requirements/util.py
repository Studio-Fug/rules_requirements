# SPDX-License-Identifier: AGPL-3.0-or-later
"""Small shared helpers."""

from __future__ import annotations

import re
from typing import Any

_NUM = re.compile(r"(\d+)")


def natural_key(text: str) -> tuple[Any, ...]:
    """Sort key that orders ``REQ-2`` before ``REQ-10``."""
    return tuple(int(p) if p.isdigit() else p for p in _NUM.split(text))


def dedupe(items: list[str]) -> list[str]:
    """Drop empties and duplicates, preserving first-seen order."""
    seen: set[str] = set()
    out = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out
