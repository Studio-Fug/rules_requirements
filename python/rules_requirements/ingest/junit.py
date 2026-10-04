# SPDX-License-Identifier: AGPL-3.0-or-later
"""JUnit XML ingestor — the standard evidence format.

Accepts the common dialects: a bare ``<testsuite>`` or ``<testsuites>`` root,
nested suites, xunit2 per-testcase ``<properties>`` (pytest, our writers,
googletest >= 1.10) and property *attributes* on ``<testcase>`` (older
googletest ``RecordProperty`` output). Suite-level ``<properties>`` apply to
every case in the suite (a convenient place for ``level`` and
``artifact.*`` stamps).

Bazel writes one ``test.xml`` per test target under ``bazel-testlogs``; the
target label is recovered from that path so ``verified_by`` target traces work.
"""

from __future__ import annotations

import re
from typing import Iterable
from xml.etree import ElementTree as ET

from rules_requirements.ingest import (
    ERROR,
    FAILED,
    PASSED,
    SKIPPED,
    Ingestor,
    TestCase,
    apply_properties,
)

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
# Any other testlogs directory (rr_evidence's output, <name>/testlogs).
_TESTLOGS_DIR = re.compile(r"(?:^|/)testlogs/")


def target_from_path(path: str) -> str:
    """``.../bazel-testlogs/pkg/sub/name/test.xml`` -> ``//pkg/sub:name``.

    Also handles the resolved form (``bazel-out/<cfg>/testlogs/...``), the
    external-repo form (``testlogs/external/<repo>/pkg/name``) and sharded /
    retried runs (``.../name/shard_1_of_4/test.xml``, ``run_2_of_3``,
    ``test_attempts/attempt_1.xml``). Returns "" outside a testlogs tree.
    """
    norm = path.replace("\\", "/")
    m = _TESTLOGS_ROOT.search(norm) or _TESTLOGS_DIR.search(norm)
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


FILE_PROPERTY = "rr.file"
"""The test source a case came from (our writers; else the ``file`` attribute)."""

SYNTHETIC_PROPERTY = "rr.synthetic"
"""``true``: the target's single whole-run result (no per-case output)."""

SCOPE_PROPERTY = "rr.scope"
"""``target``: a result about the whole target run (exit status, load error), not a test case."""

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


class JUnitIngestor(Ingestor):
    name = "junit"
    suffixes = (".xml",)

    def sniff(self, path: str, head: bytes) -> bool:
        return path.endswith(".xml") and (b"<testsuite" in head or b"<testcase" in head)

    def ingest(self, path: str) -> Iterable[TestCase]:
        target = target_from_path(path)
        try:
            # Test reports are produced by the build itself; expat (>= 2.4)
            # also refuses entity-expansion bombs.
            root = ET.parse(path).getroot()  # noqa: S314
        except (ET.ParseError, OSError) as exc:
            # A report we cannot read must not silently vanish: it may well be
            # the report of a failing run (e.g. control characters in a log).
            name = target.rsplit(":", 1)[-1] if target else path.rsplit("/", 1)[-1]
            return [
                TestCase(name=name, status=ERROR, message=f"unreadable JUnit report: {exc}", source=path, target=target)
            ]
        return list(self._suite(root, [], path, target))

    def _suite(self, el: ET.Element, inherited: list[tuple[str, str]], path: str, target: str) -> Iterable[TestCase]:
        # Properties on <testsuites> or <testsuite> apply to every case below
        # (googletest writes RecordProperty calls made outside tests there).
        props = inherited + (_props(el) if el.tag in ("testsuite", "testsuites") else [])
        suite = el.get("name", "") if el.tag == "testsuite" else ""
        synthetic = is_bazel_generated(el)
        for child in el:
            if child.tag in ("testsuite", "testsuites"):
                yield from self._suite(child, props, path, target)
            elif child.tag == "testcase":
                status, message = _status(child)
                case = TestCase(
                    name=child.get("name", ""),
                    classname=child.get("classname", ""),
                    status=status,
                    message=message,
                    duration=_duration(child.get("time")),
                    source=path,
                    target=target,
                    suite=suite,
                )
                if synthetic:
                    case.properties[SYNTHETIC_PROPERTY] = "true"
                attrs = [(k, v) for k, v in child.attrib.items() if k in _TRACE_ATTRS]
                extra = [(k, v) for k, v in child.attrib.items() if k not in _STANDARD_ATTRS and k not in _TRACE_ATTRS]
                case = apply_properties(case, props + attrs + extra + _props(child))
                if child.get("file") and FILE_PROPERTY not in case.properties:
                    # The standard attribute (rr_case.h, googletest, pytest's
                    # xunit1) names the source when no rr.file property does.
                    case.properties[FILE_PROPERTY] = child.get("file", "")
                yield case
