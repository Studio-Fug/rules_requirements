# SPDX-License-Identifier: AGPL-3.0-or-later
"""JUnit XML ingestor — the standard evidence format.

Accepts the common dialects: a bare ``<testsuite>`` or ``<testsuites>`` root,
nested suites, xunit2 per-testcase ``<properties>`` (pytest, our writers,
googletest >= 1.10) and property *attributes* on ``<testcase>`` (older
googletest ``RecordProperty`` output). Of suite-level ``<properties>``, only
``level`` and ``artifact.*`` stamps reach the cases below; a suite-level
``requirement`` does not (it is recorded as ``suite-level-requirement``).

A ``<testcase>`` holding ``<testcase>`` children (subtests, as some runners
nest them) is a *scope*, not a case: each child becomes a case whose
classname is the parent's path joined with `` > `` (the shape rr_node_test
gives node:test subtests), and a failure of the parent itself that none of
its children explains becomes a target-scope ``<hooks>`` error.

A ``<testcase>`` whose subtests sit in a ``<testsuite>`` of their own is a
scope the same way. A nested case's own ``classname`` attribute is ignored:
its classname is always its parent's path, so a subtest keys under the case
that ran it.

A report that cannot be read at all is one target-scope error, so whatever
the target verified reads as tainted, never as missing. A file Bazel names as
a report (``test.xml`` or ``test_attempts/attempt_N.xml`` in a testlogs tree)
is read whatever its first bytes are: empty, binary or with its root element
after a long prolog, it is still the target's report.

Bazel writes one ``test.xml`` per test target under ``bazel-testlogs``; the
target label is recovered from that path so ``verified_by`` target traces work.
"""

from __future__ import annotations

import re
from typing import Iterable, Iterator
from xml.etree import ElementTree as ET

from rules_requirements.ingest import (
    ARTIFACT_PREFIX,
    ERROR,
    FAILED,
    FILE_PROPERTY,
    LEVEL_PROPERTY,
    NAME_TAG,
    PASSED,
    REQUIREMENT_PROPERTY,
    SCOPE_PROPERTY,
    SKIPPED,
    SYNTHETIC_PROPERTY,
    Ingestor,
    TestCase,
    apply_properties,
    name_tags,
    split_ids,
    workspace_relative,
)
from rules_requirements.util import dedupe

# Standard testcase attributes that are *not* traceability properties.
_STANDARD_ATTRS = {
    "name",
    "classname",
    "time",
    "status",
    "result",
    "timestamp",
    "file",
    "line",
    "assertions",
    "value_param",
    "type_param",
}
_TRACE_ATTRS = ("requirement", "requirements", "level")


# Bazel's test output directories: "shard_1_of_4", "run_2_of_3", and, for a
# sharded test run several times, the two in one component:
# "shard_1_of_4_run_2_of_3" (TestActionBuilder). Only these exact shapes are
# run levels: a target named run_tests or shard_test is a target.
SHARD_RUN = re.compile(r"^(?:shard_(\d+)_of_(\d+)(?:_run_(\d+)_of_(\d+))?|run_(\d+)_of_(\d+))$")
ATTEMPT = re.compile(r"^attempt_(\d+)\.xml$")
_RUN_LEVELS = ("test.outputs", "test_attempts")

# The root of a testlogs tree: the convenience symlink, or the output tree's
# own (bazel-out/<cfg>/testlogs). Matched leftmost, so a package that has a
# directory named testlogs is still a package.
_TESTLOGS_ROOT = re.compile(r"(?:^|/)(?:bazel-testlogs|bazel-out/[^/]+/testlogs)/")
# Any other testlogs directory (rr_evidence's output, <name>/testlogs). With no
# recognised bazel root present, the *innermost* (rightmost) one is the root, so
# an ancestor directory merely named testlogs does not swallow the package.
_TESTLOGS_DIR = re.compile(r"(?:^|/)testlogs/")


