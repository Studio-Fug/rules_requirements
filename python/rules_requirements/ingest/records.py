# SPDX-License-Identifier: AGPL-3.0-or-later
"""Native evidence records (``*.rr.yaml`` / ``*.rr.json``).

For evidence no test runner produces — a signed-off inspection, an analysis
report, a bench measurement recorded by hand — write a records file::

    evidence:
      - name: enclosure-label-legible
        status: passed
        requirements: [REQ-12]
        level: inspection
        artifact: {board_rev: C}
        properties: {signed_by: "J. Doe", date: "2026-09-01", record: "QA-114"}

Each entry becomes one :class:`~rules_requirements.ingest.TestCase`.
"""

from __future__ import annotations

import json
from typing import Any, Iterable

from rules_requirements._vendor import yaml
from rules_requirements.ingest import STATUS_ORDER, Ingestor, TestCase, apply_properties


class RecordsIngestor(Ingestor):
    name = "records"
    suffixes = (".rr.yaml", ".rr.yml", ".rr.json")

    def ingest(self, path: str) -> Iterable[TestCase]:
        with open(path, encoding="utf-8") as fh:
            data: Any = json.load(fh) if path.endswith(".json") else yaml.safe_load(fh)
        items = (data or {}).get("evidence", []) if isinstance(data, dict) else data or []
        out = []
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            raw_status = item.get("status")
            status = str(raw_status or "").lower()
            message = str(item.get("message", ""))
            if status not in STATUS_ORDER:
                # A record without an explicit outcome (e.g. a planned but
                # unsigned inspection) must not count as passed.
                message = f"record has no valid status (got {raw_status!r}); expected passed/failed/skipped/error"
                status = "error"
            case = TestCase(
                name=str(item.get("name", f"record-{i + 1}")),
                classname=str(item.get("classname", "")),
                status=status,
                message=message,
                duration=float(item.get("duration", 0) or 0),
                source=path,
                target=str(item.get("target", "")),
            )
            reqs = item.get("requirements", [])
            if isinstance(reqs, str):
                reqs = [reqs]
            props = [("requirement", str(r)) for r in reqs]
            if item.get("level"):
                props.append(("level", str(item["level"])))
            props += [(f"artifact.{k}", str(v)) for k, v in (item.get("artifact") or {}).items()]
            props += [(str(k), str(v)) for k, v in (item.get("properties") or {}).items()]
            out.append(apply_properties(case, props))
        return out
