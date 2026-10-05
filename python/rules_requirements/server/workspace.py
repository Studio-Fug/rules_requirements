# SPDX-License-Identifier: AGPL-3.0-or-later
"""The web editor's view of a repository: model files, evidence, annotations
and git history, with every mutation going through surgical text edits.

All paths handed out or accepted are relative to the workspace root; anything
resolving outside it (or into ``.git``) is refused.

**A test case verifies at most one requirement.** Every save builds the model
the edit would produce and runs the static claim checks
(:func:`~rules_requirements.validate.validate`) and
:func:`~rules_requirements.attribution.attribute` over the loaded evidence
before anything is written. An edit that would let one test case verify two
entities (``shared-case``, ``same-code-multiple-owners``, a new
``attribution-conflict``), break a selector or a target (``bad-selector``,
``bad-target``) or contradict the verification-set lock
(``lock-owner-changed``) is refused with a 409 that names the case and its
current owner (:class:`Conflict`). Which entity owns a case is read from the
:class:`~rules_requirements.attribution.Attribution` alone; the editor never
derives it from tags or targets.
"""

from __future__ import annotations

import datetime as _dt
import difflib
import hashlib
import json
import os
import re
import subprocess
import tempfile
import threading
from collections import Counter
from dataclasses import dataclass, field, replace
from typing import Any, Collection, Iterable, Mapping

from rules_requirements import annotations as rr_annotations
from rules_requirements import case_selectors, edit, ingest, labels
from rules_requirements import config as cfg
from rules_requirements import lock as rr_lock
from rules_requirements._vendor import yaml
from rules_requirements.attribution import (
    MULTI_TAG,
    OWNED_STATES,
    VIA_TAG,
    Attribution,
    Member,
    attribute,
)
from rules_requirements.case_keys import SYNTHETIC_PATH, CaseKey
from rules_requirements.diff import EntityChange, diff_models
from rules_requirements.model import (
    CLAIM_FIELDS,
    Claim,
    Model,
    Note,
    load_text,
    model_files,
    parse_documents,
    read_model,
)
from rules_requirements.trace import Matrix, build_matrix
from rules_requirements.util import natural_key
from rules_requirements.validate import Issue, claim_conflicts, validate


