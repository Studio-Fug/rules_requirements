# SPDX-License-Identifier: AGPL-3.0-or-later
"""Print the test cases ingested from an rr_evidence tree, one per line.

``dump_cases.py OUT EVIDENCE_DIR...`` — the golden of the rr_node_test
fixtures: each case's key (``<target>#<classname>::<name>``), its status, the
first line of its message and its traceability properties. Durations and
source paths vary between runs and are left out.
"""

import os
import sys

from rules_requirements.ingest.junit import JUnitIngestor


def main(out: str, roots: list[str]) -> int:
    lines = []
    for root in roots:
        for dirpath, _dirs, files in sorted(os.walk(root)):
            for name in sorted(files):
                if not name.endswith(".xml"):
                    continue
                for case in JUnitIngestor().ingest(os.path.join(dirpath, name)):
                    path = f"{case.classname}::{case.name}" if case.classname else case.name
                    props = [f"requirement={r}" for r in case.requirements]
                    props += [f"level={case.level}"] if case.level else []
                    props += [f"artifact.{k}={v}" for k, v in sorted(case.artifact.items())]
                    props += [f"{k}={v}" for k, v in sorted(case.properties.items())]
                    message = case.message.splitlines()[0] if case.message else ""
                    lines.append(f"{case.target}#{path}  [{case.status}]  {' '.join(props)}  {message}".rstrip())
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1], sys.argv[2:]))
