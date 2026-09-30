# SPDX-License-Identifier: AGPL-3.0-or-later
"""Write traceability-tagged JUnit XML from any hand-rolled test harness.

Useful for on-hardware (HIL/HITL) runners that are plain programs rather than
test-framework suites::

    from rules_requirements.hooks.junit_writer import JUnitWriter

    report = JUnitWriter("bench_e2e", default_level="hitl",
                         artifact={"firmware_build_id": build_id})
    with report.case("flash_and_boot", ["REQ-13", "REQ-21"]):
        flash(dut)               # an exception records a failure, then re-raises
    report.write(os.environ.get("XML_OUTPUT_FILE", "bench_e2e.xml"))

The ``artifact`` identity is stamped on every case (``artifact.<key>``
properties) so the report can mark evidence from an older build as stale.
"""

from __future__ import annotations

import re
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator
from xml.etree import ElementTree as ET

_STATUSES = ("passed", "failed", "error", "skipped")


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


@dataclass
class JUnitWriter:
    suite: str
    classname: str = ""
    default_level: str = ""
    artifact: dict[str, str] = field(default_factory=dict)
    cases: list[_Case] = field(default_factory=list)

    def add(
        self,
        name: str,
        requirements: list[str] | tuple[str, ...] = (),
        status: str = "passed",
        message: str = "",
        duration: float = 0.0,
        level: str = "",
        artifact: dict[str, str] | None = None,
        classname: str = "",
    ) -> None:
        if status not in _STATUSES:
            raise ValueError(f"status must be one of {_STATUSES}, got {status!r}")
        self.cases.append(
            _Case(
                name=name,
                requirements=list(requirements),
                status=status,
                message=message,
                duration=duration,
                level=level or self.default_level,
                artifact={**self.artifact, **(artifact or {})},
                classname=classname or self.classname or self.suite,
            )
        )

    @contextmanager
    def case(
        self,
        name: str,
        requirements: list[str] | tuple[str, ...] = (),
        level: str = "",
        artifact: dict[str, str] | None = None,
    ) -> Iterator[None]:
        """Record ``name`` as passed, or failed if the block raises (re-raised)."""
        start = time.monotonic()
        try:
            yield
        except BaseException as exc:
            self.add(
                name, requirements, "failed", f"{type(exc).__name__}: {exc}", time.monotonic() - start, level, artifact
            )
            raise
        self.add(name, requirements, "passed", "", time.monotonic() - start, level, artifact)

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
            if c.requirements or c.level or c.artifact:
                props = ET.SubElement(tc, "properties")
                for rid in c.requirements:
                    ET.SubElement(props, "property", name="requirement", value=x(rid))
                if c.level:
                    ET.SubElement(props, "property", name="level", value=x(c.level))
                for key, value in sorted(c.artifact.items()):
                    ET.SubElement(props, "property", name=x(f"artifact.{key}"), value=x(value))
            if c.status in ("failed", "error"):
                tag = "failure" if c.status == "failed" else "error"
                ET.SubElement(tc, tag, message=x(_summary(c.message) or c.status)).text = x(c.message)
            elif c.status == "skipped":
                ET.SubElement(tc, "skipped", message=x(c.message))
        return root

    def to_string(self) -> str:
        return ET.tostring(self.to_element(), encoding="unicode")

    def write(self, path: str) -> None:
        tree = ET.ElementTree(self.to_element())
        ET.indent(tree)
        tree.write(path, encoding="utf-8", xml_declaration=True)
