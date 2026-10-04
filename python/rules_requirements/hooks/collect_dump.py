# SPDX-License-Identifier: AGPL-3.0-or-later
"""A tiny pytest plugin that dumps what pytest would collect, as JSON.

``rr migrate apply --stage tags`` loads this with ``-p`` in a
``--collect-only`` subprocess to verify a rewrite *dynamically*: it records,
for every collected item, its ``nodeid`` and the requirement ids
:func:`rules_requirements.hooks.pytest_plugin.trace_of` returns — the exact
function the JUnit hook uses, so the dump cannot disagree with what a real run
would attribute.

It also registers the ``rr`` markers exactly as the JUnit hook does (the same
``pytest_configure``), so a project running ``--strict-markers`` that loads
rr's plugin with ``-p`` (as the Bazel runner does) collects here too.

The dump is written to the file named by the ``RR_COLLECT_DUMP`` environment
variable when collection finishes::

    {"items": [["pkg/test_m.py::test_a", ["REQ-1"]], ...],
     "skipped": {"pkg/test_hw.py": "could not import 'hwlib'..."},
     "args": ["/abs/project/pkg"], "norecursedirs": [".*", "build", ...]}

``items`` is a list of ``[nodeid, ids]`` pairs, not a mapping, so two items
that share a nodeid are both recorded (the check refuses on the duplicate
rather than silently keeping only the last). ``skipped`` names the collectors
pytest skipped at collection time
(``importorskip``, a module-level ``pytest.skip``): tests the check cannot see.
``args`` are the paths pytest started collecting from (the command line's, or
``testpaths``), made absolute, and ``norecursedirs`` the patterns it does not
recurse into: the check uses them to tell whether pytest reaches a symlink.

Stdlib only (``rules_requirements`` stays importable without pytest); the
``pytest`` import happens through :mod:`rules_requirements.hooks.pytest_plugin`.
"""

from __future__ import annotations

import json
import os
from typing import Any

from rules_requirements.hooks.pytest_plugin import _trylast, pytest_configure, trace_of

__all__ = ["pytest_collection_finish", "pytest_collection_modifyitems", "pytest_collectreport", "pytest_configure"]

_ITEMS: list[list[Any]] = []
_SKIPPED: dict[str, str] = {}


@_trylast  # after every conftest's modifyitems, exactly as the JUnit hook records
def pytest_collection_modifyitems(items: list[Any]) -> None:
    for item in items:
        _ITEMS.append([item.nodeid, trace_of(item)[0]])


def pytest_collectreport(report: Any) -> None:
    if getattr(report, "skipped", False):
        longrepr = getattr(report, "longrepr", None)
        # A skip's longrepr is (path, lineno, message).
        reason = longrepr[2] if isinstance(longrepr, tuple) and len(longrepr) == 3 else str(longrepr or "")
        _SKIPPED[report.nodeid] = str(reason)


def pytest_collection_finish(session: Any) -> None:
    path = os.environ.get("RR_COLLECT_DUMP")
    if not path:
        return
    config = session.config
    base = str(getattr(getattr(config, "invocation_params", None), "dir", "") or os.getcwd())
    args = [os.path.abspath(os.path.join(base, str(a).split("::")[0])) for a in (getattr(config, "args", None) or [])]
    try:
        norecurse = [str(p) for p in config.getini("norecursedirs")]
    except (ValueError, KeyError):  # pragma: no cover - pytest always defines it
        norecurse = []
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"items": _ITEMS, "skipped": _SKIPPED, "args": args, "norecursedirs": norecurse}, fh, sort_keys=True)
