#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Build the docs with Sphinx and fail on every docutils problem too.

``sphinx -W`` turns Sphinx's own warnings into errors, but a docutils system
message printed outside Sphinx's logger leaves its exit status 0. sphinx-argparse
parses each ``--help`` text as reStructuredText on its own, so a help string
such as ``(multi-verifies-*)`` prints

    :1: (WARNING/2) Inline emphasis start-string without end-string.

and the CLI reference then shows a ``problematic`` node. This wrapper runs
Sphinx with the given arguments (the last one is the output directory), echoes
its output, and exits 1 when any line is a docutils ``WARNING/``, ``ERROR/``
or ``SEVERE/`` message, or any built HTML page holds a ``problematic`` or
``system-message`` node; otherwise it exits with Sphinx's status::

    python tools/docs_build.py -W -n --keep-going -b html docs docs/_build/html
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

# docutils prints "<source>:<line>: (WARNING/2) <message>"; Sphinx's own
# warnings read "WARNING: ...", which -W already fails on.
DOCUTILS_MESSAGE = re.compile(r"\((?:WARNING|ERROR|SEVERE)/[0-9]\)")
BROKEN_NODE = re.compile(r'class="[^"]*\b(?:problematic|system-message)\b')


def docutils_messages(output: str) -> list[str]:
    """The lines of Sphinx's output that are docutils system messages."""
    return [line for line in output.splitlines() if DOCUTILS_MESSAGE.search(line)]


def broken_pages(outdir: str) -> list[str]:
    """The built HTML pages that render a docutils problem node."""
    found = []
    for base, _, files in os.walk(outdir):
        for name in sorted(files):
            if not name.endswith(".html"):
                continue
            path = os.path.join(base, name)
            with open(path, encoding="utf-8", errors="replace") as fh:
                if BROKEN_NODE.search(fh.read()):
                    found.append(os.path.relpath(path, outdir))
    return sorted(found)


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    proc = subprocess.run(
        [sys.executable, "-m", "sphinx", *argv],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    sys.stdout.write(proc.stdout)
    messages = docutils_messages(proc.stdout)
    pages = broken_pages(argv[-1]) if os.path.isdir(argv[-1]) else []
    for line in messages:
        print(f"docs_build: docutils message outside Sphinx's logger: {line}", file=sys.stderr)
    for page in pages:
        print(f"docs_build: {page} renders a problematic or system-message node", file=sys.stderr)
    if proc.returncode:
        return proc.returncode
    return 1 if messages or pages else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
