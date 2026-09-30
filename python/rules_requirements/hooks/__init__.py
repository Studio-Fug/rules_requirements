# SPDX-License-Identifier: AGPL-3.0-or-later
"""Test-framework hooks that emit traceability-tagged JUnit XML.

* :mod:`~rules_requirements.hooks.pytest_plugin` — ``@pytest.mark.rr(...)``.
* :mod:`~rules_requirements.hooks.pytest_runner` — Bazel ``py_test`` entry point.
* :mod:`~rules_requirements.hooks.unittest` — ``@rr.verifies(...)`` + JUnit runner.
* :mod:`~rules_requirements.hooks.junit_writer` — for hand-rolled harnesses.
* :mod:`~rules_requirements.hooks.wrap` — wrap a test binary (Rust libtest)
  and convert its output to JUnit.

googletest and Rust have native hooks outside Python: ``RR_VERIFIES(...)``
in ``@rules_requirements//cc:gtest`` and ``rr::verifies!(...)`` in
``@rules_requirements//rust:rr``.
"""
