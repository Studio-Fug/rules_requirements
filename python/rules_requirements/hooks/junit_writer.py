# SPDX-License-Identifier: AGPL-3.0-or-later
"""Write traceability-tagged JUnit XML from any hand-rolled test harness.

Useful for on-hardware (HIL/HITL) runners that are plain programs rather than
test-framework suites::

    from rules_requirements.hooks.junit_writer import JUnitWriter

    report = JUnitWriter("bench_e2e", default_level="hitl",
                         artifact={"firmware_build_id": build_id})
    with report.case("flash_and_boot", requirement="REQ-13"):
        flash(dut)               # an exception records a failure, then re-raises
    report.write(os.environ.get("XML_OUTPUT_FILE", "bench_e2e.xml"))

Each case names at most one requirement: a test case verifies at most one
requirement. The pre-0.2 list form (``report.case("x", ["REQ-13", "REQ-21"])``)
still records every id, with a :class:`~rules_requirements.hooks.ids.MultipleRequirementsWarning`.

The ``artifact`` identity is stamped on every case (``artifact.<key>``
properties) so the report can mark evidence from an older build as stale, and
the harness's source file (``rr.file``) identifies the test code that ran.
For runs made of ordered steps and checks, see
:class:`~rules_requirements.hooks.checkplan.CheckPlan`.
"""

from __future__ import annotations

import errno
import os
import re
import stat
import sys
import time
from collections.abc import Iterable as _Iterable
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Mapping, Optional, Union
from xml.etree import ElementTree as ET

from rules_requirements.hooks.ids import check_id, split_ids, warn_multiple

_STATUSES = ("passed", "failed", "error", "skipped")

#: Property naming the source file of the test code that produced a case.
FILE_PROPERTY = "rr.file"

# What ``requirement=`` accepts: one id, or (deprecated) a list of them.
Requirement = Union[str, Iterable[str], None]


@dataclass
class _Case:
    name: str
    requirements: list[str]
    status: str = "passed"
    message: str = ""
    duration: float = 0.0
    level: str = ""
    artifact: dict[str, str] = field(default_factory=dict)
    classname: str = ""
    file: str = ""

    @property
    def requirement(self) -> str | None:
        """The id this case verifies (the first, for a deprecated multi-id case)."""
        return self.requirements[0] if self.requirements else None


# Characters XML 1.0 cannot represent (ANSI escapes in serial logs, NULs from
# crashing binaries...). Left in, they make the whole report unparsable.
_INVALID_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")


def xml_safe(text: str) -> str:
    """``text`` with XML-invalid characters replaced by ``#xNN`` (like pytest)."""
    return _INVALID_XML.sub(lambda m: f"#x{ord(m.group(0)):02X}", str(text))


def _summary(message: str) -> str:
    """One-line summary: the exception line of a traceback, else the first line."""
    lines = [ln for ln in message.strip().splitlines() if ln.strip()]
    if not lines:
        return ""
    return lines[-1].strip() if lines[0].startswith("Traceback") else lines[0].strip()


_RUNFILES = re.compile(r"\.runfiles/([^/]+)/(.+)$")
_BAZEL_BIN = re.compile(r"/bazel-out/[^/]+/bin/(.+)$")


def source_file(path: str) -> str:
    """``path`` relative to the workspace root, as ``rr.file`` records it.

    Under Bazel a test runs from its runfiles tree (``<x>.runfiles/<repo>/...``)
    or the output tree (``bazel-out/<cfg>/bin/...``); both prefixes are
    stripped, so the result is the path of the source in the workspace
    (``external/<repo>/...`` for another repository's file). Elsewhere it is
    relative to ``$BUILD_WORKSPACE_DIRECTORY`` or the current directory when
    under it, else absolute. ``""`` stays ``""``.
    """
    if not path:
        return ""
    norm = os.path.abspath(path).replace(os.sep, "/")
    m = _RUNFILES.search(norm)
    if m:
        repo, rest = m.groups()
        main = {"_main", "__main__", os.environ.get("TEST_WORKSPACE") or "_main"}
        return rest if repo in main else f"external/{repo}/{rest}"
    m = _BAZEL_BIN.search(norm)
    if m:
        return m.group(1)
    root = os.environ.get("BUILD_WORKSPACE_DIRECTORY") or os.getcwd()
    rel = os.path.relpath(norm, root).replace(os.sep, "/")
    return norm if rel == ".." or rel.startswith("../") else rel


