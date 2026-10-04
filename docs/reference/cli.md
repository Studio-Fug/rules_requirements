# Command line: `rr`

`rr` is installed by `pip install rules-requirements`, and available in Bazel as
`bazel run @rules_requirements//python:rr -- <command> ...` (or
`bazel run @rules_requirements -- <command> ...`). Under `bazel run`, relative
paths resolve against the directory Bazel was invoked from.

| Command | Purpose | Guide |
| ------- | ------- | ----- |
| `validate` | Check the model's shape, references and coverage rules. | {doc}`../guides/model` |
| `scan` (`check-annotations`) | Check that source annotations name defined ids; list them. | {doc}`../guides/annotations` |
| `report` (`aggregate`) | Join the model with evidence; write HTML/JSON/Markdown and the gap queue. | {doc}`../guides/outputs` |
| `graph` | Export the trace graph as DOT, Mermaid, SVG or JSON. | {doc}`../guides/outputs` |
| `ingest` | Print the test cases parsed from evidence files (debugging). | {doc}`../guides/evidence` |
| `cases` | List every test case key in the evidence, with status, declared ids and flags. | {doc}`../guides/evidence` |
| `migrate plan` | Write the attribution worksheet: evidence that counts toward two or more entities. | {doc}`../guides/migrating-to-per-case` |
| `migrate apply` | Rewrite multi-id Python test tags to the owners decided on a worksheet. | {doc}`../guides/migrating-to-per-case` |
| `wrap` | Run a test binary and convert its output to traceability JUnit. | {doc}`../guides/hooks` |
| `case` | Append one test case to a JUnit file (shell and ad-hoc harnesses). | {doc}`../guides/hooks` |

`rr --version` prints the installed version (`rr 0.2.0`).

Exit status: `0` on success; `1` when validation fails, `scan` finds undefined
ids, or a `report --fail-on` / `--pyramid-policy error` condition holds; `2`
when the model is invalid for `scan`, `report`, `graph` and `migrate`, or a
report format cannot be inferred (`migrate` still runs on a model whose only
errors are `shared-case` / `same-code-multiple-owners`: resolving them is its
job). `cases` and `migrate plan` exit `2` when the
`--evidence` paths hold no evidence at all. `migrate apply` exits `1`, and writes
nothing unless given `--partial`, when it refused a file or could not find a
decided case's test in the module it names, and `2` when the worksheet is
unreadable or an owner is not one of the ids its case counts toward. `wrap`
exits with the wrapped command's status; `case` exits `2` when it cannot record
the case (no output file, more than one id, a malformed id or `--artifact`).

The reference below is generated from the command's own argument parser.

```{eval-rst}
.. argparse::
   :module: rules_requirements.cli
   :func: build_parser
   :prog: rr
   :nodescription:
```