def is_testlogs_report(path: str) -> bool:
    """Whether ``path`` is a report Bazel itself names inside a testlogs tree:
    ``<target dir>/test.xml`` (also under ``shard_i_of_n`` / ``run_k_of_n``) or
    ``<target dir>/test_attempts/attempt_N.xml``.

    Such a file is JUnit by contract, whatever its first bytes say, so it is
    never skipped: an empty, binary or late-rooted one is read (and, if it
    cannot be, becomes the target's ``<unreadable>`` error).
    """
    norm = path.replace("\\", "/")
    parts = norm.split("/")
    if len(parts) < 2 or not target_from_path(norm):
        return False
    base, parent = parts[-1], parts[-2]
    if base == "test.xml":
        return parent not in _RUN_LEVELS
    return parent == "test_attempts" and ATTEMPT.match(base) is not None


def target_from_path(path: str) -> str:
    """``.../bazel-testlogs/pkg/sub/name/test.xml`` -> ``//pkg/sub:name``.

    Also handles the resolved form (``bazel-out/<cfg>/testlogs/...``), the
    external-repo form (``testlogs/external/<repo>/pkg/name``) and sharded /
    retried runs (``.../name/shard_1_of_4/test.xml``, ``run_2_of_3``,
    ``test_attempts/attempt_1.xml``). Returns "" outside a testlogs tree.
    """
    norm = path.replace("\\", "/")
    m = _TESTLOGS_ROOT.search(norm)
    if m is None:
        # No recognised bazel root: the innermost testlogs/ is the root.
        generic = list(_TESTLOGS_DIR.finditer(norm))
        m = generic[-1] if generic else None
    if m is None:
        return ""
    parts = norm[m.end() :].split("/")
    if parts and parts[-1].endswith(".xml"):
        parts.pop()
    while parts and (SHARD_RUN.match(parts[-1]) or parts[-1] in _RUN_LEVELS):
        parts.pop()
    repo = ""
    if len(parts) > 2 and parts[0] == "external":
        repo, parts = "@" + parts[1], parts[2:]
    if len(parts) < 1 or not parts[-1]:
        return ""
    name = parts[-1]
    pkg = "/".join(parts[:-1])
    return f"{repo}//{pkg}:{name}"


# FILE_PROPERTY, SYNTHETIC_PROPERTY and SCOPE_PROPERTY live in the ingest
# package (re-exported here, where M1 defined them).
__all__ = [
    "FILE_PROPERTY",
    "SCOPE_PROPERTY",
    "SYNTHETIC",
    "SYNTHETIC_PROPERTY",
    "TARGET_SCOPE",
    "JUnitIngestor",
    "is_bazel_generated",
    "is_testlogs_report",
    "target_from_path",
]

UNREADABLE_NAME = "<unreadable>"
"""Name of the target-scope error recorded for a report that cannot be parsed."""

HOOKS_NAME = "<hooks>"
"""Name of the target-scope error for a parent case's own failure (cf. rr_node_test)."""

SYNTHETIC = {SYNTHETIC_PROPERTY: "true"}
TARGET_SCOPE = {SCOPE_PROPERTY: "target"}


def is_bazel_generated(suite: ET.Element) -> bool:
    """Whether ``suite`` is the report Bazel writes for a test that wrote none.

    Bazel's ``generate-xml.sh`` fingerprint: a ``<testsuite name=N>`` holding
    exactly one ``<testcase name=N status="run">`` without a classname, and a
    ``<system-out>`` that starts with "Generated test.log". Such a result says
    only how the whole target ended — it has no per-case identity.
    """
    if suite.tag != "testsuite":
        return False
    cases = suite.findall("testcase")
    if len(cases) != 1 or suite.findall("testsuite"):
        return False
    case, name = cases[0], suite.get("name", "")
    if not name or case.get("name") != name or case.get("classname") or case.get("status") != "run":
        return False
    out = suite.find("system-out")
    if out is None:
        out = case.find("system-out")
    return out is not None and (out.text or "").lstrip().startswith("Generated test.log")


def _status(case: ET.Element) -> tuple[str, str]:
    for tag, status in (("error", ERROR), ("failure", FAILED), ("skipped", SKIPPED)):
        el = case.find(tag)
        if el is not None:
            msg = el.get("message") or (el.text or "").strip()
            return status, msg.strip()
    # googletest marks disabled/filtered tests this way.
    if case.get("status") == "notrun" or case.get("result") in ("skipped", "suppressed"):
        return SKIPPED, ""
    return PASSED, ""


