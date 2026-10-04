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
* compare: each decided case the written files settle must end with exactly
  its owner (no id for ``none``), every other item must keep exactly the ids
  it had, and the set of collected nodeids must not change.

The check vouches for what collects in the environment it runs in (the
``--python`` interpreter, ``os.environ``): a test that only exists under
another environment, or a module skipped at collection time, is not seen.
Collection-time skips are reported (:attr:`CollectCheck.skipped`).

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

# Directories never copied into the scratch tree: version control, build and
# tool caches, vendored trees. Bazel's convenience symlinks (``bazel-*``) and
# virtualenvs (a directory holding ``pyvenv.cfg``) are skipped too. Whatever is
# skipped is also ``--ignore``d in BOTH collections, so the two trees are
# collected over the same files. Every OTHER symlink is NOT skipped silently:
# one resolving inside the tree is recreated in the copy so both collections
# follow it, and one resolving outside the tree makes apply refuse (a real
# ``pytest`` run would follow it, changing attribution the check cannot cover).
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
    # Collectors pytest skipped at collection time (``importorskip``, a
    # module-level ``pytest.skip``), nodeid -> reason: their tests were not
    # collected in the check's environment, so the check could not see them.
    skipped: dict[str, str] = field(default_factory=dict)

    @property
    def failed_to_run(self) -> bool:
        return bool(self.error)


def _skipped_dir(path: str, name: str) -> bool:
    return name in _SKIP_DIRS or os.path.isfile(os.path.join(path, "pyvenv.cfg"))


def _is_bazel_link(name: str) -> bool:
    """A Bazel convenience symlink (``bazel-out``, ``bazel-bin``, ``bazel-<ws>``)."""
    return name.startswith("bazel-")


def _within(path: str, root_real: str) -> bool:
    """Whether ``path`` resolves to ``root_real`` or somewhere beneath it."""
    real = os.path.realpath(path)
    return real == root_real or real.startswith(root_real + os.sep)


def _copy_tree(src: str, dst: str) -> tuple[list[str], list[tuple[str, str]]]:
    """Copy ``src`` to ``dst``; returns ``(ignored, offenders)``.

    VCS/cache dirs, virtualenvs and Bazel convenience symlinks (``bazel-*``) are
    not copied and are listed in ``ignored`` (relative, ``/``-separated), which
    the caller ``--ignore``s in BOTH collections so the two trees collect over
    the same files.

    No OTHER symlink is skipped silently, because a real ``pytest`` run from
    ``src`` would follow it:

    * one whose target resolves INSIDE ``src`` is recreated in the copy
      (retargeted into the copy), so both collections follow it and the
      collection check covers whatever it reaches;
    * one whose target resolves OUTSIDE ``src`` and could be collected (a
      directory, or a ``.py`` file) is returned in ``offenders`` as
      ``(path, realpath)``: the caller fails closed on it. An out-of-tree
      symlink to a non-``.py`` file is only ``ignored`` (pytest would not
      collect it)."""
    ignored: list[str] = []
    offenders: list[tuple[str, str]] = []
    src_real = os.path.realpath(src)

    def rel_of(rel: str, name: str) -> str:
        return os.path.normpath(os.path.join(rel, name)).replace(os.sep, "/")

    def recreate(source: str, link: str) -> None:
        real = os.path.realpath(source)
        target = os.path.join(dst, os.path.relpath(real, src_real))
        try:
            os.symlink(target, link, target_is_directory=os.path.isdir(real))
        except (OSError, NotImplementedError):  # pragma: no cover - platform without symlinks
            offenders.append((os.path.relpath(source, src).replace(os.sep, "/"), real))

    for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
        rel = os.path.relpath(dirpath, src)
        target_dir = dst if rel == "." else os.path.join(dst, rel)
        os.makedirs(target_dir, exist_ok=True)
        keep = []
        for d in dirnames:
            full = os.path.join(dirpath, d)
            relpath = rel_of(rel, d)
            if os.path.islink(full):
                if _is_bazel_link(d) or _skipped_dir(full, d):
                    ignored.append(relpath)  # bazel convenience symlink or a cache/venv
                elif _within(full, src_real):
                    recreate(full, os.path.join(target_dir, d))  # covered by the copy
                else:
                    offenders.append((relpath, os.path.realpath(full)))  # fail closed
                continue  # never recurse into a symlinked directory
            if _skipped_dir(full, d):
                if d != "__pycache__":
                    ignored.append(relpath)
            else:
                keep.append(d)
        dirnames[:] = keep
        for name in filenames:
            source = os.path.join(dirpath, name)
            relpath = rel_of(rel, name)
            if os.path.islink(source):
                if _is_bazel_link(name):
                    ignored.append(relpath)
                elif _within(source, src_real):
                    recreate(source, os.path.join(target_dir, name))  # covered by the copy
                elif name.endswith(".py"):
                    offenders.append((relpath, os.path.realpath(source)))  # fail closed
                else:
                    ignored.append(relpath)  # out-of-tree data file: pytest would not collect it
                continue
            try:
                shutil.copy2(source, os.path.join(target_dir, name))
            except OSError:
                pass  # a socket, a vanished file: nothing pytest would import
    return sorted(ignored), sorted(offenders)


