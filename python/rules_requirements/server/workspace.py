# SPDX-License-Identifier: AGPL-3.0-or-later
"""The web editor's view of a repository: model files, evidence, annotations
and git history, with every mutation going through surgical text edits.

All paths handed out or accepted are relative to the workspace root; anything
resolving outside it (or into ``.git``) is refused.
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
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Mapping

from rules_requirements import annotations as rr_annotations
from rules_requirements import config as cfg
from rules_requirements import edit, ingest
from rules_requirements._vendor import yaml
from rules_requirements.diff import EntityChange, diff_models
from rules_requirements.model import Model, Note, load_text, model_files, parse_documents, read_model
from rules_requirements.trace import Matrix, build_matrix
from rules_requirements.util import natural_key
from rules_requirements.validate import Issue, validate


class WorkspaceError(Exception):
    """A request the workspace refuses (bad input, conflict, unsafe path)."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@dataclass
class Snapshot:
    model: Model
    issues: list[Issue]
    warnings: list[str]
    matrix: Matrix
    references: list[rr_annotations.Reference] | None
    signature: tuple[Any, ...] = ()


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
        if kind not in cfg.KINDS:
            raise WorkspaceError(f"unknown kind {kind!r}")
        with self._lock:
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
            txn.commit()
            return ent.id

    def update(self, entity_id: str, data: Mapping[str, Any], version: str = "") -> None:
        """Replace an entity's fields with ``data``.

        ``notes`` are kept unless ``data`` names them (the form editor does
        not); with ``version`` (from :func:`entity_payload`) a concurrent change
        since the caller loaded the entity is a 409 instead of being lost.
        """
        with self._lock:
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
            txn.commit()

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
        self.git(*args, env=self._identity_env(author or self.author))

    def commit(self, message: str, author: str = "") -> str:
        if not message.strip():
            raise WorkspaceError("a commit message is required")
        changed = self._changed_model_files()
        if not changed:
            raise WorkspaceError("no model changes to commit", 409)
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

    def commit(self) -> None:
        for rel, text in self.texts.items():
            try:
                edit.verify(self.orig[rel], text, self.expect.get(rel, {}), rel, aliases=self.aliases)
            except edit.EditError as exc:
                raise WorkspaceError(f"{rel}: {exc}", 422) from exc
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
    }


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
