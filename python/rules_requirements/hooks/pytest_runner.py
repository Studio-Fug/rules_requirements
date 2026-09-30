# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bazel ``py_test`` entry point that runs pytest with traceability on.

Bazel sets ``$XML_OUTPUT_FILE`` for every test and keeps whatever JUnit the
test writes there. This runner points pytest's ``--junitxml`` at it (xunit2,
so per-testcase properties survive) and loads the ``rr`` marker plugin. A
suite's ``main`` becomes::

    from rules_requirements.hooks.pytest_runner import main

    if __name__ == "__main__":
        raise SystemExit(main(__file__))

or use the ``rr_py_test`` macro, which generates exactly that.
"""

from __future__ import annotations

import os
import sys


def main_argv(argv: list[str]) -> int:
    """Run pytest with ``argv`` (paths and options) exactly; see :func:`main`."""
    sys.argv = [sys.argv[0], *argv]
    return main(os.getcwd())


def main(anchor: str, extra_args: list[str] | None = None) -> int:
    """Run pytest over the directory containing ``anchor`` (or argv paths)."""
    import pytest

    from rules_requirements.hooks import pytest_plugin

    here = os.path.dirname(os.path.abspath(anchor))
    argv = sys.argv[1:]
    # Option values ("-p no:x", "-k expr") are not paths; only existing files are.
    has_paths = any(not a.startswith("-") and os.path.exists(a.split("::")[0]) for a in argv)
    args = ([] if has_paths else [here]) + ["-p", "no:cacheprovider"]
    xml_out = os.environ.get("XML_OUTPUT_FILE")
    if xml_out:
        args += [f"--junitxml={xml_out}", "-o", "junit_family=xunit2"]
    args += list(extra_args or []) + argv
    # Safe even when pip's pytest11 entry point also loads the plugin: the entry
    # point is named after the module, so pytest skips it once registered here.
    return int(pytest.main(args, plugins=[pytest_plugin]))
