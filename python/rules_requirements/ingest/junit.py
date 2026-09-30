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


def target_from_path(path: str) -> str:
    """``.../bazel-testlogs/pkg/sub/name/test.xml`` -> ``//pkg/sub:name``.

    Also handles the resolved form (``bazel-out/<cfg>/testlogs/...``), the
    external-repo form (``testlogs/external/<repo>/pkg/name``) and sharded /
    retried runs (``.../name/shard_1_of_4/test.xml``, ``run_2_of_3``,
    ``attempt_1.xml``). Returns "" outside a testlogs tree.
    """
    norm = path.replace("\\", "/")
    idx, marker = -1, ""
    for m in ("bazel-testlogs/", "/testlogs/"):
        idx = norm.rfind(m)
        if idx != -1:
            marker = m
            break
    if idx == -1:
        return ""
    parts = norm[idx + len(marker) :].split("/")
    if parts and parts[-1].endswith(".xml"):
        parts.pop()
    while parts and (
        parts[-1].startswith(("shard_", "run_", "attempt_")) or parts[-1] in ("test.outputs", "test_attempts")
    ):
        parts.pop()
    repo = ""
    if len(parts) > 2 and parts[0] == "external":
        repo, parts = "@" + parts[1], parts[2:]
    if len(parts) < 1 or not parts[-1]:
        return ""
    name = parts[-1]
    pkg = "/".join(parts[:-1])
    return f"{repo}//{pkg}:{name}"


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
                )
                attrs = [(k, v) for k, v in child.attrib.items() if k in _TRACE_ATTRS]
                extra = [(k, v) for k, v in child.attrib.items() if k not in _STANDARD_ATTRS and k not in _TRACE_ATTRS]
                yield apply_properties(case, props + attrs + extra + _props(child))