def _rr_pythonpath(tmp: str) -> str:
    """A directory holding ONLY ``rules_requirements`` (a symlink to this
    package, or a copy), for the collection subprocess's ``PYTHONPATH``.

    Never the directory this package was imported from: for a pip/pipx
    install that is rr's whole ``site-packages``, which would put rr's own
    pytest, pluggy and pytest11 plugins ahead of the project interpreter's."""
    path = os.path.join(tmp, "rrpath")
    os.makedirs(path, exist_ok=True)
    package = os.path.dirname(os.path.abspath(rules_requirements.__file__))
    link = os.path.join(path, "rules_requirements")
    try:
        os.symlink(package, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        shutil.copytree(package, link, ignore=shutil.ignore_patterns("__pycache__"))
    return path


def _collect(
    tree: str, python: str, pytest_args: list[str], dump_path: str, rr_path: str
) -> tuple[Optional[dict[str, list[str]]], dict[str, str], list[str], str, int]:
    """Collect ``tree`` with the dump plugin: (items or None, skipped, duplicates, output, rc).

    ``duplicates`` names any nodeid the dump recorded more than once (two
    collected items the check cannot tell apart). No path argument is passed,
    so pytest picks what to collect exactly as a plain ``pytest`` run from
    ``tree`` would (``testpaths`` included)."""
    if os.path.exists(dump_path):
        os.remove(dump_path)
    env = dict(os.environ)
    parts = [tree, rr_path]
    if env.get("PYTHONPATH"):
        parts.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(parts)
    env["PYTHONDONTWRITEBYTECODE"] = "1"  # leave no __pycache__ in the user's tree
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
    ]
    proc = subprocess.run(cmd, cwd=tree, env=env, capture_output=True, text=True)
    output = proc.stdout + proc.stderr
    items: Optional[dict[str, list[str]]] = None
    skipped: dict[str, str] = {}
    duplicates: list[str] = []
    if os.path.exists(dump_path):
        try:
            with open(dump_path, encoding="utf-8") as fh:
                loaded = json.load(fh)
            got = loaded.get("items")
            if isinstance(got, list):
                items = {}
                seen: set[str] = set()
                dups: set[str] = set()
                for pair in got:
                    nid, ids = str(pair[0]), list(pair[1])
                    if nid in seen:
                        dups.add(nid)
                    seen.add(nid)
                    items[nid] = ids
                duplicates = sorted(dups)
            sk = loaded.get("skipped")
            if isinstance(sk, dict):
                skipped = {str(k): str(v) for k, v in sk.items()}
        except (OSError, ValueError, TypeError, IndexError):
            items = None
    return items, skipped, duplicates, output, proc.returncode