def _props(el: ET.Element) -> list[tuple[str, str]]:
    return [(p.get("name", ""), p.get("value", p.text or "")) for p in el.findall("./properties/property")]


def _duration(value: str | None) -> float:
    try:
        return float(value or 0)
    except ValueError:
        return 0.0


def _inheritable(props: list[tuple[str, str]]) -> tuple[list[tuple[str, str]], list[str]]:
    """Split scope-level properties into what reaches the cases below
    (``level``, ``artifact.*``) and the requirement ids, which do not."""
    keep: list[tuple[str, str]] = []
    ids: list[str] = []
    for name, value in props:
        if name in (REQUIREMENT_PROPERTY, "requirements"):
            ids.extend(split_ids(value))
        elif name == LEVEL_PROPERTY or name.startswith(ARTIFACT_PREFIX):
            keep.append((name, value))
    return keep, ids


def _line(value: str | None) -> int:
    try:
        return max(int(value or 0), 0)
    except ValueError:
        return 0


class _Scope:
    """What an enclosing suite or parent case hands down to the cases below."""

    def __init__(
        self,
        props: list[tuple[str, str]] | None = None,
        ids: list[str] | None = None,
        classname: str = "",
        file: str = "",
    ) -> None:
        self.props = props or []  # level / artifact.* only
        self.ids = ids or []  # named at this or an enclosing scope: NOT inherited
        self.classname = classname  # a parent case's path (nested <testcase>), else ""
        self.file = file  # a parent case's source

    def below(self, el: ET.Element) -> _Scope:
        # A requirement written as an attribute of the suite is suite-level
        # too: not inherited, reported like the property form.
        attrs = [(k, v) for k, v in el.attrib.items() if k in (REQUIREMENT_PROPERTY, "requirements")]
        keep, ids = _inheritable(attrs + _props(el))
        return _Scope(self.props + keep, self.ids + ids, self.classname, self.file)


_JUNIT_TAGS = ("testsuites", "testsuite", "testcase")


def _unreadable(path: str, target: str, why: str) -> TestCase:
    return TestCase(
        name=UNREADABLE_NAME,
        status=ERROR,
        message=f"unreadable JUnit report: {why}",
        source=path,
        target=target,
        properties=dict(TARGET_SCOPE),
    )


def suite_names(path: str) -> tuple[str, ...]:
    """The names of every ``<testsuite>`` in the JUnit report at ``path``, in
    document order without repeats (``""`` for an unnamed one): the suite
    names its cases would be keyed under. ``()`` when it cannot be read or
    holds no ``<testsuite>``."""
    try:
        root = ET.parse(path).getroot()  # noqa: S314 - a test report, as in JUnitIngestor.ingest
    except (ET.ParseError, OSError):
        return ()
    return tuple(dict.fromkeys(el.get("name", "") for el in root.iter("testsuite")))


