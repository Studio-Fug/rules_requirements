# SPDX-License-Identifier: AGPL-3.0-or-later
"""The docs build fails on docutils problems that ``sphinx -W`` misses.

tools/docs_build.py (the Docs workflow's build step) fails on any docutils
``WARNING/`` line and any rendered ``problematic`` node; and every ``rr``
help text sphinx-argparse renders must parse as reStructuredText cleanly.
"""

import argparse
import importlib.util
import os

import pytest

from rules_requirements import cli

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOTS = (os.path.dirname(os.path.dirname(_HERE)), os.path.join(os.environ.get("TEST_SRCDIR", ""), "_main"))


def _tool():
    for root in _ROOTS:
        path = os.path.join(root, "tools", "docs_build.py")
        if os.path.exists(path):
            spec = importlib.util.spec_from_file_location("docs_build", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    raise AssertionError("tools/docs_build.py not found (under Bazel it must be in the test's data)")


def test_a_docutils_warning_line_fails_the_build():
    tool = _tool()
    out = (
        "reading sources... [ 50%] reference/cli\n"
        ":1: (WARNING/2) Inline emphasis start-string without end-string.\n"
        "<string>:3: (ERROR/3) Unexpected indentation.\n"
        "build succeeded.\n"
    )
    assert tool.docutils_messages(out) == out.splitlines()[1:3]
    # Sphinx's own warnings are -W's business, and a clean log is clean.
    assert tool.docutils_messages("WARNING: undefined label: 'x'\nbuild succeeded.\n") == []


def test_a_page_with_a_problematic_node_fails_the_build(tmp_path):
    tool = _tool()
    (tmp_path / "reference").mkdir()
    (tmp_path / "index.html").write_text('<p class="first">fine</p>')
    (tmp_path / "reference" / "cli.html").write_text(
        '<p>treat annotation warnings (multi-verifies-<a href="#id1"><span class="problematic">*</span></a>)</p>'
    )
    assert tool.broken_pages(str(tmp_path)) == [os.path.join("reference", "cli.html")]


def _help_texts(parser, path="rr"):
    for action in parser._actions:
        if action.help and action.help is not argparse.SUPPRESS:
            yield f"{path} {'/'.join(action.option_strings) or action.dest}", action.help
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                yield from _help_texts(sub, f"{path} {name}")


def test_every_cli_help_text_is_clean_restructuredtext():
    """sphinx-argparse renders each help text as RST: an unescaped ``*``
    (``multi-verifies-*``) became a problematic node in the CLI reference."""
    docutils_core = pytest.importorskip("docutils.core")
    from docutils import nodes

    texts = list(_help_texts(cli.build_parser()))
    assert any("multi-verifies-annotation" in text for _, text in texts)
    bad = []
    for where, text in texts:
        tree = docutils_core.publish_doctree(
            text, settings_overrides={"report_level": 5, "halt_level": 5, "warning_stream": False}
        )
        problems = [m.astext() for m in tree.findall(nodes.system_message)]
        problems += [p.astext() for p in tree.findall(nodes.problematic)]
        if problems:
            bad.append(f"{where}: {text!r}: {problems}")
    assert not bad, "\n".join(bad)