class WorkspaceError(Exception):
    """A request the workspace refuses (bad input, conflict, unsafe path).

    ``data`` is extra JSON the API returns with the message (the conflicts of
    a refused save)."""

    def __init__(self, message: str, status: int = 400, data: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.status = status
        self.data = dict(data or {})


# Problems a save may not introduce (refused with 409). Hard errors of the
# one-owner rule: none of them can be configured off.
GUARDED = ("shared-case", "same-code-multiple-owners", "bad-selector", "bad-target", "lock-owner-changed")
REPORT_TIME = ("attribution-conflict", "same-code-multiple-owners")  # quarantines a save may not introduce


@dataclass(frozen=True)
class Conflict:
    """A problem an edit would introduce: why the save guard refuses it.

    ``case`` is the test case that would get two owners (``<target>#<path>``,
    or ``<target>`` with ``any case`` when every case of it would), and
    ``owner`` its owner now: from the attribution (``owner_via``
    ``attribution``), the lock (``lock``), or the one claim that selects it
    today (``claim``); "" when nothing owns it yet."""

    code: str
    message: str
    case: str = ""
    entities: tuple[str, ...] = ()
    owner: str = ""
    owner_via: str = ""

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.case:
            out["case"] = self.case
        if self.entities:
            out["entities"] = list(self.entities)
        if self.owner:
            out["owner"] = self.owner
            out["owner_via"] = self.owner_via
        return out


@dataclass
class Check:
    """What :meth:`Workspace.check` found for a candidate edit."""

    conflicts: list[Conflict]
    model: Model | None = None
    attribution: Attribution | None = None
    issues: list[Issue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.conflicts


@dataclass
class Snapshot:
    model: Model
    issues: list[Issue]
    warnings: list[str]
    matrix: Matrix
    references: list[rr_annotations.Reference] | None
    signature: tuple[Any, ...] = ()

    @property
    def attribution(self) -> Attribution | None:
        """Who owns each test case: :func:`~rules_requirements.attribution.attribute`,
        as :func:`~rules_requirements.trace.build_matrix` ran it."""
        return self.matrix.attribution


_REF_OK = re.compile(r"^[A-Za-z0-9._/@{}~^-]+$")


@dataclass
class Workspace:
    root: str
    model_paths: list[str]
    evidence_paths: list[str] = field(default_factory=list)
    current_build: Mapping[str, str] = field(default_factory=dict)
    scan: bool = True
    author: str = ""

    def __post_init__(self) -> None:
        self.root = os.path.realpath(self.root)
        self.model_paths = [self._rel(p) for p in self.model_paths]
        self.evidence_paths = [p if os.path.isabs(p) else os.path.join(self.root, p) for p in self.evidence_paths]
        self._lock = threading.RLock()
        self._snapshot: Snapshot | None = None
        self._evidence: ingest.Evidence | None = None
        self._refs: list[rr_annotations.Reference] | None = None

    # ------------------------------------------------------------------ paths

    def _rel(self, path: str) -> str:
        full = os.path.realpath(path if os.path.isabs(path) else os.path.join(self.root, path))
        if full != self.root and not full.startswith(self.root + os.sep):
            raise WorkspaceError(f"{path} is outside the workspace", 403)
        return os.path.relpath(full, self.root).replace(os.sep, "/")

    def abspath(self, rel: str) -> str:
        rel = self._rel(rel)
        if ".git" in rel.split("/"):
            raise WorkspaceError("refusing to touch .git", 403)
        return os.path.join(self.root, rel)

    def files(self) -> list[str]:
        return [self._rel(p) for p in model_files([os.path.join(self.root, p) for p in self.model_paths])]

    # ------------------------------------------------------------------ model

    def _signature(self) -> tuple[Any, ...]:
        sig = []
        for rel in self.files():
            try:
                st = os.stat(os.path.join(self.root, rel))
                sig.append((rel, st.st_mtime_ns, st.st_size))
            except OSError:
                sig.append((rel, 0, 0))
        return tuple(sig)

    def snapshot(self, refresh: bool = False) -> Snapshot:
        """The current model, validation and trace matrix (cached until files change)."""
        with self._lock:
            sig = self._signature()
            if refresh:
                self._evidence = None
                self._refs = None
            if self._snapshot is not None and self._snapshot.signature == sig and not refresh:
                return self._snapshot
            model, warnings = read_model([os.path.join(self.root, p) for p in self.model_paths], root=self.root)
            issues = validate(model)
            if self._evidence is None:
                self._evidence = ingest.collect(self.evidence_paths) if self.evidence_paths else ingest.Evidence()
            if self.scan and self._refs is None:
                self._refs = rr_annotations.scan(self.root, model.config)
            matrix = build_matrix(model, self._evidence, current_build=self.current_build, references=self._refs)
            self._snapshot = Snapshot(model, issues, warnings, matrix, self._refs, sig)
            return self._snapshot

    @property
    def model(self) -> Model:
        return self.snapshot().model

    # ------------------------------------------------------------------ edits

    def _load(self, rel: str) -> str | None:
        """The file's text exactly as stored (None if it does not exist)."""
        try:
            with open(self.abspath(rel), encoding="utf-8", newline="") as fh:
                return fh.read()
        except FileNotFoundError:
            return None

    def _read(self, rel: str) -> str:
        return (self._load(rel) or "").replace("\r\n", "\n")

    def _txn(self) -> _Transaction:
        return _Transaction(self)

    @staticmethod
    def version(ent: Any) -> str:
        """A short fingerprint of an entity's content (optimistic concurrency)."""
        blob = json.dumps(edit.entity_to_dict(ent), sort_keys=True, ensure_ascii=False)
        return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]  # noqa: S324 — not security relevant

    def next_id(self, kind: str) -> str:
        model = self.model
        prefix = model.config.prefix(kind)
        rx = re.compile(re.escape(prefix) + r"-(\d+)$")
        nums = [m.group(1) for e in model.section(kind).values() if (m := rx.match(e.id))]
        width = max((len(n) for n in nums if n.startswith("0")), default=0)
        return f"{prefix}-{(max(int(n) for n in nums) + 1 if nums else 1):0{width}d}"

    def file_for_new(self, kind: str) -> str:
        """Where a new entity of ``kind`` goes: next to its siblings.

        One-object-per-file layouts get a new file in the siblings' directory;
        section files get the entity appended to the file holding most of the
        kind (or, with no siblings, the first model file).
        """
        model = self.model
        siblings = list(model.section(kind).values())
        counts: dict[str, int] = {}
        for ent in siblings:
            counts[ent.location.path] = counts.get(ent.location.path, 0) + 1
        if counts:
            best = max(sorted(counts), key=lambda p: counts[p])
            if counts[best] == 1 and self._single_object(best):
                return os.path.dirname(best)  # a directory: one file per object
            return best
        files = [f for f in self.files() if not f.endswith(".json")]
        if files:
            return files[0]
        base = self.model_paths[0] if self.model_paths else "requirements"
        return base if base.endswith((".yaml", ".yml")) else f"{base}/requirements.yaml"

    def _single_object(self, rel: str) -> bool:
        return bool(re.search(r"^kind:\s", self._read(rel), re.M))

    def _covered(self, rel: str) -> bool:
        return any(rel == p or rel.startswith(p.rstrip("/") + "/") for p in self.model_paths)

    def _loadable(self, rel: str, exts: tuple[str, ...] = (".yaml", ".yml")) -> bool:
        """Whether the model loader would read ``rel`` (see ``model_files``):
        a model path itself, or a file with one of ``exts`` under a model
        directory that is not inside a dot-directory."""
        for p in self.model_paths:
            if rel == p:
                return True
            sub = rel if p == "." else rel[len(p) + 1 :] if rel.startswith(p.rstrip("/") + "/") else ""
            if sub and rel.endswith(exts) and not any(x.startswith(".") for x in sub.split("/")[:-1]):
                return True
        return False

    def create(self, kind: str, data: Mapping[str, Any], file: str = "") -> str:
        with self._lock:
            txn, eid = self._create_txn(kind, data, file)
            txn.commit()
            return eid

    def _create_txn(self, kind: str, data: Mapping[str, Any], file: str = "") -> tuple[_Transaction, str]:
        if kind not in cfg.KINDS:
            raise WorkspaceError(f"unknown kind {kind!r}")
        data = dict(data)
        data["id"] = str(data.get("id") or self.next_id(kind)).strip()
        if self.model.get(data["id"]) is not None:
            raise WorkspaceError(f"{data['id']} already exists", 409)
        ent, problems = edit.dict_to_entity(kind, data)
        if ent is None or problems:
            raise WorkspaceError("; ".join(problems) or "invalid entity")
        if not self.model.config.id_regex(kind).match(ent.id):
            raise WorkspaceError(f"{ent.id} does not match the {kind} id pattern")
        target = self._rel(file) if file else self.file_for_new(kind)
        named = target.endswith((".yaml", ".yml", ".json"))  # a file, not a folder to put one in
        if not named:
            target = f"{target}/{ent.id}.yaml"
        if not self._loadable(target):
            raise WorkspaceError(
                f"{target} would not be read as part of the model (model paths: {', '.join(self.model_paths)};"
                " files in dot-directories are skipped)"
            )
        txn = self._txn()
        exists = os.path.exists(self.abspath(target))
        want = {ent.id: {**data, "kind": kind}}
        if exists and not self._single_object(target):
            txn.edit(target, lambda text: edit.insert_entity(text, kind, edit.entity_to_dict(ent)), want)
        elif exists and named and file:  # asked for this one-object file: one more document in it
            txn.edit(target, lambda text: edit.append_document(text, kind, edit.entity_to_dict(ent)), want)
        elif exists:
            raise WorkspaceError(f"{target} already exists", 409)
        else:
            txn.edit(target, lambda _text: edit.render_file(kind, edit.entity_to_dict(ent)), want)
        return txn, ent.id

    def update(self, entity_id: str, data: Mapping[str, Any], version: str = "") -> None:
        """Replace an entity's fields with ``data``.

        ``notes`` are kept unless ``data`` names them (the form editor does
        not); with ``version`` (from :func:`entity_payload`) a concurrent change
        since the caller loaded the entity is a 409 instead of being lost.
        """
        with self._lock:
            self._update_txn(entity_id, data, version).commit()

    def _update_txn(self, entity_id: str, data: Mapping[str, Any], version: str = "") -> _Transaction:
        ent = self.model.get(entity_id)
        if ent is None:
            raise WorkspaceError(f"{entity_id} does not exist", 404)
        if version and version != self.version(ent):
            raise WorkspaceError(f"{entity_id} changed since it was loaded; reload and reapply your edit", 409)
        data = dict(data)
        if str(data.get("id", entity_id)) != entity_id:
            raise WorkspaceError("use rename to change an id")
        data["id"] = entity_id
        if "notes" not in data and ent.notes:
            data["notes"] = edit.entity_to_dict(ent).get("notes", [])
        new, problems = edit.dict_to_entity(ent.kind, data)
        if new is None or problems:
            raise WorkspaceError("; ".join(problems) or "invalid entity")
        txn = self._txn()
        txn.edit(
            ent.location.path,
            lambda t: edit.update_entity(t, entity_id, edit.entity_to_dict(new)),
            {entity_id: {**data, "kind": ent.kind}},
        )
        return txn

    # ------------------------------------------------------------------ the save guard

    def precheck(self, entity_id: str, data: Mapping[str, Any], kind: str = "", file: str = "") -> dict[str, Any]:
        """A dry run of saving ``data`` as ``entity_id`` (an update when it
        exists, else a create of ``kind``): the problems the save guard would
        refuse it for, the validation issues of the entity, and its
        verification set as attribution would compute it. Nothing is written."""
        with self._lock:
            existing = self.model.get(entity_id) if entity_id else None
            try:
                if existing is not None and (not kind or kind == existing.kind):
                    txn = self._update_txn(entity_id, data)
                else:
                    txn, entity_id = self._create_txn(kind, {**data, "id": entity_id or data.get("id", "")}, file)
                result = txn.check()
            except WorkspaceError as exc:
                problems = exc.data.get("conflicts") or [{"code": "invalid", "message": str(exc)}]
                return {"ok": False, "id": entity_id, "problems": problems, "issues": [], "members": [], "set": {}}
        members = result.attribution.members_of(entity_id) if result.attribution is not None else ()
        return {
            "ok": result.ok,
            "id": entity_id,
            "problems": [c.to_dict() for c in result.conflicts],
            "issues": [_issue(i) for i in result.issues if i.entity == entity_id],
            "members": [_member(m) for m in members],
            "set": _set_summary(members),
            "selectors": _selector_counts(members),
        }

    def candidate(self, txn: _Transaction) -> Model:
        """The model as it would read with ``txn`` written."""
        texts: dict[str, str | None] = {}
        for rel, text in txn.texts.items():
            dst = txn.moves.get(rel, rel)
            if edit.is_blank(text) and txn.raw.get(rel) is not None:
                texts[rel] = None  # removed: nothing left in it
                continue
            if dst != rel:
                texts[rel] = None
            texts[dst] = text
        order = list(self.files())
        order += sorted(r for r, t in texts.items() if t is not None and r not in order and self._loadable(r))
        docs: list[tuple[str, Any]] = []
        errors: list[str] = []
        for rel in order:
            content = texts[rel] if rel in texts else self._read(rel)
            if content is None:
                continue
            try:
                docs.extend((rel, d) for d in load_text(content))
            except yaml.YAMLError as exc:
                errors.append(f"{rel}: cannot load: {exc}")
        model, _ = parse_documents(docs)
        model = replace(model, root=self.root)
        if errors:
            model = replace(model, parse_errors=tuple(errors) + model.parse_errors)
        return model

    def check(
        self,
        candidate: Model,
        *,
        edited: Collection[str] = (),
        aliases: Mapping[str, str] | None = None,
        lock: rr_lock.Lock | str | None = "configured",
    ) -> Check:
        """What saving ``candidate`` over the current model would introduce.

        Only *new* problems count, so an edit is never blocked by a conflict
        it did not cause (it is still reported by validation):

        * a pair of claims of two entities that can select one case (the
          ``shared-case`` and ``same-code-multiple-owners`` witnesses of
          :func:`~rules_requirements.validate.claim_conflicts`);
        * a ``bad-selector`` or ``bad-target`` of an ``edited`` entity;
        * a lock entry another entity's claims would select (``lock-owner-changed``);
        * a case :func:`~rules_requirements.attribution.attribute` would
          quarantine over the loaded evidence (``attribution-conflict``,
          ``same-code-multiple-owners`` by source file).

        ``aliases`` maps renamed ids (new -> old); ``lock`` is the lock the
        save would leave (by default the configured one, read from disk).
        """
        snap = self.snapshot()
        before_att = snap.attribution
        back = dict(aliases or {})

        def old(entity: str) -> str:
            return back.get(entity, entity)

        conflicts: list[Conflict] = []
        # Static: pairs of claims that can select one case.
        before_claims = {(c.entity, c.target, c.pattern) for c in snap.model.claims()}
        before_pairs = {
            (c.code, frozenset((x.entity, x.target, x.pattern) for x in (c.first, c.second)))
            for c in claim_conflicts(snap.model)
        }
        for pair in claim_conflicts(candidate):
            ident = (pair.code, frozenset((old(x.entity), x.target, x.pattern) for x in (pair.first, pair.second)))
            if ident not in before_pairs:
                conflicts.append(self._pair_conflict(pair, before_att, before_claims, old))
        # Validation of the edited entities: selectors and targets.
        issues = validate(candidate)
        edited_old = {old(e) for e in edited}

        def shape(i: Issue, entity: str) -> tuple[str, str, str]:
            msg = i.message[len(i.entity) :] if i.entity and i.message.startswith(i.entity) else i.message
            return (i.code, entity, msg)

        seen = Counter(shape(i, i.entity) for i in snap.issues if i.code in ("bad-selector", "bad-target"))
        for i in issues:
            if i.code not in ("bad-selector", "bad-target") or old(i.entity) not in edited_old:
                continue
            k = shape(i, old(i.entity))
            if seen[k]:
                seen[k] -= 1
                continue
            conflicts.append(Conflict(i.code, i.message, entities=(i.entity,)))
        # The lock: a locked case another entity's claims would select.
        if lock == "configured":
            try:
                lock = rr_lock.configured_lock(candidate)
            except rr_lock.LockError:
                lock = None
        assert not isinstance(lock, str)
        conflicts += self._lock_conflicts(candidate, lock, snap.model, before_att.lock if before_att else None, old)
        # Report time: what attribution would quarantine over this evidence.
        after_att: Attribution | None = None
        if not any(i.code == "duplicate-id" for i in issues):
            try:
                after_att = attribute(
                    candidate, self._evidence or ingest.Evidence(), current_build=self.current_build, lock=lock
                )
            except ValueError:
                after_att = None
        named = {c.case for c in conflicts}
        for q in after_att.quarantined if after_att is not None else ():
            if q.code not in REPORT_TIME or str(q.key) in named:
                continue
            was = before_att.quarantine_of(q.key) if before_att is not None else None
            if was is not None and {old(e) for e in q.entities} <= set(was.entities):
                continue
            owner = before_att.owner_of(q.key) if before_att is not None else None
            now = f" (it is owned by {owner} now)" if owner else ""
            who = " and ".join(q.entities)
            if q.code == REPORT_TIME[0]:
                msg = f"this would make {q.key} verify both {who}{now}; a test case verifies at most one requirement"
            else:
                msg = (
                    f"this would make {q.key} and the same test code in another target verify {who}{now}; "
                    "a test case verifies at most one requirement"
                )
            conflicts.append(Conflict(q.code, msg, str(q.key), q.entities, owner or "", "attribution" if owner else ""))
        return Check(conflicts, candidate, after_att, issues)

    def _pair_conflict(
        self,
        pair: Any,
        att: Attribution | None,
        before_claims: set[tuple[str, str, str | None]],
        old: Any,
    ) -> Conflict:
        """The conflict of a new pair of claims: the case both select, and who owns it now."""
        first, second = pair.first, pair.second
        if (old(second.entity), second.target, second.pattern) in before_claims and (
            old(first.entity),
            first.target,
            first.pattern,
        ) not in before_claims:
            first, second = second, first  # `first` is the claim that was there before
        key: CaseKey | None = None
        if pair.example:
            key = CaseKey(second.target, pair.example)
        else:
            for k in sorted(att.cases, key=lambda k: natural_key(k.path)) if att is not None else ():
                if k.target == second.target and second.matches(k.path) and first.matches(k.path):
                    key = k
                    break
            if key is None and pair.example is None:
                key = CaseKey(second.target, SYNTHETIC_PATH)
        case = str(key) if key is not None else f"{second.target} (any case)"
        owner, via = "", ""
        here = CaseKey(first.target, key.path) if key is not None else None
        if att is not None and here is not None and here in att.owner:
            owner, via = att.owner[here], "attribution"
        elif (old(first.entity), first.target, first.pattern) in before_claims:
            owner, via = first.entity, "claim"
        now = {"attribution": f" (it is owned by {owner} now)", "claim": f" ({owner} claims it now)"}.get(via, "")
        if pair.code == "shared-case":
            msg = (
                f"this would make {case} verify both {first.entity} and {second.entity}{now}; "
                "a test case verifies at most one requirement"
            )
        else:
            msg = (
                f"this would make {case} verify {second.entity} while the same test code in {first.target} "
                f"verifies {first.entity}{now} (config.variants); a test case verifies at most one requirement"
            )
        return Conflict(pair.code, msg, case, (first.entity, second.entity), owner, via)

    @staticmethod
    def _lock_conflicts(
        candidate: Model, lock: rr_lock.Lock | None, before: Model, before_lock: rr_lock.Lock | None, old: Any
    ) -> list[Conflict]:
        if lock is None:
            return []

        def claimants(model: Model) -> Any:
            by_target: dict[str, list[Claim]] = {}
            for c in model.claims():
                by_target.setdefault(c.target, []).append(c)
            return lambda target, path: sorted(
                {c.entity for c in by_target.get(target, ()) if c.matches(path)}, key=natural_key
            )

        now, then = claimants(candidate), claimants(before)
        out = []
        for entry in lock.entries:
            others = [e for e in now(entry.target, entry.path) if e != entry.owner]
            if not others:
                continue
            was = before_lock.entry(entry.target, entry.path) if before_lock is not None else None
            if was is not None and was.owner == old(entry.owner):
                known = {e for e in then(entry.target, entry.path) if e != was.owner}
                if {old(e) for e in others} <= known:
                    continue
            out.append(
                Conflict(
                    "lock-owner-changed",
                    f"{entry.case} is locked to {entry.owner}; this would give it to {', '.join(others)}. "
                    "Move it in the case ledger (that re-locks it), or re-lock with `rr sets lock --write` "
                    "and review the owner change",
                    entry.case,
                    (entry.owner, *others),
                    entry.owner,
                    "lock",
                )
            )
        return out

    # ------------------------------------------------------------------ the case ledger

    def _attribution(self) -> Attribution:
        att = self.snapshot().attribution
        if att is None:  # build_matrix always attributes; a hand-made Matrix may not
            raise WorkspaceError("no attribution: the trace was built without one", 500)
        return att

    def cases(self, target: str = "", q: str = "", state: str = "") -> dict[str, Any]:
        """The case ledger: every test case of the loaded evidence with its one
        owner (or none), how it got it, and its quarantine — read from the
        :class:`~rules_requirements.attribution.Attribution`.

        ``target`` keeps one target's cases; ``q`` those whose key or owner
        contains it (any case); ``state`` is ``owned``, ``unowned`` (no owner
        and not quarantined), ``quarantined`` or ``unlocked`` (owned, and
        absent from a configured lock)."""
        att = self._attribution()
        needle = q.strip().lower()
        rows = []
        for key in att.cases:
            row = _case_row(att, key, self.current_build)
            if target and key.target != target:
                continue
            if needle and needle not in str(key).lower() and needle not in str(row.get("owner") or "").lower():
                continue
            if state and not _in_state(row, state, att):
                continue
            rows.append(row)
        owned = len(att.owner)
        quarantined = len(att.quarantined)
        return {
            "mode": att.mode,
            "lock": att.lock.path if att.lock is not None else "",
            "summary": {
                "cases": len(att.cases),
                "owned": owned,
                "unowned": len(att.cases) - owned - quarantined,
                "quarantined": quarantined,
            },
            "targets": sorted({k.target for k in att.cases}, key=natural_key),
            "cases": rows,
        }

    def attribution_payload(self) -> dict[str, Any]:
        """The attribution at a glance: mode, lock, per-target counts, every
        quarantine and issue, and each verifiable entity's set."""
        snap = self.snapshot()
        att = self._attribution()
        targets: dict[str, dict[str, Any]] = {}
        for t, run in att.targets.items():
            keys = [k for k in att.cases if k.target == t]
            owners = sorted({att.owner[k] for k in keys if k in att.owner}, key=natural_key)
            targets[t] = {
                "cases": len(keys),
                "owned": sum(1 for k in keys if k in att.owner),
                "owners": owners,
                "ran": run.ran,
                "synthetic": run.synthetic_only,
            }
            if run.tainted:
                targets[t]["taint"] = run.taint_message
        sets = {}
        for ent in att.entities:
            v = snap.matrix.verdicts.get(ent)
            sets[ent] = {"status": v.status if v else "", **_set_summary(att.members_of(ent))}
        return {
            "mode": att.mode,
            "lock": att.lock.path if att.lock is not None else "",
            "summary": self.cases()["summary"],
            "targets": targets,
            "quarantined": [q.to_dict() for q in att.quarantined],
            "issues": [i.to_dict() for i in att.issues],
            "sets": sets,
        }

    def move_case(self, case: str, to: str, *, expand: bool = False, dry_run: bool = False) -> dict[str, Any]:
        """Give test case ``case`` to entity ``to`` ("" or ``none``: to no one)
        by editing the model's claims — the only way a case changes owner.

        The claims that select it now give it up (a literal selector is
        dropped; a glob, or a whole-target claim of a target with per-case
        results, is rewritten into the literal selectors of the other cases
        it selects in the loaded evidence, which needs ``expand``); ``to``
        gains a literal selector (a whole-target claim for a target that
        reports only its single result). A configured lock entry of the case
        is re-locked in the same transaction. The edit then passes the save
        guard like any other, and attribution over the candidate model must
        give the case to ``to``. With ``dry_run`` nothing is written: the
        plan and its problems are returned.
        """
        with self._lock:
            snap = self.snapshot()
            att = self._attribution()
            try:
                key = CaseKey.parse(case)
            except ValueError as exc:
                raise WorkspaceError(str(exc)) from exc
            if key not in att.cases:
                raise WorkspaceError(f"{case} is not a test case of the loaded evidence", 404)
            target_id = "" if to.strip().lower() in ("", "none") else to.strip()
            if target_id and not snap.model.is_verifiable(target_id):
                raise WorkspaceError(f"{target_id} is not a user need, requirement or mitigation of the model")
            owner = att.owner_of(key)
            quarantine = att.quarantine_of(key)
            if quarantine is not None and quarantine.code == MULTI_TAG:
                raise WorkspaceError(
                    f"{key} names {', '.join(quarantine.declared)} in its own evidence (multi-tag); no model edit "
                    "can give it one owner: tag the test with one id",
                    409,
                )
            if owner == (target_id or None) and quarantine is None:
                raise WorkspaceError(f"{key} is {'owned by ' + owner if owner else 'unowned'} already", 409)
            if owner and att.via.get(key) == VIA_TAG and not target_id:
                raise WorkspaceError(
                    f"{key} is owned by {owner} through its own tag (hybrid attribution); to make it verify "
                    "nothing, remove the tag from the test",
                    409,
                )
            plan: list[str] = []
            needs_expand: list[str] = []
            edits: dict[str, list[Any]] = {}  # entity -> its new claim items
            claims = list(att.claimed_by.get(key, ()))
            for claim in claims:
                if claim.entity == target_id:
                    continue
                items = edits.setdefault(claim.entity, self._claim_items(claim.entity))
                note = self._give_up(items, claim, key, att, needs_expand)
                plan.append(f"{claim.entity}: {note}")
            if target_id and not any(c.entity == target_id for c in claims):
                items = edits.setdefault(target_id, self._claim_items(target_id))
                plan.append(f"{target_id}: {self._take(items, key, owner or '')}")
            txn = self._txn()
            for eid, items in edits.items():
                ent = snap.model.get(eid)
                assert ent is not None
                data = edit.entity_to_dict(ent)
                data[CLAIM_FIELDS[ent.kind]] = [i for i in items if i is not None]
                if not data[CLAIM_FIELDS[ent.kind]]:
                    del data[CLAIM_FIELDS[ent.kind]]
                txn.edit(
                    ent.location.path,
                    lambda t, eid=eid, data=data: edit.update_entity(t, eid, data),
                    {eid: {**data, "kind": ent.kind}},
                )
            lock = att.lock if snap.model.config.sets_lock else None
            if lock is not None and lock.entry(key.target, key.path) is not None:
                here = (key.target, key.path)
                entries = [
                    rr_lock.LockEntry(e.target, e.path, target_id) if (e.target, e.path) == here else e
                    for e in lock.entries
                    if target_id or (e.target, e.path) != here
                ]
                new_lock = rr_lock.Lock(tuple(entries), lock.path)
                txn.lock = new_lock
                txn.files_out[self._rel(snap.model.lock_path())] = rr_lock.render_lock(new_lock)
                plan.append(f"lock: {key} -> {target_id or 'no entry'}")
            result = txn.check()
            problems = [c.to_dict() for c in result.conflicts]
            if needs_expand and not expand:
                problems.append(
                    {
                        "code": "needs-expand",
                        "message": "; ".join(needs_expand)
                        + ". Confirm to rewrite them into literal selectors of the cases they select now",
                    }
                )
            gets = result.attribution.owner_of(key) if result.attribution is not None else None
            if result.ok and gets != (target_id or None):
                problems.append(
                    {
                        "code": "not-moved",
                        "message": f"after this edit attribution would give {key} to {gets or 'no one'}, "
                        f"not to {target_id or 'no one'}",
                    }
                )
            out = {
                "case": str(key),
                "from": owner or "",
                "to": target_id,
                "plan": plan,
                "ok": not problems,
                "problems": problems,
            }
            if dry_run:
                return out
            if problems:
                raise WorkspaceError("; ".join(p["message"] for p in problems), 409, {"conflicts": problems})
            txn.commit()
            return out

    def _claim_items(self, entity_id: str) -> list[Any]:
        ent = self.model.get(entity_id)
        assert ent is not None
        return list(edit.entity_to_dict(ent).get(CLAIM_FIELDS[ent.kind], []))

    def _give_up(self, items: list[Any], claim: Claim, key: CaseKey, att: Attribution, expand: list[str]) -> str:
        """Rewrite ``claim`` (one selector of ``items[claim.index]``) so that it no longer selects ``key``."""
        item = items[claim.index]
        if item is None:
            return f"its claim of {key.target} is dropped already"
        data = dict(item) if isinstance(item, dict) else {"target": item}
        if claim.pattern is None:
            if key.synthetic or not any(k.target == key.target and not k.synthetic and k != key for k in att.cases):
                items[claim.index] = None
                return f"drop its whole-target claim of {key.target}"
            others = [k.path for k in att.cases if k.target == key.target and not k.synthetic and k != key]
            data.pop("whole", None)
            data.pop("reason", None)
            data["cases"] = [case_selectors.escape(p) for p in sorted(others, key=natural_key)]
            items[claim.index] = data
            expand.append(f"{claim.entity} claims the whole target {key.target}")
            return f"replace its whole-target claim of {key.target} by {len(others)} literal selector(s)"
        selectors = list(data.get("cases", []))
        if claim.literal:
            selectors = [p for p in selectors if p != claim.pattern]
            note = f"drop selector {claim.pattern!r} of {key.target}"
        else:
            others = [
                k.path
                for k in att.cases
                if k.target == key.target and k != key and not k.synthetic and claim.matches(k.path)
            ]
            lits = [case_selectors.escape(p) for p in sorted(others, key=natural_key)]
            at = selectors.index(claim.pattern) if claim.pattern in selectors else len(selectors)
            selectors = selectors[:at] + [x for x in lits if x not in selectors] + selectors[at + 1 :]
            expand.append(f"{claim.entity} selects it with the glob {claim.pattern!r}")
            note = f"replace glob {claim.pattern!r} of {key.target} by {len(lits)} literal selector(s)"
        if selectors:
            data["cases"] = selectors
            items[claim.index] = data
        else:
            items[claim.index] = None
            note += " (and the item, now empty)"
        return note

    def _take(self, items: list[Any], key: CaseKey, owner: str) -> str:
        """Add a claim of ``key`` to ``items``."""
        if key.synthetic:
            reason = (
                f"moved from {owner}: the target reports no per-case results"
                if owner
                else ("the target reports no per-case results")
            )
            items.append({"target": key.target, "whole": True, "reason": reason})
            return f"claim the whole target {key.target}"
        pattern = case_selectors.escape(key.path)
        for i, item in enumerate(items):
            if (
                isinstance(item, dict)
                and labels.try_normalize(str(item.get("target", "")), self.model.config.main_repo) == key.target
                and isinstance(item.get("cases"), list)
                and not item.get("whole")
            ):
                items[i] = {**item, "cases": [*item["cases"], pattern]}
                return f"add selector {pattern!r} to its claim of {key.target}"
        items.append({"target": key.target, "cases": [pattern]})
        return f"claim {pattern!r} of {key.target}"

    def referrers(self, entity_id: str) -> list[tuple[str, str]]:
        """(referring id, relation) for every reference to ``entity_id``."""
        out = []
        for ent in self.model.entities():
            for relation, target in ent.references():
                if target == entity_id:
                    out.append((ent.id, relation))
        return out

    def _retarget(self, txn: _Transaction, holder_id: str, old: str, new: str | None) -> None:
        """Rewrite (or, with ``new`` None, drop) every reference to ``old`` in ``holder_id``."""
        holder = self.model.get(holder_id)
        if holder is None:
            return
        data = edit.entity_to_dict(holder)
        for key in ("satisfies", "refines", "mitigates", "implemented_by", "mitigated_by"):
            if key in data:
                data[key] = [x for x in (new if x == old else x for x in data[key]) if x is not None]
        if data.get("method") == old:
            if new is None:
                data.pop("method")
            else:
                data["method"] = new
        txn.edit(
            holder.location.path,
            lambda t: edit.update_entity(t, holder_id, data),
            {holder_id: {**data, "kind": holder.kind}},
        )

    def delete(self, entity_id: str, force: bool = False) -> None:
        with self._lock:
            ent = self.model.get(entity_id)
            if ent is None:
                raise WorkspaceError(f"{entity_id} does not exist", 404)
            # Deleting one copy of a duplicated id leaves the id defined: its
            # references stay (they now resolve to the remaining copy).
            duplicated = any(f"duplicate id {entity_id} (" in e for e in self.model.parse_errors)
            refs = [] if duplicated else self.referrers(entity_id)
            if refs and not force:
                who = ", ".join(f"{i} ({r})" for i, r in refs)
                raise WorkspaceError(f"{entity_id} is referenced by {who}", 409)
            txn = self._txn()
            for ref_id in dict.fromkeys(i for i, _ in refs):
                self._retarget(txn, ref_id, entity_id, None)
            # A file left with no content at all (a one-object file) is removed.
            txn.edit(ent.location.path, lambda t: edit.delete_entity(t, entity_id), {entity_id: None})
            txn.commit()

    def rename(self, old_id: str, new_id: str) -> list[str]:
        """Change an id and every model reference to it, in one transaction.

        Source annotations are not rewritten (the scan reports them). A
        one-object file named after the old id is renamed with it.
        """
        with self._lock:
            ent = self.model.get(old_id)
            if ent is None:
                raise WorkspaceError(f"{old_id} does not exist", 404)
            if self.model.get(new_id) is not None:
                raise WorkspaceError(f"{new_id} already exists", 409)
            if not self.model.config.id_regex(ent.kind).match(new_id):
                raise WorkspaceError(f"{new_id} does not match the {ent.kind} id pattern")
            txn = self._txn()
            touched = list(dict.fromkeys(i for i, _ in self.referrers(old_id)))
            for ref_id in touched:
                self._retarget(txn, ref_id, old_id, new_id)
            data = {**edit.entity_to_dict(ent), "id": new_id}
            rel = ent.location.path
            txn.aliases[new_id] = old_id
            txn.edit(
                rel, lambda t: edit.update_entity(t, old_id, data), {old_id: None, new_id: {**data, "kind": ent.kind}}
            )
            base, ext = os.path.splitext(os.path.basename(rel))
            if base == old_id and self._single_object(rel):
                txn.move(rel, os.path.join(os.path.dirname(rel), new_id + ext).replace(os.sep, "/"))
            txn.commit()
            return touched

    # ------------------------------------------------------------------ notes

    def add_note(self, entity_id: str, text: str, kind: str = "comment", author: str = "") -> str:
        with self._lock:
            ent = self.model.get(entity_id)
            if ent is None:
                raise WorkspaceError(f"{entity_id} does not exist", 404)
            used = {n.id for n in ent.notes}
            nid = next(f"n{i}" for i in range(1, len(used) + 2) if f"n{i}" not in used)
            note = Note(
                text=text.strip(), kind=kind, author=author or self.author, created=_dt.date.today().isoformat(), id=nid
            )
            self.update(entity_id, edit.entity_to_dict(replace(ent, notes=(*ent.notes, note))))
            return nid

    def update_note(self, entity_id: str, note_id: str, **changes: Any) -> None:
        with self._lock:
            ent = self.model.get(entity_id)
            if ent is None:
                raise WorkspaceError(f"{entity_id} does not exist", 404)
            if note_id not in {n.id for n in ent.notes}:
                raise WorkspaceError(f"{entity_id} has no note {note_id}", 404)
            notes = tuple(
                replace(n, **{k: v for k, v in changes.items() if v is not None}) if n.id == note_id else n
                for n in ent.notes
            )
            self.update(entity_id, {**edit.entity_to_dict(ent), "notes": [edit._note_dict(n) for n in notes]})

    def delete_note(self, entity_id: str, note_id: str) -> None:
        with self._lock:
            ent = self.model.get(entity_id)
            if ent is None:
                raise WorkspaceError(f"{entity_id} does not exist", 404)
            notes = [edit._note_dict(n) for n in ent.notes if n.id != note_id]
            if len(notes) == len(ent.notes):
                raise WorkspaceError(f"{entity_id} has no note {note_id}", 404)
            self.update(entity_id, {**edit.entity_to_dict(ent), "notes": notes})

    # ------------------------------------------------------------------ sources

    def source(self, rel: str, max_bytes: int = 512 * 1024) -> list[str]:
        """Lines of a text file inside the workspace (for the code browser)."""
        path = self.abspath(rel)
        if not os.path.isfile(path):
            raise WorkspaceError(f"{rel} is not a file", 404)
        if os.path.getsize(path) > max_bytes:
            raise WorkspaceError(f"{rel} is too large to display", 413)
        try:
            with open(path, encoding="utf-8") as fh:
                return fh.read().splitlines()
        except UnicodeDecodeError as exc:
            raise WorkspaceError(f"{rel} is not a text file", 415) from exc

    # ------------------------------------------------------------------ git

    def git(self, *args: str, check: bool = True, env: Mapping[str, str] | None = None) -> str:
        proc = subprocess.run(
            ["git", "-C", self.root, *args],
            capture_output=True,
            text=True,
            env={**os.environ, **(env or {})},
            check=False,
        )
        if check and proc.returncode != 0:
            raise WorkspaceError(f"git {args[0]} failed: {proc.stderr.strip() or proc.stdout.strip()}", 500)
        return proc.stdout

    def is_git(self) -> bool:
        try:
            return self.git("rev-parse", "--is-inside-work-tree", check=False).strip() == "true"
        except OSError:
            return False

    def _check_ref(self, ref: str) -> str:
        if not ref or not _REF_OK.match(ref) or ref.startswith("-"):
            raise WorkspaceError(f"invalid ref {ref!r}")
        return ref

    def _resolve(self, ref: str) -> str:
        """A commit id for ``ref``; unknown refs are a 404, not a server error."""
        self._check_ref(ref)
        sha = self.git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False).strip()
        if not sha:
            raise WorkspaceError(f"unknown ref {ref!r}", 404)
        return sha

    def git_status(self) -> dict[str, Any]:
        if not self.is_git():
            return {"git": False}
        branch = self.git("rev-parse", "--abbrev-ref", "HEAD", check=False).strip()
        head = self.git("rev-parse", "--short", "HEAD", check=False).strip()
        changed = self._changed_model_files()
        name = self.git("config", "user.name", check=False).strip()
        email = self.git("config", "user.email", check=False).strip()
        return {
            "git": True,
            "branch": branch,
            "head": head,
            "changed": changed,
            "user": f"{name} <{email}>" if name else "",
        }

    def _changed_model_files(self) -> list[str]:
        """Model files (YAML/JSON under the model paths) with uncommitted changes."""
        out = self.git("status", "--porcelain", "-z", "--untracked-files=all", "--", *self.model_paths, check=False)
        entries = out.split("\0")
        changed: list[str] = []
        i = 0
        while i < len(entries):
            entry = entries[i]
            i += 1
            if len(entry) < 4:
                continue
            status, path = entry[:2], entry[3:]
            if status[0] in "RC":  # renames/copies carry the source path next
                i += 1
            prefix = self.git("rev-parse", "--show-prefix", check=False).strip()
            rel = path[len(prefix) :] if prefix and path.startswith(prefix) else path
            if rel.endswith((".yaml", ".yml", ".json")) and self._covered(rel):
                changed.append(rel)
        return sorted(dict.fromkeys(changed))

    def refs(self) -> dict[str, Any]:
        if not self.is_git():
            return {"branches": [], "tags": [], "head": ""}
        branches = [
            b.strip()
            for b in self.git("for-each-ref", "--format=%(refname:short)", "refs/heads", "refs/remotes").splitlines()
            if b.strip() and not b.endswith("/HEAD")
        ]
        tags = []
        for line in self.git(
            "for-each-ref",
            "--sort=-creatordate",
            "--format=%(refname:short)\t%(objectname:short)\t%(creatordate:short)\t%(contents:subject)",
            "refs/tags",
        ).splitlines():
            parts = line.split("\t")
            if parts and parts[0]:
                tags.append(
                    {
                        "name": parts[0],
                        "sha": parts[1] if len(parts) > 1 else "",
                        "date": parts[2] if len(parts) > 2 else "",
                        "message": parts[3] if len(parts) > 3 else "",
                    }
                )
        return {
            "branches": branches,
            "tags": tags,
            "head": self.git("rev-parse", "--abbrev-ref", "HEAD", check=False).strip(),
        }

    def log(self, limit: int = 30, ref: str = "HEAD") -> list[dict[str, str]]:
        if not self.is_git():
            return []
        out = self.git(
            "log",
            f"-n{int(limit)}",
            "--format=%H%x1f%h%x1f%an%x1f%ad%x1f%s%x1f%P",
            "--date=short",
            self._resolve(ref),
            "--",
            *self.model_paths,
            check=False,
        )
        rows = []
        for line in out.splitlines():
            full, short, author, date, subject, parents = (line.split("\x1f") + [""] * 6)[:6]
            parent = parents.split()[0] if parents.split() else ""
            # `parent` is the first parent, or "" for a root commit (diff it from EMPTY).
            rows.append(
                {"sha": full, "short": short, "author": author, "date": date, "subject": subject, "parent": parent}
            )
        return rows

    def model_at(self, ref: str) -> Model:
        """The model as committed at ``ref``.

        ``WORKTREE`` is the files on disk; ``EMPTY`` is no model at all (to
        diff a repository's first commit, or a model's whole history).
        """
        if ref in ("", "WORKTREE"):
            return self.model
        if ref == "EMPTY":
            return Model(config=self.model.config)
        ref = self._resolve(ref)
        # --full-name: paths relative to the repository top (the workspace root
        # may be a subdirectory); -z: no C-quoting of non-ASCII names.
        listing = self.git("ls-tree", "-r", "-z", "--full-name", "--name-only", ref, "--", *self.model_paths)
        prefix = self.git("rev-parse", "--show-prefix").strip()  # the root, relative to the repository top
        docs: list[tuple[str, Any]] = []
        for full in sorted(p for p in listing.split("\0") if p):
            rel = full[len(prefix) :] if full.startswith(prefix) else full
            if not self._loadable(rel, (".yaml", ".yml", ".json")):
                continue  # read the files the working-tree loader reads, and only those
            try:
                docs.extend((rel, d) for d in load_text(self.git("show", f"{ref}:{full}")))
            except yaml.YAMLError:
                continue
        model, _ = parse_documents(docs)
        return model

    def diff(self, old: str, new: str = "WORKTREE") -> list[EntityChange]:
        return diff_models(self.model_at(old), self.model_at(new))

    def _require_identity(self, author: str) -> None:
        """Refuse up front when neither the editor nor git knows who is committing
        (git would stop with "Author identity unknown", or record a guess)."""
        if re.match(r"^\s*(.+?)\s*<([^>]+)>\s*$", author or ""):
            return
        if (
            self.git("config", "user.name", check=False).strip()
            and self.git("config", "user.email", check=False).strip()
        ):
            return
        raise WorkspaceError(
            "who is committing? Set your name and e-mail in the editor (top right, as Name <email>), "
            "or configure git user.name and user.email",
            400,
        )

    def _identity_env(self, author: str) -> dict[str, str]:
        """Committer/tagger identity from ``author`` when git has none configured."""
        m = re.match(r"^\s*(.+?)\s*<([^>]+)>\s*$", author or "")
        if not m or self.git_status().get("user"):
            return {}
        return {"GIT_COMMITTER_NAME": m.group(1), "GIT_COMMITTER_EMAIL": m.group(2)}

    def tag(self, name: str, message: str = "", ref: str = "HEAD", author: str = "") -> None:
        """Name a baseline: an annotated tag on ``ref``."""
        name = name.strip()
        if subprocess.run(
            ["git", "check-ref-format", f"refs/tags/{name}"], capture_output=True
        ).returncode != 0 or name.startswith("-"):
            raise WorkspaceError(f"invalid tag name {name!r}")
        if self.git("tag", "--list", name).strip():
            raise WorkspaceError(f"tag {name} already exists", 409)
        args = ["tag", "-a", name, "-m", message or f"requirements baseline {name}", self._resolve(ref)]
        self._require_identity(author or self.author)
        self.git(*args, env=self._identity_env(author or self.author))

    def commit(self, message: str, author: str = "") -> str:
        if not message.strip():
            raise WorkspaceError("a commit message is required")
        changed = self._changed_model_files()
        if not changed:
            raise WorkspaceError("no model changes to commit", 409)
        self._require_identity(author or self.author)
        # Stage and commit exactly the model files: never sweep in unrelated
        # edits (a BUILD file, scratch files) that happen to sit alongside.
        self.git("add", "-A", "--", *changed)
        args = ["commit", "-m", message, "--", *changed]
        author = author or self.author
        m = re.match(r"^\s*(.+?)\s*<([^>]+)>\s*$", author or "")
        if m:
            args[1:1] = ["--author", f"{m.group(1)} <{m.group(2)}>"]
        self.git(*args, env=self._identity_env(author))
        return self.git("rev-parse", "--short", "HEAD").strip()