def _resolve_ids(requirement: Any, requirements: Any, subject: str, stacklevel: int) -> list[str]:
    """The ids to record for one case, from ``requirement=`` or the legacy list.

    One id string is checked (RR-E104); any other iterable (a list, tuple,
    set, generator...) is the deprecated form, recorded verbatim, with a
    warning when it names several ids. ``stacklevel`` is that of the warning
    as seen from the caller of ``_resolve_ids`` (2: the caller's caller).
    """
    if requirement is not None and requirements is not None:
        raise TypeError(f"{subject}: pass requirement= or the deprecated requirements=, not both")
    value = requirement if requirements is None else requirements
    if value is None:
        return []
    if isinstance(value, str):
        return [check_id(value, f"{subject}: requirement")]
    if isinstance(value, bytes) or not isinstance(value, _Iterable):
        raise TypeError(f"{subject}: requirement must be one id string, got {type(value).__name__}")
    values = list(value)
    ids = split_ids(values)
    if len(ids) > 1:
        warn_multiple(subject, ids, stacklevel=stacklevel + 1)
    return [str(v) for v in values]


@dataclass
class JUnitWriter:
    """Collects test cases and writes them as one JUnit ``<testsuite>``.

    ``file`` is the source file of the harness, written as the ``rr.file``
    property of every case; by default the running script (``sys.argv[0]``)
    relative to the workspace. Pass ``""`` to write none.
    """

    suite: str
    classname: str = ""
    default_level: str = ""
    artifact: dict[str, str] = field(default_factory=dict)
    cases: list[_Case] = field(default_factory=list)
    file: Optional[str] = None

    def __post_init__(self) -> None:
        if self.file is None:
            self.file = source_file(sys.argv[0] if sys.argv and sys.argv[0] not in ("", "-", "-c") else "")

    def add(
        self,
        name: str,
        requirement: Requirement = None,
        status: str = "passed",
        message: str = "",
        duration: float = 0.0,
        level: str = "",
        artifact: dict[str, str] | None = None,
        classname: str = "",
        *,
        requirements: Iterable[str] | None = None,
        file: str | None = None,
    ) -> None:
        """Record one case.

        ``requirement`` is the one id the case verifies (or ``None``). A list
        or tuple there, or in the deprecated ``requirements=`` keyword, still
        records every id it holds, with a
        :class:`~rules_requirements.hooks.ids.MultipleRequirementsWarning` when
        it names several. ``file`` overrides the writer's source file.
        """
        # the warning points at the caller of add()
        ids = _resolve_ids(requirement, requirements, f"JUnitWriter case {name!r}", stacklevel=2)
        self._append(name, ids, status, message, duration, level, artifact, classname, file)

    def _append(
        self,
        name: str,
        ids: Iterable[str] = (),
        status: str = "passed",
        message: str = "",
        duration: float = 0.0,
        level: str = "",
        artifact: Mapping[str, str] | None = None,
        classname: str = "",
        file: str | None = None,
    ) -> _Case:
        """Record a case with ``ids`` as given (for transcribing other hooks' evidence)."""
        if status not in _STATUSES:
            raise ValueError(f"status must be one of {_STATUSES}, got {status!r}")
        case = _Case(
            name=name,
            requirements=list(ids),
            status=status,
            message=message,
            duration=duration,
            level=level or self.default_level,
            artifact={**self.artifact, **(artifact or {})},
            classname=classname or self.classname or self.suite,
            file=(self.file or "") if file is None else file,
        )
        self.cases.append(case)
        return case

    @contextmanager
    def case(
        self,
        name: str,
        requirement: Requirement = None,
        level: str = "",
        artifact: dict[str, str] | None = None,
        *,
        requirements: Iterable[str] | None = None,
        classname: str = "",
        file: str | None = None,
    ) -> Iterator[None]:
        """Record ``name`` as passed, or failed if the block raises (re-raised)."""
        # this generator runs under contextlib's __enter__: the warning points
        # at the ``with`` statement, one frame further up
        ids = _resolve_ids(requirement, requirements, f"JUnitWriter case {name!r}", stacklevel=3)
        start = time.monotonic()
        try:
            yield
        except BaseException as exc:
            self._append(
                name,
                ids,
                "failed",
                f"{type(exc).__name__}: {exc}",
                time.monotonic() - start,
                level,
                artifact,
                classname,
                file,
            )
            raise
        self._append(name, ids, "passed", "", time.monotonic() - start, level, artifact, classname, file)

    def not_reached(
        self,
        names: Iterable[str],
        reason: str,
        classname: str = "",
        *,
        tags: Mapping[str, str] | None = None,
        level: str = "",
        artifact: dict[str, str] | None = None,
    ) -> None:
        """Record planned cases a device failure kept from running.

        Each name becomes its own failed case, ``"not reached: <reason>"``, so
        the requirement each one verifies fails through its own case.
        ``tags`` maps a name to the one id that case verifies.
        """
        tags = dict(tags or {})
        ids = {n: check_id(rid, f"not_reached: {n}") for n, rid in tags.items()}
        for name in names:
            self._append(
                name,
                [ids[name]] if name in ids else [],
                "failed",
                f"not reached: {reason}",
                0.0,
                level,
                artifact,
                classname,
            )

    def to_element(self) -> ET.Element:
        x = xml_safe
        root = ET.Element("testsuites")
        suite = ET.SubElement(
            root,
            "testsuite",
            name=x(self.suite),
            tests=str(len(self.cases)),
            failures=str(sum(c.status == "failed" for c in self.cases)),
            errors=str(sum(c.status == "error" for c in self.cases)),
            skipped=str(sum(c.status == "skipped" for c in self.cases)),
            time=f"{sum(c.duration for c in self.cases):.3f}",
        )
        for c in self.cases:
            tc = ET.SubElement(suite, "testcase", classname=x(c.classname), name=x(c.name), time=f"{c.duration:.3f}")
            if c.requirements or c.level or c.artifact or c.file:
                props = ET.SubElement(tc, "properties")
                for rid in c.requirements:
                    ET.SubElement(props, "property", name="requirement", value=x(rid))
                if c.level:
                    ET.SubElement(props, "property", name="level", value=x(c.level))
                for key, value in sorted(c.artifact.items()):
                    ET.SubElement(props, "property", name=x(f"artifact.{key}"), value=x(value))
                if c.file:
                    ET.SubElement(props, "property", name=FILE_PROPERTY, value=x(c.file))
            if c.status in ("failed", "error"):
                tag = "failure" if c.status == "failed" else "error"
                ET.SubElement(tc, tag, message=x(_summary(c.message) or c.status)).text = x(c.message)
            elif c.status == "skipped":
                ET.SubElement(tc, "skipped", message=x(c.message))
        return root

    def to_string(self) -> str:
        return ET.tostring(self.to_element(), encoding="unicode")

    def write(self, path: str, append: bool = False) -> None:
        """Write the JUnit XML to ``path``.

        With ``append``, the cases are added to the JUnit already at ``path``
        (to this writer's suite there, else as a new suite); a missing or
        empty file is written afresh. The new file replaces the old one
        atomically and keeps its permission bits; like any replacement it
        needs a writable directory, but not a writable file. Where ``fcntl``
        exists (not on Windows) appends from concurrent processes
        (``rr case ... &``) are serialised by a lock on the file, so none is
        lost; a read-only file on NFS and a dangling symlink, which cannot be
        locked themselves, are serialised by a sidecar lock file
        (``.<name>.lock``) next to them, removed again.
        """
        if not append:
            tree = ET.ElementTree(self.to_element())
            ET.indent(tree)
            tree.write(path, encoding="utf-8", xml_declaration=True)
            return
        with _locked(path) as mode:
            root = self.to_element()
            if os.path.exists(path) and os.path.getsize(path) > 0:
                root = _merge(_read_root(path), root)
            tree = ET.ElementTree(root)
            ET.indent(tree)
            fd, tmp = _create_temp(path)
            try:
                with os.fdopen(fd, "wb") as fh:
                    tree.write(fh, encoding="utf-8", xml_declaration=True)
                if mode is not None:
                    os.chmod(tmp, mode)
                os.replace(tmp, path)
            except BaseException:
                if os.path.exists(tmp):
                    os.remove(tmp)
                raise


