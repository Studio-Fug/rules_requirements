# SPDX-License-Identifier: AGPL-3.0-or-later
"""A tiny pytest plugin that dumps what pytest would collect, as JSON.

``rr migrate apply --stage tags`` loads this with ``-p`` in a
``--collect-only`` subprocess to verify a rewrite *dynamically*: it records,
for every collected item, its ``nodeid`` and the requirement ids
:func:`rules_requirements.hooks.pytest_plugin.trace_of` returns — the exact
function the JUnit hook uses, so the dump cannot disagree with what a real run
would attribute.

The dump is written to the file named by the ``RR_COLLECT_DUMP`` environment
variable when collection finishes::

    {"items": {"pkg/test_m.py::test_a": ["REQ-1"], ...}}

Stdlib only (``rules_requirements`` stays importable without pytest); the
``pytest`` import happens through :mod:`rules_requirements.hooks.pytest_plugin`.
"""

from __future__ import annotations

import json
import os
from typing import Any

from rules_requirements.hooks.pytest_plugin import _trylast, trace_of

_ITEMS: dict[str, list[str]] = {}


@_trylast  # after every conftest's modifyitems, exactly as the JUnit hook records
def pytest_collection_modifyitems(items: list[Any]) -> None:
    for item in items:
        _ITEMS[item.nodeid] = trace_of(item)[0]


def pytest_collection_finish(session: Any) -> None:
    path = os.environ.get("RR_COLLECT_DUMP")
    if not path:
        return
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"items": _ITEMS}, fh, sort_keys=True)