class _Transaction:
    """Edits to one or more model files, verified together, then written.

    Every edited file is re-parsed and checked with :func:`edit.verify`
    (exactly the intended entities changed, nothing else); only if every file
    passes is anything written, and the writes themselves are all-or-nothing:
    every new file is staged first, then all are moved into place, and a
    failure part-way restores the files already replaced.
    """

    def __init__(self, ws: Workspace):
        self.ws = ws
        self.texts: dict[str, str] = {}  # LF-normalised, as edited
        self.orig: dict[str, str] = {}  # LF-normalised, as read
        self.raw: dict[str, str | None] = {}  # exactly as read (None: a new file)
        self.expect: dict[str, dict[str, Any]] = {}
        self.moves: dict[str, str] = {}
        self.aliases: dict[str, str] = {}  # renamed ids, new -> old
        self.files_out: dict[str, str] = {}  # other files written with the model (the lock), as-is
        self.lock: rr_lock.Lock | str | None = "configured"  # the lock the save leaves

    def _text(self, rel: str) -> str:
        if rel not in self.texts:
            if rel.endswith(".json"):
                raise WorkspaceError(f"{rel} is a JSON model file; the editor writes YAML only — edit it by hand", 422)
            raw = self.ws._load(rel)
            text = (raw or "").replace("\r\n", "\n")
            if "\r" in text:
                raise WorkspaceError(f"{rel} has bare CR line endings; convert it to LF or CRLF to edit it here", 422)
            self.texts[rel] = self.orig[rel] = text
            self.raw[rel] = raw
        return self.texts[rel]

    def edit(self, rel: str, fn: Any, expect: Mapping[str, Any]) -> None:
        text = self._text(rel)
        try:
            self.texts[rel] = fn(text)
        except edit.EditError as exc:
            raise WorkspaceError(f"{rel}: {exc}", 422) from exc
        except KeyError as exc:
            raise WorkspaceError(f"{rel}: {exc.args[0] if exc.args else exc}", 404) from exc
        except ValueError as exc:
            raise WorkspaceError(f"{rel}: {exc}", 400) from exc
        self.expect.setdefault(rel, {}).update(expect)

    def move(self, src: str, dst: str) -> None:
        if os.path.exists(self.ws.abspath(dst)):
            raise WorkspaceError(f"{dst} already exists", 409)
        self.moves[src] = dst

    def verify(self) -> None:
        for rel, text in self.texts.items():
            try:
                edit.verify(self.orig[rel], text, self.expect.get(rel, {}), rel, aliases=self.aliases)
            except edit.EditError as exc:
                raise WorkspaceError(f"{rel}: {exc}", 422) from exc

    def edited(self) -> list[str]:
        """The ids this transaction writes (new ids of renamed entities)."""
        return [eid for want in self.expect.values() for eid, data in want.items() if data is not None]

    def check(self) -> Check:
        """Verify the edits and run the save guard on the model they produce."""
        self.verify()
        return self.ws.check(self.ws.candidate(self), edited=self.edited(), aliases=self.aliases, lock=self.lock)

    def commit(self) -> None:
        result = self.check()
        if not result.ok:
            raise WorkspaceError(
                "; ".join(c.message for c in result.conflicts),
                409,
                {"conflicts": [c.to_dict() for c in result.conflicts]},
            )
        writes: list[tuple[str, str, int | None]] = []  # (path, content, mode of a new file)
        removes: list[str] = []
        for rel, text in self.texts.items():
            src, dst = self.ws.abspath(rel), self.ws.abspath(self.moves.get(rel, rel))
            if edit.is_blank(text) and self.raw[rel] is not None:
                removes.append(src)  # nothing left in it (a one-object file)
                continue
            data = with_line_endings(self.raw[rel] or "", text)
            if dst == src and data == self.raw[rel]:
                continue  # unchanged: leave the file (and its mtime) alone
            writes.append((dst, data, _mode(src) if dst != src else None))
            if dst != src:
                removes.append(src)
        for rel, data in self.files_out.items():
            path = self.ws.abspath(rel)
            writes.append((path, data, None))
        _write_all(writes, removes, self.ws.root)


