# SPDX-License-Identifier: AGPL-3.0-or-later
"""Verify a tag rewrite against pytest itself (``rr migrate apply``).

The static codemod (:mod:`rules_requirements.tag_codemod`) cannot prove what
pytest will *collect*: a test can be installed by a factory, a metaclass,
``__init_subclass__``, ``setattr`` or an ``exec`` the syntax tree does not
follow. So, by default, after the rewrite passes the static guards, apply
checks it dynamically:

* copy the project tree to a temporary directory and write the rewritten files
  there (never in place);
* run ``<python> -m pytest --collect-only`` with the
  :mod:`~rules_requirements.hooks.collect_dump` plugin in both the original
  tree ("before") and the copy ("after"), recording every item's ``nodeid``
  and the ids :func:`~rules_requirements.hooks.pytest_plugin.trace_of` gives it;
* compare: each decided case must end with exactly its owner (no id for
  ``none``), every other item must keep exactly the ids it had, and the set of
  collected nodeids must not change.

Any difference, or a collection that fails in either tree, makes apply refuse
and write nothing. ``rules_requirements`` stays stdlib-only: pytest runs as a
subprocess.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Optional

import rules_requirements
from rules_requirements.case_keys import CaseKey, nodeid_to_case_path
from rules_requirements.migrate import NONE, OPEN

# Directories never copied into the scratch tree: version control, Bazel's
# convenience symlinks, build and tool caches, vendored trees.
_SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    "node_modules",
    "site-packages",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".nox",
}


@dataclass
class Offender:
    """One item whose collection the rewrite would change."""

    item: str  # the nodeid, or a decided case path matched by nothing
    before: Optional[list[str]]
    after: Optional[list[str]]
    expected: Optional[list[str]]  # the owner's id(s); None for an undecided item
    reason: str

    def describe(self) -> str:
        parts = [f"before={self.before}", f"after={self.after}"]
        if self.expected is not None:
            parts.append(f"expected={self.expected}")
        return f"{self.item}: {self.reason} ({', '.join(parts)})"


@dataclass
class CollectCheck:
    """The outcome of the dynamic collection check."""

    ok: bool
    offenders: list[Offender] = field(default_factory=list)
    error: str = ""  # a collection that could not run; ``output`` has the detail
    output: str = ""

    @property
    def failed_to_run(self) -> bool:
        return bool(self.error)


def _should_skip_dir(name: str) -> bool:
    return name in _SKIP_DIRS or name.startswith("bazel-") or "venv" in name


def _copy_tree(src: str, dst: str) -> None:
    """Copy ``src`` to ``dst``, skipping VCS/cache dirs, ``bazel-*`` symlinks
    and every symlink (so ``bazel-out`` and the like are not followed)."""
    for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
        dirnames[:] = [d for d in dirnames if not _should_skip_dir(d) and not os.path.islink(os.path.join(dirpath, d))]
        rel = os.path.relpath(dirpath, src)
        target_dir = dst if rel == "." else os.path.join(dst, rel)
        os.makedirs(target_dir, exist_ok=True)
        for name in filenames:
            source = os.path.join(dirpath, name)
            if os.path.islink(source):
                continue
            try:
                shutil.copy2(source, os.path.join(target_dir, name))
            except OSError:
                pass  # a socket, a vanished file: nothing pytest would import


def _rr_pythonpath() -> str:
    """The directory that makes ``rules_requirements`` importable (``python/``)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(rules_requirements.__file__)))


def _collect(
    tree: str, python: str, pytest_args: list[str], dump_path: str
) -> tuple[Optional[dict[str, list[str]]], str, int]:
    """Collect ``tree`` with the dump plugin; return (items or None, output, rc)."""
    if os.path.exists(dump_path):
        os.remove(dump_path)
    env = dict(os.environ)
    parts = [tree, _rr_pythonpath()]
    if env.get("PYTHONPATH"):
        parts.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(parts)
    env["RR_COLLECT_DUMP"] = dump_path
    cmd = [
        python,
        "-m",
        "pytest",
        "--collect-only",
        "-q",
        "-p",
        "no:cacheprovider",
        "-p",
        "rules_requirements.hooks.collect_dump",
        *pytest_args,
        ".",
    ]
    proc = subprocess.run(cmd, cwd=tree, env=env, capture_output=True, text=True)
    output = proc.stdout + proc.stderr
    items: Optional[dict[str, list[str]]] = None
    if os.path.exists(dump_path):
        try:
            with open(dump_path, encoding="utf-8") as fh:
                loaded = json.load(fh)
            got = loaded.get("items")
            if isinstance(got, dict):
                items = {str(k): list(v) for k, v in got.items()}
        except (OSError, ValueError):
            items = None
    return items, output, proc.returncode


