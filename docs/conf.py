# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sphinx configuration for the rules_requirements documentation."""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "python"))

import rules_requirements  # noqa: E402
from rules_requirements import graph  # noqa: E402
from rules_requirements.model import parse_documents, read_model  # noqa: E402

project = "rules_requirements"
author = "Studio Fug"
copyright = "2026, Studio Fug. AGPL-3.0-or-later"
release = rules_requirements.__version__
version = release

extensions = [
    "myst_parser",
    "sphinx.ext.autodoc",
    "sphinx.ext.intersphinx",
    "sphinx.ext.viewcode",
    "sphinx.ext.githubpages",  # writes .nojekyll: GitHub Pages must not strip _static/
    "sphinx_copybutton",
    "sphinxarg.ext",
]

source_suffix = {".md": "markdown", ".rst": "restructuredtext"}
exclude_patterns = ["_build", "_generated", "_static", "Thumbs.db", ".DS_Store", "requirements.txt"]

myst_enable_extensions = ["colon_fence", "deflist", "fieldlist", "attrs_inline"]
myst_heading_anchors = 3

html_theme = "furo"
html_title = f"rules_requirements {release}"
html_favicon = "_static/logo/rules_requirements_logo.svg"
html_static_path = ["_static"]
html_css_files = ["custom.css"]
html_extra_path = ["_generated/extra"]
html_theme_options = {
    # The logo's black and white are swapped for dark backgrounds, where the
    # original's black edges would disappear.
    "light_logo": "logo/rules_requirements_logo.svg",
    "dark_logo": "logo/rules_requirements_logo-dark.svg",
    "source_repository": "https://github.com/Studio-Fug/rules_requirements/",
    "source_branch": "main",
    "source_directory": "docs/",
}

intersphinx_mapping = {"python": ("https://docs.python.org/3", None)}
autodoc_member_order = "bysource"
autodoc_default_options = {"members": True, "show-inheritance": True}
autodoc_typehints = "signature"
nitpicky = True
nitpick_ignore_regex = [
    # Documented in the Python docs under their public aliases (unittest.TestCase ...).
    (r"py:class", r"unittest\..*"),
    # Type variables and private helpers that appear in signatures.
    (r"py:class", r"rules_requirements\.rr\.F"),
    (r"py:class", r".*\._Case"),
    # Unqualified cross-module references in docstrings.
    (r"py:class", r"(ValidationError|Model|JUnitWriter)"),
    (r"py:meth", r"ingest"),
]
copybutton_prompt_text = r"\$ "
copybutton_prompt_is_regexp = True


# --------------------------------------------------------------------------- #
# Generated assets: trace graphs drawn by rules_requirements itself, and the  #
# JSON schema published at the URL its $id names.                             #
# --------------------------------------------------------------------------- #

_CONCEPT = {
    "user_needs": [{"id": "UN-1", "title": "What a user must be able to do"}],
    "requirements": [
        {"id": "REQ-1", "title": "What the product must do", "satisfies": ["UN-1"]},
        {"id": "REQ-2", "title": "A refinement of REQ-1", "refines": ["REQ-1"]},
        {"id": "REQ-3", "title": "A risk control, as a requirement"},
    ],
    "mitigations": [
        {"id": "MIT-1", "title": "Risk control measure", "mitigates": ["RISK-1"], "implemented_by": ["REQ-3"]}
    ],
    "risks": [{"id": "RISK-1", "title": "Hazard -> harm"}],
}


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _generate() -> None:
    out = os.path.join(HERE, "_generated")
    # Concept picture (no statuses: neutral colours).
    concept, _ = parse_documents([("concept.yaml", _CONCEPT)])
    nodes, edges = graph.build(concept)
    _write(os.path.join(out, "concept-graph.svg"), graph.to_svg(nodes, edges, link_prefix="#"))

    # The thermostat example, coloured by the statuses in its golden report.
    example = os.path.join(ROOT, "examples", "thermostat")
    model, _ = read_model(os.path.join(example, "requirements"))
    with open(os.path.join(example, "report.golden.json"), encoding="utf-8") as fh:
        golden = json.load(fh)
    statuses = {
        item["id"]: item["status"]
        for section in ("user_needs", "requirements", "mitigations", "risks", "test_methods")
        for item in golden.get(section, [])
    }
    nodes, edges = graph.build(model, statuses)
    _write(os.path.join(out, "thermostat-graph.svg"), graph.to_svg(nodes, edges, link_prefix="#"))

    # Serve the model schema at its $id.
    schema_dir = os.path.join(out, "extra", "schema")
    os.makedirs(schema_dir, exist_ok=True)
    shutil.copy(os.path.join(ROOT, "schema", "rules_requirements.schema.json"), schema_dir)


# At import time: html_extra_path is validated before any build event fires.
_generate()


def _dataclass_signature(app, what, name, obj, options, signature, return_annotation):
    """Render dataclass signatures from ``__init__``.

    Sphinx 9.1 leaves the (``from __future__ import annotations``) string
    annotations of a dataclass's class signature unevaluated, while the same
    ``__init__`` documented as a function resolves them; use the latter.
    """
    if what != "class" or not dataclasses.is_dataclass(obj):
        return None
    from sphinx.util.inspect import signature as signature_of
    from sphinx.util.inspect import stringify_signature

    sig = signature_of(obj.__init__, bound_method=True)
    # `<factory>` is not Python, and an unparsable signature is split naively.
    params = [
        p.replace(default=_FACTORY) if p.default is dataclasses._HAS_DEFAULT_FACTORY else p  # type: ignore[attr-defined]
        for p in sig.parameters.values()
    ]
    sig = sig.replace(parameters=params)
    return stringify_signature(sig, show_return_annotation=False), None


class _Factory:
    def __repr__(self) -> str:
        return "field(default_factory=...)"


_FACTORY = _Factory()


def setup(app):
    app.connect("autodoc-process-signature", _dataclass_signature)