def with_line_endings(orig: str, text: str) -> str:
    """``text`` (LF line endings) with the line endings of ``orig`` restored:
    every line kept or changed from ``orig`` keeps its own ending; added lines
    get the ending most of ``orig`` uses."""
    if "\r\n" not in orig:
        return text
    parts = orig.split("\n")
    crlf = [p.endswith("\r") for p in parts[:-1]]
    old = [p[:-1] if c else p for p, c in zip(parts[:-1], crlf)] + parts[-1:]
    new = text.split("\n")
    use = [2 * sum(crlf) >= len(crlf)] * (len(new) - 1)
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("equal", "replace"):  # a replaced line keeps its line's ending
            for k in range(j2 - j1):
                i = i1 + min(k, i2 - i1 - 1)
                if i < len(crlf) and j1 + k < len(use):
                    use[j1 + k] = crlf[i]
    return "".join(line + ("\r\n" if c else "\n") for line, c in zip(new[:-1], use)) + new[-1]


def _umask() -> int:
    """The process umask, read without changing it (``os.umask`` sets it for
    every thread, including git subprocesses started meanwhile)."""
    try:
        with open("/proc/self/status", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("Umask:"):
                    return int(line.split()[1], 8)
    except (OSError, ValueError):
        pass
    return 0o022


def _mode(path: str) -> int:
    try:
        return os.stat(path).st_mode & 0o7777
    except FileNotFoundError:
        return 0o666 & ~_umask()


def _write_all(writes: list[tuple[str, str, int | None]], removes: list[str], root: str) -> None:
    """Replace, create and remove files as one unit: refuse what cannot be
    written, stage every new content in a temporary file next to its target,
    then move them all into place and remove files; on a failure part-way,
    restore every file already changed."""

    def show(path: str) -> str:
        return os.path.relpath(path, root).replace(os.sep, "/")

    for path, _, _ in writes:
        if os.path.exists(path) and not os.access(path, os.W_OK):
            raise WorkspaceError(f"{show(path)} is read-only", 403)
    for folder in {os.path.dirname(p) for p, _, _ in writes} | {os.path.dirname(p) for p in removes}:
        while not os.path.isdir(folder) and os.path.dirname(folder) != folder:
            folder = os.path.dirname(folder)  # to be created: its first existing ancestor
        if not os.access(folder, os.W_OK | os.X_OK):
            raise WorkspaceError(f"{show(folder)}/ is not writable", 403)
    staged: list[tuple[str, str]] = []  # (temporary file, target)
    try:
        for path, data, mode in writes:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".rr-", suffix=".tmp")
            staged.append((tmp, path))
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
                fh.write(data)
            os.chmod(tmp, _mode(path) if mode is None else mode)
    except OSError as exc:
        _discard(tmp for tmp, _ in staged)
        raise WorkspaceError(f"could not write {show(exc.filename or root)}: {exc.strerror or exc}", 500) from exc
    backups: list[tuple[str, bytes | None, int]] = []  # (path, previous content or None, mode)
    try:
        for tmp, path in staged:  # a backup counts once its file has changed
            backup = _backup(path)
            os.replace(tmp, path)
            backups.append(backup)
        for path in removes:
            backup = _backup(path)
            os.remove(path)
            backups.append(backup)
    except OSError as exc:
        failed = _restore(backups)
        _discard(tmp for tmp, _ in staged)
        msg = f"could not write {show(exc.filename or root)}: {exc.strerror or exc}; nothing was changed"
        if failed:
            msg = msg.replace("nothing was changed", "could not restore " + ", ".join(map(show, failed)))
        raise WorkspaceError(msg, 500) from exc