@contextmanager
def _locked(path: str) -> Iterator[Optional[int]]:
    """Hold an exclusive lock for appending to the file at ``path`` (created
    empty if missing); yields its permission bits for the replacement file, or
    ``None`` when there is no file to keep the bits of.

    The file is replaced (``os.replace``), not rewritten, so appending needs
    only a writable directory, as it did before the lock: the file is opened
    for writing where allowed (an NFS client's ``flock`` needs that for an
    exclusive lock), and read-only otherwise (a local ``flock`` needs no
    write access). Where the file itself cannot be locked, a sidecar lock
    file next to it (``.<name>.lock``, removed again) is locked instead: for
    a read-only file on NFS, and for a dangling symlink, which is not
    followed (that would leave its target behind, empty) and is replaced by
    the append. A symlink to an existing file is followed for the lock and
    then replaced like any file. Because the file is replaced, a waiter may
    end up holding the lock on an unlinked inode: it then retries on the
    current file.
    """
    try:
        import fcntl
    except ImportError:  # pragma: no cover - Windows: no locking
        yield None
        return
    while True:
        fd = _open_to_lock(path)
        if fd is None:  # a dangling symlink: nothing to lock but a sidecar
            with _sidecar_locked(path):
                if _dangling(path):
                    yield None
                    return
            continue  # an append replaced the link meanwhile: lock that file
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
            except OSError as exc:  # NFS: no exclusive lock on a read-only fd
                if exc.errno != errno.EBADF or fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE != os.O_RDONLY:
                    raise
                with _sidecar_locked(path):
                    if _same_file(fd, path):
                        yield stat.S_IMODE(os.fstat(fd).st_mode)
                        return
                continue
            if _same_file(fd, path):
                yield stat.S_IMODE(os.fstat(fd).st_mode)
                return
        finally:
            os.close(fd)  # also releases the lock


