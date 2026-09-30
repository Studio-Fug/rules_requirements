#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Presubmit: every first-party source file carries an SPDX license header."""

import sys

TAG = "SPDX-License-Identifier: AGPL-3.0-or-later"


def main(paths: list[str]) -> int:
    missing = []
    for path in paths:
        if "/_vendor/" in path:
            continue
        with open(path, encoding="utf-8", errors="replace") as fh:
            head = "".join(fh.readline() for _ in range(5))
        if TAG not in head:
            missing.append(path)
    for path in missing:
        print(f"{path}: missing '{TAG}' in the first lines", file=sys.stderr)
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