def _backup(path: str) -> tuple[str, bytes | None, int]:
    try:
        with open(path, "rb") as fh:
            return path, fh.read(), _mode(path)
    except FileNotFoundError:
        return path, None, 0


def _restore(backups: list[tuple[str, bytes | None, int]]) -> list[str]:
    """Put back files as they were (the way they were changed: through a
    temporary file, so a read-only file restores too); returns failures."""
    failed = []
    for path, content, mode in reversed(backups):
        tmp = ""
        try:
            if content is None:
                if os.path.exists(path):
                    os.remove(path)
                continue
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".rr-", suffix=".tmp")
            with os.fdopen(fd, "wb") as fh:
                fh.write(content)
            os.chmod(tmp, mode)
            os.replace(tmp, path)
        except OSError:
            failed.append(path)
            if tmp:
                _discard([tmp])
    return failed


def _discard(paths: Iterable[str]) -> None:
    for tmp in paths:
        try:
            os.remove(tmp)
        except OSError:
            pass


def entity_payload(ws: Workspace, entity_id: str) -> dict[str, Any]:
    """Everything the UI shows for one entity."""
    snap = ws.snapshot()
    ent = snap.model.get(entity_id)
    if ent is None:
        raise WorkspaceError(f"{entity_id} does not exist", 404)
    verdict = snap.matrix.verdicts.get(entity_id)
    att = snap.attribution
    verifiable = snap.model.is_verifiable(entity_id)
    members = att.members_of(entity_id) if att is not None else ()
    incoming = [
        {"id": other.id, "kind": other.kind, "relation": rel, "title": other.title, "status": _status(snap, other.id)}
        for other in snap.model.entities()
        for rel, target in other.references()
        if target == entity_id
    ]
    outgoing = []
    for rel, target in ent.references():
        t = snap.model.get(target)
        if t is None and rel == "method" and snap.model.config.level(target) is not None:
            # A requirement may name a verification level directly: not a trace.
            outgoing.append(
                {
                    "id": target,
                    "kind": "level",
                    "relation": rel,
                    "title": "verification level",
                    "status": "",
                    "missing": False,
                    "level": True,
                }
            )
            continue
        outgoing.append(
            {
                "id": target,
                "kind": t.kind if t else "",
                "relation": rel,
                "title": t.title if t else "",
                "status": _status(snap, target),
                "missing": t is None,
            }
        )
    return {
        "id": ent.id,
        "kind": ent.kind,
        "version": ws.version(ent),
        "data": edit.entity_to_dict(ent),
        "location": {"path": ent.location.path, "line": ent.location.line},
        "status": verdict.status if verdict else "",
        "demanded": verdict.demanded if verdict else "",
        "provided": verdict.provided if verdict else "",
        "stale": bool(verdict and verdict.stale),
        "pyramid_violation": bool(verdict and verdict.pyramid_violation),
        "evidence": [
            e.to_dict() | ({"source": e.source} if e.source else {}) for e in (verdict.evidence if verdict else [])
        ],
        "implemented_in": [r.to_dict() for r in (verdict.implemented_in if verdict else [])],
        "verified_in": [r.to_dict() for r in (verdict.verified_in if verdict else [])],
        "incoming": sorted(incoming, key=lambda x: natural_key(x["id"])),
        "outgoing": outgoing,
        "issues": [_issue(i) for i in snap.issues if i.entity == entity_id],
        "gaps": [g.to_dict() for g in snap.matrix.gaps if g.entity == entity_id],
        # Its verification set, as attribution decided it (never from tags or targets).
        "members": [_member(m) for m in members],
        "set": _set_summary(members) if verifiable else {},
        "quarantined": [q.to_dict() for q in att.quarantined if entity_id in q.entities] if att is not None else [],
        "basis": verdict.basis if verdict else "",
        "derived_from": list(verdict.derived_from) if verdict else [],
        "verifiable": verifiable,
    }


