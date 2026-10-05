# rules_requirements

**Requirements-driven development for Bazel projects.** Write down what users
need, what the product must do, what can go wrong and how each risk is
controlled — then let the test suites prove it, and get a traceability report
that says, for every item, whether it is *verified the way it needs to be*.

```{raw} html
<div class="rr-graph-wrap">
```

```{raw} html
:file: _generated/concept-graph.svg
```

```{raw} html
</div>
```

User needs are **satisfied by** requirements; requirements may **refine**
other requirements; risks are **mitigated by** risk control measures, which are
**implemented by** requirements. Each requirement claims the test cases that
verify it, and every verdict rolls up from there: a user need is *validated*
when the requirements satisfying it are verified, a risk is *mitigated* when
the requirements implementing its controls are verified.

**A test case verifies at most one requirement**; a set of test cases may
together verify one. The tool is built so that one test case *cannot* count
toward two requirements: ownership is decided in one place, ambiguity
quarantines the case, and a published report can be re-checked from its JSON
alone ({doc}`one-test-case-one-requirement`).

The vocabulary follows the design-control and risk-management structure of
IEC 62304, ISO 14971 and IEC 60601-1 (see {doc}`standards`), kept generic
enough for any product that wants an auditable verification and validation
argument.

## What you get

- **A model you can review in a diff** — YAML files for user needs,
  requirements, risks, mitigations and test methods, one file or one object per
  file, validated with stable rule codes ({doc}`guides/model`).
- **Test hooks that emit standard JUnit XML** with traceability properties, for
  pytest, unittest, googletest, plain-assert C++, Rust and node:test, plus a
  writer for hand-rolled hardware harnesses ({doc}`guides/hooks`).
- **Pluggable evidence ingestion** — JUnit is the standard; Rust libtest output
  and signed inspection records are built in; other formats are a small class
  away ({doc}`guides/evidence`).
- **One owner per test case** — `verified_by` claims individual cases by
  selector, two requirements claiming one case is a model error with an
  example case, a case whose evidence names two ids counts for nobody, and a
  generated lock pins every requirement's set of cases
  ({doc}`one-test-case-one-requirement`).
- **Verification rigor, not just coverage** — requirements demand a level
  (`analysis < simulation < sil < hil < hitl`, or `inspection`), evidence
  provides one, and the report tells *under-verified* and *stale* apart from
  verified ({doc}`concepts`).
- **Reports** in HTML, Markdown and deterministic JSON, with a trace graph and
  a typed gap queue that routes each gap to an agent or to a human gate
  ({doc}`guides/outputs`).
- **Source annotations** — `# @rr(REQ-0001): Implements isolated access to
  secure data` — tie requirements to the code that implements them
  ({doc}`guides/annotations`).
- **Hermetic reports in Bazel** — tests run inside a build action, the report is
  a build output, a golden test pins it ({doc}`guides/bazel`).
- **No runtime dependencies** — the toolkit is pure Python standard library;
  YAML parsing is vendored.

## Where to start

- New to the tool: {doc}`getting-started`, then the {doc}`tutorial`.
- Bringing it into an existing monorepo: {doc}`guides/integration`.
- Looking for a flag or field: {doc}`reference/cli`, {doc}`guides/model`,
  {doc}`reference/api`.

```{toctree}
:caption: Start here
:maxdepth: 2
:hidden:

getting-started
concepts
one-test-case-one-requirement
tutorial
standards
```

```{toctree}
:caption: Guides
:maxdepth: 2
:hidden:

guides/model
guides/annotations
guides/hooks
guides/evidence
guides/outputs
guides/bazel
guides/integration
guides/web-editor
guides/migrating-to-per-case
```

```{toctree}
:caption: Reference
:maxdepth: 2
:hidden:

reference/cli
reference/api
```

```{toctree}
:caption: Project
:maxdepth: 1
:hidden:

development
release-notes
```