class JUnitIngestor(Ingestor):
    name = "junit"
    suffixes = (".xml",)

    def sniff(self, path: str, head: bytes) -> bool:
        if not path.endswith(".xml"):
            return False
        return b"<testsuite" in head or b"<testcase" in head or is_testlogs_report(path)

    def ingest(self, path: str) -> Iterable[TestCase]:
        target = target_from_path(path)
        try:
            # Test reports are produced by the build itself; expat (>= 2.4)
            # also refuses entity-expansion bombs.
            root = ET.parse(path).getroot()  # noqa: S314
        except (ET.ParseError, OSError) as exc:
            # A report we cannot read must not silently vanish: it may well be
            # the report of a failing run (e.g. control characters in a log,
            # or an empty file left by a crash). It is about the whole run
            # (rr.scope=target), so every member claimed on the target reads
            # tainted rather than missing.
            return [_unreadable(path, target, str(exc) or type(exc).__name__)]
        if root.tag not in _JUNIT_TAGS and root.find(".//testsuite") is None and root.find(".//testcase") is None:
            return [_unreadable(path, target, f"no JUnit content (root element <{root.tag}>)")]
        if root.tag == "testcase":  # a bare case as the whole report
            return list(self._case(root, _Scope(), path, target, ""))
        return list(self._suite(root, _Scope(), path, target))

    def _suite(self, el: ET.Element, scope: _Scope, path: str, target: str) -> Iterable[TestCase]:
        # Of the properties on <testsuites> or <testsuite>, level and
        # artifact.* apply to every case below; requirement ids do not.
        if el.tag in ("testsuite", "testsuites"):
            scope = scope.below(el)
        suite = el.get("name", "") if el.tag == "testsuite" else ""
        synthetic = is_bazel_generated(el)
        for child in el:
            if child.tag in ("testsuite", "testsuites"):
                yield from self._suite(child, scope, path, target)
            elif child.tag == "testcase":
                yield from self._case(child, scope, path, target, suite, synthetic)

    def _case(
        self,
        el: ET.Element,
        scope: _Scope,
        path: str,
        target: str,
        suite: str,
        synthetic: bool = False,
    ) -> Iterator[TestCase]:
        # Nested cases, directly or inside a <testsuite> of their own: either
        # way the <testcase> is a scope for them, never a case beside them.
        children = [c for c in el if c.tag in ("testcase", "testsuite")]
        status, message = _status(el)
        name = el.get("name", "")
        classname = scope.classname or el.get("classname", "")
        own_file = el.get("file", "")
        if children:
            nested = list(self._parent(el, children, scope, path, target, suite, classname, own_file))
            if nested:
                yield from nested
                return
            # Only empty nested suites: the <testcase> is an ordinary case.
        case = TestCase(
            name=name,
            classname=classname,
            status=status,
            message=message,
            duration=_duration(el.get("time")),
            source=path,
            target=target,
            suite=suite,
            line=_line(el.get("line")),
            suite_declared=tuple(dedupe(scope.ids)),
        )
        if synthetic:
            case.properties[SYNTHETIC_PROPERTY] = "true"
        attrs = [(k, v) for k, v in el.attrib.items() if k in _TRACE_ATTRS]
        extra = [(k, v) for k, v in el.attrib.items() if k not in _STANDARD_ATTRS and k not in _TRACE_ATTRS]
        case = apply_properties(case, scope.props + attrs + extra + _props(el))
        if FILE_PROPERTY not in case.properties and (own_file or scope.file):
            # The standard attribute (rr_case.h, googletest, pytest's
            # xunit1) names the source when no rr.file property does.
            case.properties[FILE_PROPERTY] = own_file or scope.file
        case.file = workspace_relative(case.properties.get(FILE_PROPERTY, ""))
        yield case

    def _parent(
        self,
        el: ET.Element,
        children: list[ET.Element],
        scope: _Scope,
        path: str,
        target: str,
        suite: str,
        classname: str,
        own_file: str,
    ) -> Iterator[TestCase]:
        """A ``<testcase>`` with ``<testcase>`` (or ``<testsuite>``) children: a scope for them."""
        leaf = NAME_TAG.sub("", el.get("name", "")).strip()
        props = [(k, v) for k, v in el.attrib.items() if k in _TRACE_ATTRS] + _props(el)
        keep, ids = _inheritable(props)
        inner = _Scope(
            scope.props + keep,
            # The parent's own requirement (property, attribute or name tag)
            # is scope-level: it does not reach the children.
            scope.ids + ids + name_tags(el.get("name", "")),
            f"{classname} > {leaf}" if classname else leaf,
            next((v for k, v in props if k == FILE_PROPERTY), "") or own_file or scope.file,
        )
        cases: list[TestCase] = []
        for child in children:
            if child.tag == "testsuite":
                cases.extend(self._suite(child, inner, path, target))
            else:
                cases.extend(self._case(child, inner, path, target, suite))
        if not cases:
            return  # nothing nested after all (empty suites): the caller reads a leaf
        yield from cases
        status, message = _status(el)
        if status in (FAILED, ERROR) and not any(c.is_failure for c in cases):
            # The parent failed on its own (a hook, its body outside the
            # subtests): about every case below, owned by none of them.
            yield TestCase(
                name=HOOKS_NAME,
                classname=inner.classname,
                status=ERROR,
                message=message or f"{inner.classname} failed outside its subtests",
                duration=_duration(el.get("time")),
                source=path,
                target=target,
                suite=suite,
                properties=dict(TARGET_SCOPE),
            )