def _case_row(att: Attribution, key: CaseKey, current: Mapping[str, str]) -> dict[str, Any]:
    """One row of the case ledger, read from the attribution."""
    res = att.cases[key]
    row: dict[str, Any] = {
        "case": str(key),
        "target": key.target,
        "path": key.path,
        "owner": att.owner.get(key),
        "via": att.via.get(key, ""),
        "status": res.status,
        "level": res.level,
        "declared": list(res.declared),
        "claimed_by": sorted({c.entity for c in att.claimed_by.get(key, ())}, key=natural_key),
    }
    for name in ("file", "line", "message"):
        if getattr(res, name):
            row[name] = getattr(res, name)
    if res.synthetic:
        row["synthetic"] = True
    if res.flaky:
        row["flaky"] = True
    if res.stale(current):
        row["stale"] = True
    q = att.quarantine_of(key)
    if q is not None:
        row["quarantine"] = {"code": q.code, "entities": list(q.entities), "detail": q.detail}
    if att.lock is not None:
        row["locked_to"] = att.lock.owner_of(key.target, key.path) or ""
    return row


def _in_state(row: Mapping[str, Any], state: str, att: Attribution) -> bool:
    if state == "owned":
        return bool(row["owner"])
    if state == "unowned":
        return not row["owner"] and "quarantine" not in row
    if state == "quarantined":
        return "quarantine" in row
    if state == "unlocked":
        return bool(row["owner"]) and att.lock is not None and not row.get("locked_to")
    raise WorkspaceError(f"unknown case state {state!r} (owned, unowned, quarantined, unlocked)")