@contextmanager
def _sidecar_locked(path: str) -> Iterator[None]:
    """Hold an exclusive lock on ``.<name>.lock`` next to ``path``, for
    appends that cannot lock the file itself. The sidecar is opened for
    writing (NFS needs that) and removed before the lock is released;
    a waiter left holding the removed one retries on the current sidecar."""
    import fcntl

    lock = os.path.join(os.path.dirname(path), f".{os.path.basename(path)}.lock")
    while True:
        fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o666)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            if _same_file(fd, lock):
                try:
                    yield
                finally:
                    os.remove(lock)
                return
        finally:
            os.close(fd)


def _same_file(fd: int, path: str) -> bool:
    """Whether ``path`` (followed if a symlink) is still the file open as ``fd``."""
    held, current = os.fstat(fd), _stat(path)
    return current is not None and (held.st_dev, held.st_ino) == (current.st_dev, current.st_ino)


def _dangling(path: str) -> bool:
    return os.path.islink(path) and not os.path.exists(path)


def _create_temp(path: str) -> tuple[int, str]:
    """A new file next to ``path``, created with the mode a plain write would
    give (0666 less the umask), not ``mkstemp``'s 0600: the replacement keeps
    that mode when there is no old file's mode to keep (a dangling symlink;
    Windows)."""
    while True:
        tmp = os.path.join(os.path.dirname(path), f".{os.path.basename(path)}.{os.urandom(6).hex()}")
        try:
            return os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o666), tmp
        except FileExistsError:
            continue


def _open_to_lock(path: str) -> Optional[int]:
    """An fd on the file at ``path``, created if missing; ``None`` for a
    dangling symlink. No O_EXCL: it refuses every symlink, so a first writer
    that met a dangling one would retry forever."""
    if _dangling(path):
        return None
    try:
        return os.open(path, os.O_RDWR | os.O_CREAT, 0o666)
    except PermissionError:  # a read-only file in a writable directory
        return os.open(path, os.O_RDONLY | os.O_CREAT, 0o666)


def _stat(path: str) -> Optional[os.stat_result]:
    try:
        return os.stat(path)
    except FileNotFoundError:
        return None


def _read_root(path: str) -> ET.Element:
    # The file was written by this run's own harness or test binary.
    existing = ET.parse(path).getroot()  # noqa: S314
    if existing.tag == "testsuites":
        return existing
    if existing.tag == "testsuite":
        root = ET.Element("testsuites")
        root.append(existing)
        return root
    raise ValueError(f"{path}: not JUnit XML (root element <{existing.tag}>)")


def _merge(root: ET.Element, new: ET.Element) -> ET.Element:
    """``root`` with the suites of ``new`` merged in, by suite name."""
    for suite in new.findall("testsuite"):
        same = next((s for s in root.findall("testsuite") if s.get("name") == suite.get("name")), None)
        if same is None:
            root.append(suite)
            continue
        same.extend(suite.findall("testcase"))
        cases = same.findall("testcase")
        same.set("tests", str(len(cases)))
        for attr, tag in (("failures", "failure"), ("errors", "error"), ("skipped", "skipped")):
            same.set(attr, str(sum(c.find(tag) is not None for c in cases)))
        same.set("time", f"{sum(_seconds(c.get('time')) for c in cases):.3f}")
    return root


def _seconds(value: str | None) -> float:
    try:
        return float(value or 0)
    except ValueError:
        return 0.0


def __getattr__(name: str) -> Any:
    # CheckPlan lives in its own module (it builds on JUnitWriter) and is
    # re-exported here lazily to avoid an import cycle.
    if name == "CheckPlan":
        from rules_requirements.hooks.checkplan import CheckPlan

        return CheckPlan
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