def _decided_paths(decided: Mapping[CaseKey, str]) -> dict[str, list[str]]:
    """Each decided case path -> the id(s) the case must end with (none -> [])."""
    out: dict[str, list[str]] = {}
    for key, owner in decided.items():
        if owner == OPEN:
            continue  # undecided: handled as an "every other item" case
        out[key.path] = [] if owner == NONE else [owner]
    return out


def compare(
    before: Mapping[str, list[str]],
    after: Mapping[str, list[str]],
    decided: Mapping[CaseKey, str],
) -> list[Offender]:
    """Items the rewrite would collect differently than intended."""
    offenders: list[Offender] = []
    want = _decided_paths(decided)

    after_by_path: dict[str, list[str]] = {}
    for nid in after:
        after_by_path.setdefault(nodeid_to_case_path(nid), []).append(nid)

    # The set of collected nodeids must not change.
    for nid in sorted(set(before) ^ set(after)):
        where = "the original tree only" if nid in before else "the rewritten tree only"
        offenders.append(
            Offender(nid, before.get(nid), after.get(nid), None, f"collected in {where} (the nodeid set changed)")
        )

    # Each decided case must end with exactly its owner, and must be collected.
    for path, expected in sorted(want.items()):
        nids = after_by_path.get(path, [])
        if not nids:
            offenders.append(Offender(path, None, None, expected, "a decided case matched no collected item"))
            continue
        for nid in sorted(nids):
            if after.get(nid) != expected:
                offenders.append(
                    Offender(
                        nid, before.get(nid), after.get(nid), expected, "a decided case does not end with its owner"
                    )
                )

    # Every other (undecided) item must keep exactly the ids it had.
    for nid in sorted(after):
        if nodeid_to_case_path(nid) in want:
            continue
        if nid in before and after[nid] != before[nid]:
            offenders.append(Offender(nid, before[nid], after[nid], None, "an undecided item's ids changed"))
    return offenders


def check(
    root: str,
    rewrites: Mapping[str, tuple[str, str]],
    decided: Mapping[CaseKey, str],
    *,
    python: str = "",
    pytest_args: Iterable[str] = (),
    scratch: str = "",
) -> CollectCheck:
    """Run the dynamic collection check for a rewrite.

    ``rewrites`` maps each relative path to ``(new_text, encoding)`` for the
    files apply would write; ``decided`` is the worksheet's ``CaseKey -> id |
    "none" | "?"``. ``python`` (default :data:`sys.executable`) is the
    interpreter whose pytest and project dependencies collect the tests.
    """
    python = python or sys.executable
    extra = list(pytest_args)
    tmp = tempfile.mkdtemp(prefix="rr-collect-", dir=scratch or None)
    try:
        copy = os.path.join(tmp, "after")
        _copy_tree(root, copy)
        for rel, (new_text, encoding) in rewrites.items():
            dest = os.path.join(copy, rel)
            os.makedirs(os.path.dirname(dest) or copy, exist_ok=True)
            with open(dest, "w", encoding=encoding, newline="") as fh:
                fh.write(new_text)

        before, before_out, before_rc = _collect(root, python, extra, os.path.join(tmp, "before.json"))
        after, after_out, after_rc = _collect(copy, python, extra, os.path.join(tmp, "after.json"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    has_decided = any(owner != OPEN for owner in decided.values())
    for label, items, out, rc in (
        ("original", before, before_out, before_rc),
        ("rewritten", after, after_out, after_rc),
    ):
        if items is None:
            reason = "the dump plugin wrote no items (not loaded?)" if rc == 0 else f"pytest --collect-only exited {rc}"
            return CollectCheck(False, error=f"collection failed in the {label} tree: {reason}", output=out)
        if rc != 0:
            return CollectCheck(
                False, error=f"collection failed in the {label} tree: pytest --collect-only exited {rc}", output=out
            )
        if has_decided and not items:
            return CollectCheck(
                False,
                error=f"collection found no tests in the {label} tree, but the worksheet has decided cases",
                output=out,
            )

    assert before is not None and after is not None  # guarded above
    offenders = compare(before, after, decided)
    return CollectCheck(not offenders, offenders=offenders)
