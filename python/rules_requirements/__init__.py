# SPDX-License-Identifier: AGPL-3.0-or-later
"""rules_requirements — requirements-driven development and traceability.

Core API::

    from rules_requirements import load_model, ingest, build_matrix, report

    model = load_model("requirements/")
    evidence = ingest.collect(["bazel-testlogs"])
    matrix = build_matrix(model, evidence)
    open("report.html", "w").write(report.render_html(matrix))

Only the Python standard library is required (YAML parsing is vendored).
"""

from rules_requirements.model import (
    Mitigation,
    Model,
    Requirement,
    Risk,
    TestMethod,
    UserNeed,
    load_model,
    read_model,
)
from rules_requirements.trace import Matrix, build_matrix
from rules_requirements.validate import Issue, ValidationError, validate

__version__ = "0.2.0"

__all__ = [
    "Issue",
    "Matrix",
    "Mitigation",
    "Model",
    "Requirement",
    "Risk",
    "TestMethod",
    "UserNeed",
    "ValidationError",
    "build_matrix",
    "load_model",
    "read_model",
    "validate",
]