def _relocate(items: Mapping[str, list[str]], copy: str, root: str) -> dict[str, list[str]]:
    """Map the copy's absolute path back to the original's inside nodeids
    (a parametrization id built from ``__file__``, say), so a path the test
    itself computes does not read as a changed nodeid."""
    pairs = sorted(
        {(copy, root), (os.path.realpath(copy), os.path.realpath(root))}, key=lambda p: len(p[0]), reverse=True
    )
    out: dict[str, list[str]] = {}
    for nid, ids in items.items():
        for old, new in pairs:
            nid = nid.replace(old, new)
        out[nid] = ids
    return out


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
    files apply would write; ``decided`` holds the decided cases those files
    settle (``CaseKey -> id | "none"``, see
    :meth:`~rules_requirements.tag_codemod.ApplyResult.settled`). Every other
    item -- undecided, left to ``verified_by``, in a file held back, refused
    or outside ``--only`` -- must keep exactly its before-ids. ``python``
    (default :data:`sys.executable`) is the interpreter whose pytest and
    project dependencies collect the tests.
    """
    python = python or sys.executable
    extra = list(pytest_args)
    root = os.path.abspath(root)
    tmp = tempfile.mkdtemp(prefix="rr-collect-", dir=scratch or None)
    try:
        copy = os.path.join(tmp, "after")
        ignored, symlink_offenders = _copy_tree(root, copy)
        if symlink_offenders:
            listing = ", ".join(f"{p} -> {real}" for p, real in symlink_offenders)
            return CollectCheck(
                False,
                error=(
                    "a symlink reaches a test outside the tree, which a real pytest run would follow "
                    f"but the collection check cannot cover: {listing}"
                ),
            )
        for rel, (new_text, encoding) in rewrites.items():
            dest = os.path.join(copy, rel)
            os.makedirs(os.path.dirname(dest) or copy, exist_ok=True)
            with open(dest, "w", encoding=encoding, newline="") as fh:
                fh.write(new_text)
        rr_path = _rr_pythonpath(tmp)
        extra = [*(f"--ignore={p}" for p in ignored), *extra]
        before, before_skipped, before_dups, before_out, before_rc = _collect(
            root, python, extra, os.path.join(tmp, "before.json"), rr_path
        )
        after, after_skipped, after_dups, after_out, after_rc = _collect(
            copy, python, extra, os.path.join(tmp, "after.json"), rr_path
        )
        if after is not None:
            after = _relocate(after, copy, root)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    skipped = {**after_skipped, **before_skipped}
    has_decided = any(owner != OPEN for owner in decided.values())
    for label, items, dups, out, rc in (
        ("original", before, before_dups, before_out, before_rc),
        ("rewritten", after, after_dups, after_out, after_rc),
    ):
        if items is None:
            reason = "the dump plugin wrote no items (not loaded?)" if rc == 0 else f"pytest --collect-only exited {rc}"
            return CollectCheck(False, error=f"collection failed in the {label} tree: {reason}", output=out)
        if rc != 0:
            return CollectCheck(
                False, error=f"collection failed in the {label} tree: pytest --collect-only exited {rc}", output=out
            )
        if dups:
            return CollectCheck(
                False,
                error=(
                    f"the {label} collection has the nodeid {dups[0]!r} more than once, so the check cannot tell "
                    "the two cases apart"
                ),
                output=out,
            )
        if has_decided and not items:
            return CollectCheck(
                False,
                error=f"collection found no tests in the {label} tree, but the worksheet has decided cases",
                output=out,
            )

    assert before is not None and after is not None  # guarded above
    offenders = compare(before, after, decided)
    return CollectCheck(not offenders, offenders=offenders, skipped=skipped)
