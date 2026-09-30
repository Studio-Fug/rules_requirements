# Python API

The package is importable as `rules_requirements` and uses only the Python
standard library. The top-level package re-exports the most used names:

```python
from rules_requirements import build_matrix, load_model, read_model, validate
from rules_requirements import ingest, report

model = load_model("requirements/")  # raises ValidationError on errors
evidence = ingest.collect(["bazel-testlogs/**/test.xml"])
matrix = build_matrix(model, evidence, current_build={"dut_git_sha": "abc123"})
with open("report.html", "w", encoding="utf-8") as fh:
    fh.write(report.render_html(matrix))
```

## Model

```{eval-rst}
.. automodule:: rules_requirements.model
   :members: Model, UserNeed, Requirement, Risk, Mitigation, TestMethod, Entity, Note, VerifiedBy, Location, load_model, read_model, parse_documents, model_files

.. automodule:: rules_requirements.config
   :members: Config, Level, parse_config

.. automodule:: rules_requirements.validate
   :members: validate, Issue, ValidationError
```

## Evidence

```{eval-rst}
.. automodule:: rules_requirements.ingest
   :members: TestCase, Evidence, Ingestor, register, load_ingestor, ingestors, ingestor_for, collect, expand, apply_properties

.. automodule:: rules_requirements.ingest.junit
   :members: JUnitIngestor, target_from_path

.. automodule:: rules_requirements.ingest.libtest
   :members: LibtestIngestor, parse_libtest, merge_trace

.. automodule:: rules_requirements.ingest.records
   :members: RecordsIngestor
```

## Tracing and reports

```{eval-rst}
.. automodule:: rules_requirements.trace
   :members: build_matrix, Matrix, Verdict, EvidenceRef, Gap, classify, is_stale, route_for, find_gaps

.. automodule:: rules_requirements.annotations
   :members: Reference, extract, scan, candidate_files, unknown_references, is_test_path

.. automodule:: rules_requirements.report
   :members: to_dict, render_json, render_markdown, render_html

.. automodule:: rules_requirements.graph
   :members: Node, Edge, build, to_dot, to_mermaid, to_json, to_svg, layout
```

## Hooks

```{eval-rst}
.. automodule:: rules_requirements.rr
   :members: verifies, implements, unittest_main

.. automodule:: rules_requirements.hooks.junit_writer
   :members: JUnitWriter

.. automodule:: rules_requirements.hooks.pytest_plugin
   :members: trace_of

.. automodule:: rules_requirements.hooks.pytest_runner
   :members: main, main_argv

.. automodule:: rules_requirements.hooks.unittest
   :members: main, run, JUnitResult

.. automodule:: rules_requirements.hooks.wrap
   :members: main

.. automodule:: rules_requirements.bazel
   :members: run_generated_main, run_tests, golden
```
