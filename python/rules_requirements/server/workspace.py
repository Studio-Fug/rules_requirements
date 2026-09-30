# SPDX-License-Identifier: AGPL-3.0-or-later
"""The web editor's view of a repository: model files, evidence, annotations
and git history, with every mutation going through surgical text edits.

All paths handed out or accepted are relative to the workspace root; anything
resolving outside it (or into ``.git``) is refused.
"""

from __future__ import annotations

import datetime as _dt
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
from rules_requirements.model import Model, Note, model_files, parse_documents, read_model
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
        if rel == ".git" or rel.startswith(".git/"):
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

    def _read(self, rel: str) -> str:
        try:
            with open(self.abspath(rel), encoding="utf-8") as fh:
                return fh.read()
        except FileNotFoundError:
            return ""

    def _write(self, rel: str, text: str) -> None:
        path = self.abspath(rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".rr-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise

    def _check(self, rel: str, text: str) -> None:
        """Refuse an edit that makes the file unparsable."""
        try:
            docs = [d for d in yaml.safe_load_all(text) if d is not None]
        except yaml.YAMLError as exc:  # pragma: no cover - render_entity emits valid YAML
            raise WorkspaceError(f"edit would corrupt {rel}: {exc}", 500) from exc
        _, _ = parse_documents([(rel, d) for d in docs])

    def next_id(self, kind: str) -> str:
        model = self.model
        prefix = model.config.prefix(kind)
        existing = [e.id for e in model.section(kind).values()]
        nums = [int(m.group(1)) for i in existing if (m := re.match(re.escape(prefix) + r"-(\d+)$", i))]
        width = max((len(i.split("-", 1)[1]) for i in existing if re.match(re.escape(prefix) + r"-0\d+$", i)), default=0)
        return f"{prefix}-{(max(nums) + 1 if nums else 1):0{width}d}"

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
            if counts[best] == 1 and len(counts) > 1 and self._single_object(best):
                return os.path.dirname(best)  # a directory: one file per object
            return best
        files = self.files()
        if files:
            return files[0]
        base = self.model_paths[0] if self.model_paths else "requirements"
        return base if base.endswith((".yaml", ".yml")) else f"{base}/requirements.yaml"

    def _single_object(self, rel: str) -> bool:
        return bool(re.search(r"^kind:\s", self._read(rel), re.M))

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
            if not target.endswith((".yaml", ".yml", ".json")):
                target = f"{target}/{ent.id}.yaml"
            if os.path.exists(self.abspath(target)) and target.endswith((".yaml", ".yml")) and not self._single_object(target):
                text = edit.insert_entity(self._read(target), kind, edit.entity_to_dict(ent))
            elif os.path.exists(self.abspath(target)):
                raise WorkspaceError(f"{target} already exists", 409)
            else:
                text = edit.render_file(kind, edit.entity_to_dict(ent))
            self._check(target, text)
            if not self._covered(target):
                raise WorkspaceError(f"{target} is not part of the model paths {self.model_paths}")
            self._write(target, text)
            return ent.id

    def _covered(self, rel: str) -> bool:
        return any(rel == p or rel.startswith(p.rstrip("/") + "/") for p in self.model_paths)

    def update(self, entity_id: str, data: Mapping[str, Any]) -> None:
        with self._lock:
            ent = self.model.get(entity_id)
            if ent is None:
                raise WorkspaceError(f"{entity_id} does not exist", 404)
            data = dict(data)
            if str(data.get("id", entity_id)) != entity_id:
                raise WorkspaceError("use rename to change an id")
            data["id"] = entity_id
            new, problems = edit.dict_to_entity(ent.kind, data)
            if new is None or problems:
                raise WorkspaceError("; ".join(problems) or "invalid entity")
            rel = ent.location.path
            text = edit.update_entity(self._read(rel), entity_id, edit.entity_to_dict(new))
            self._check(rel, text)
            self._write(rel, text)

    def referrers(self, entity_id: str) -> list[tuple[str, str]]:
        """(referring id, relation) for every reference to ``entity_id``."""
        out = []
        for ent in self.model.entities():
            for relation, target in ent.references():
                if target == entity_id:
                    out.append((ent.id, relation))
        return out

    def delete(self, entity_id: str, force: bool = False) -> None:
        with self._lock:
            ent = self.model.get(entity_id)
            if ent is None:
                raise WorkspaceError(f"{entity_id} does not exist", 404)
            refs = self.referrers(entity_id)
            if refs and not force:
                who = ", ".join(f"{i} ({r})" for i, r in refs)
                raise WorkspaceError(f"{entity_id} is referenced by {who}", 409)
            for ref_id, _ in refs:
                self._drop_reference(ref_id, entity_id)
            rel = ent.location.path
            if self._single_object(rel) and len([e for e in self.model.entities() if e.location.path == rel]) == 1:
                os.remove(self.abspath(rel))
                return
            text = edit.delete_entity(self._read(rel), entity_id)
            self._check(rel, text)
            self._write(rel, text)

    def _drop_reference(self, holder_id: str, target_id: str) -> None:
        holder = self.model.get(holder_id)
        if holder is None:
            return
        data = edit.entity_to_dict(holder)
        for key in ("satisfies", "refines", "mitigates", "implemented_by", "mitigated_by"):
            if key in data:
                data[key] = [x for x in data[key] if x != target_id]
        if data.get("method") == target_id:
            data.pop("method")
        self.update(holder_id, data)

    def rename(self, old_id: str, new_id: str) -> list[str]:
        """Change an id and every model reference to it; returns touched ids.

        Source annotations are not rewritten (they are reported by the scan).
        """
        with self._lock:
            ent = self.model.get(old_id)
            if ent is None:
                raise WorkspaceError(f"{old_id} does not exist", 404)
            if self.model.get(new_id) is not None:
                raise WorkspaceError(f"{new_id} already exists", 409)
            if not self.model.config.id_regex(ent.kind).match(new_id):
                raise WorkspaceError(f"{new_id} does not match the {ent.kind} id pattern")
            touched = []
            for ref_id, _ in self.referrers(old_id):
                holder = self.model.get(ref_id)
                assert holder is not None
                data = edit.entity_to_dict(holder)
                for key in ("satisfies", "refines", "mitigates", "implemented_by", "mitigated_by"):
                    if key in data:
                        data[key] = [new_id if x == old_id else x for x in data[key]]
                if data.get("method") == old_id:
                    data["method"] = new_id
                self.update(ref_id, data)
                touched.append(ref_id)
            rel = ent.location.path
            text = self._read(rel)
            span = edit.locate(text, old_id)
            assert span is not None
            data = {**edit.entity_to_dict(ent), "id": new_id}
            rendered = (
                edit.render_entity(ent.kind, data, indent=span.indent, list_item=True)
                if span.list_item
                else edit.render_file(ent.kind, data)
            )
            text = text[: span.start] + rendered + text[span.end :]
            self._check(rel, text)
            self._write(rel, text)
            return touched

    # ------------------------------------------------------------------ notes

    def add_note(self, entity_id: str, text: str, kind: str = "comment", author: str = "") -> str:
        with self._lock:
            ent = self.model.get(entity_id)
            if ent is None:
                raise WorkspaceError(f"{entity_id} does not exist", 404)
            used = {n.id for n in ent.notes}
            nid = next(f"n{i}" for i in range(1, len(used) + 2) if f"n{i}" not in used)
            note = Note(text=text.strip(), kind=kind, author=author or self.author, created=_dt.date.today().isoformat(), id=nid)
            data = edit.entity_to_dict(replace(ent, notes=(*ent.notes, note)))
            self.update(entity_id, data)
            return nid

    def update_note(self, entity_id: str, note_id: str, **changes: str) -> None:
        with self._lock:
            ent = self.model.get(entity_id)
            if ent is None:
                raise WorkspaceError(f"{entity_id} does not exist", 404)
            if note_id not in {n.id for n in ent.notes}:
                raise WorkspaceError(f"{entity_id} has no note {note_id}", 404)
            notes = tuple(replace(n, **{k: v for k, v in changes.items() if v is not None}) if n.id == note_id else n for n in ent.notes)
            self.update(entity_id, edit.entity_to_dict(replace(ent, notes=notes)))

    def delete_note(self, entity_id: str, note_id: str) -> None:
        with self._lock:
            ent = self.model.get(entity_id)
            if ent is None:
                raise WorkspaceError(f"{entity_id} does not exist", 404)
            notes = tuple(n for n in ent.notes if n.id != note_id)
            if len(notes) == len(ent.notes):
                raise WorkspaceError(f"{entity_id} has no note {note_id}", 404)
            self.update(entity_id, edit.entity_to_dict(replace(ent, notes=notes)))

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

    def git_status(self) -> dict[str, Any]:
        if not self.is_git():
            return {"git": False}
        branch = self.git("rev-parse", "--abbrev-ref", "HEAD", check=False).strip()
        head = self.git("rev-parse", "--short", "HEAD", check=False).strip()
        porcelain = self.git("status", "--porcelain", "--", *self.model_paths, check=False)
        changed = [line[3:] for line in porcelain.splitlines() if line.strip()]
        name = self.git("config", "user.name", check=False).strip()
        email = self.git("config", "user.email", check=False).strip()
        return {"git": True, "branch": branch, "head": head, "changed": changed, "user": f"{name} <{email}>" if name else ""}

    def refs(self) -> dict[str, Any]:
        if not self.is_git():
            return {"branches": [], "tags": [], "head": ""}
        branches = [b.strip() for b in self.git("for-each-ref", "--format=%(refname:short)", "refs/heads", "refs/remotes").splitlines() if b.strip() and not b.endswith("/HEAD")]
        tags = []
        for line in self.git("for-each-ref", "--sort=-creatordate", "--format=%(refname:short)\t%(objectname:short)\t%(creatordate:short)\t%(contents:subject)", "refs/tags").splitlines():
            parts = line.split("\t")
            if parts and parts[0]:
                tags.append({"name": parts[0], "sha": parts[1] if len(parts) > 1 else "", "date": parts[2] if len(parts) > 2 else "", "message": parts[3] if len(parts) > 3 else ""})
        return {"branches": branches, "tags": tags, "head": self.git("rev-parse", "--abbrev-ref", "HEAD", check=False).strip()}

    def log(self, limit: int = 30, ref: str = "HEAD") -> list[dict[str, str]]:
        if not self.is_git():
            return []
        out = self.git("log", f"-n{int(limit)}", "--format=%H%x1f%h%x1f%an%x1f%ad%x1f%s", "--date=short", self._check_ref(ref), "--", *self.model_paths, check=False)
        rows = []
        for line in out.splitlines():
            full, short, author, date, subject = (line.split("\x1f") + ["", "", "", "", ""])[:5]
            rows.append({"sha": full, "short": short, "author": author, "date": date, "subject": subject})
        return rows

    def model_at(self, ref: str) -> Model:
        """The model as committed at ``ref`` (``WORKTREE`` = the files on disk)."""
        if ref in ("", "WORKTREE"):
            return self.model
        ref = self._check_ref(ref)
        listing = self.git("ls-tree", "-r", "--name-only", ref, "--", *self.model_paths)
        docs: list[tuple[str, Any]] = []
        for rel in sorted(p for p in listing.splitlines() if p.endswith((".yaml", ".yml", ".json"))):
            text = self.git("show", f"{ref}:{rel}")
            try:
                docs.extend((rel, d) for d in yaml.safe_load_all(text) if d is not None)
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
        if subprocess.run(["git", "check-ref-format", f"refs/tags/{name}"], capture_output=True).returncode != 0 or name.startswith("-"):
            raise WorkspaceError(f"invalid tag name {name!r}")
        if self.git("tag", "--list", name).strip():
            raise WorkspaceError(f"tag {name} already exists", 409)
        args = ["tag", "-a", name, "-m", message or f"requirements baseline {name}", self._check_ref(ref)]
        self.git(*args, env=self._identity_env(author or self.author))

    def commit(self, message: str, author: str = "") -> str:
        if not message.strip():
            raise WorkspaceError("a commit message is required")
        status = self.git_status()
        if not status.get("changed"):
            raise WorkspaceError("no model changes to commit", 409)
        self.git("add", "-A", "--", *self.model_paths)
        args = ["commit", "-m", message, "--", *self.model_paths]
        author = author or self.author
        m = re.match(r"^\s*(.+?)\s*<([^>]+)>\s*$", author or "")
        if m:
            args[1:1] = ["--author", f"{m.group(1)} <{m.group(2)}>"]
        self.git(*args, env=self._identity_env(author))
        return self.git("rev-parse", "--short", "HEAD").strip()


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
        outgoing.append({"id": target, "kind": t.kind if t else "", "relation": rel, "title": t.title if t else "", "status": _status(snap, target), "missing": t is None})
    return {
        "id": ent.id,
        "kind": ent.kind,
        "data": edit.entity_to_dict(ent),
        "location": {"path": ent.location.path, "line": ent.location.line},
        "status": verdict.status if verdict else "",
        "demanded": verdict.demanded if verdict else "",
        "provided": verdict.provided if verdict else "",
        "stale": bool(verdict and verdict.stale),
        "pyramid_violation": bool(verdict and verdict.pyramid_violation),
        "evidence": [e.to_dict() | ({"source": e.source} if e.source else {}) for e in (verdict.evidence if verdict else [])],
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
    return {"severity": i.severity, "code": i.code, "message": i.message, "entity": i.entity, "path": i.location.path, "line": i.location.line}


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
                    "data": edit.entity_to_dict(ent),
                }
            )
    return rows