def _member(m: Member) -> dict[str, Any]:
    """A member of a verification set, for the UI."""
    out = m.to_dict()
    out["name"] = m.name
    out["owned"] = m.owned
    if m.origin:
        out["origin"] = m.origin
    if m.result is not None:
        for name in ("file", "line", "message"):
            if getattr(m.result, name):
                out[name] = getattr(m.result, name)
    return out


def _set_summary(members: Iterable[Member]) -> dict[str, Any]:
    """Counts of a verification set by member state (``complete``: every
    member is present and passed — VERIFIED may still need fresh, unflaky
    evidence at the demanded level)."""
    members = list(members)
    counts = Counter(m.state for m in members)
    out: dict[str, Any] = {"members": len(members)}
    for state in (*OWNED_STATES, "missing", "not-run", "moved", "quarantined"):
        out[state.replace("-", "_")] = counts.get(state, 0)
    out["complete"] = bool(members) and counts.get("passed", 0) == len(members)
    return out


def _selector_counts(members: Iterable[Member]) -> list[dict[str, Any]]:
    """Per claim selector: how many cases it gave the entity (the editor's "matches N cases")."""
    counts: dict[tuple[str, str], dict[str, Any]] = {}
    for m in members:
        if m.via == "lock":
            continue
        row = counts.setdefault((m.target, m.selector), {"target": m.target, "selector": m.selector, "cases": 0})
        if m.owned or m.state == "quarantined":
            row["cases"] += 1
    return list(counts.values())


def _status(snap: Snapshot, entity_id: str) -> str:
    v = snap.matrix.verdicts.get(entity_id)
    return v.status if v else ""


def _issue(i: Issue) -> dict[str, Any]:
    return {
        "severity": i.severity,
        "code": i.code,
        "message": i.message,
        "entity": i.entity,
        "path": i.location.path,
        "line": i.location.line,
    }


def summary_rows(ws: Workspace, kinds: Iterable[str] = cfg.KINDS) -> list[dict[str, Any]]:
    snap = ws.snapshot()
    rows = []
    for kind in kinds:
        for ent in sorted(snap.model.section(kind).values(), key=lambda e: natural_key(e.id)):
            v = snap.matrix.verdicts.get(ent.id)
            rows.append(
                {
                    "id": ent.id,
                    "kind": kind,
                    "title": ent.title,
                    "status": v.status if v else "",
                    "open_notes": sum(1 for n in ent.notes if n.status == "open"),
                    "issues": sum(1 for i in snap.issues if i.entity == ent.id),
                    "path": ent.location.path,
                    "line": ent.location.line,
                    "tags": list(ent.tags),
                    "version": ws.version(ent),
                    "data": edit.entity_to_dict(ent),
                }
            )
    return rows
