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
| `attribution` | Print each case's one owner (or none), how it got it and its declared ids; `--check` gates on quarantines, missing cases and lock drift; `--suggest` prints selectors. | {doc}`../guides/outputs` |
| `sets lock` / `check` / `show` | Write the verification-set lock from the evidence, check the lock against it, or print one entity's set. | {doc}`../guides/model` |
| `check-report` | Re-prove from a JSON report alone that no test case is owned by two entities. | {doc}`../guides/outputs` |
| `migrate plan` | Write the attribution worksheet: evidence that counts toward two or more entities. | {doc}`../guides/migrating-to-per-case` |
| `migrate apply` | `--stage tags`: rewrite multi-id Python test tags to the owners decided on a worksheet; `--stage model`: write an explicit selector for every current owner into the model. | {doc}`../guides/migrating-to-per-case` |
| `migrate verify` | Check test evidence from after `migrate apply` against the worksheet (and a baseline). | {doc}`../guides/migrating-to-per-case` |
| `wrap` | Run a test binary and convert its output to traceability JUnit. | {doc}`../guides/hooks` |
| `case` | Append one test case to a JUnit file (shell and ad-hoc harnesses). | {doc}`../guides/hooks` |

`rr --version` prints the installed version (`rr 0.3.0`).

Exit status: `0` on success; `1` when validation fails, `scan` finds undefined
ids, or a `report --fail-on` / `--pyramid-policy error` condition holds, or
an attribution issue is an error (with `report --strict`, any attribution
warning); `2` when the model is invalid for `scan`, `report`, `graph`,
`attribution`, `sets` and `migrate`, or a report format cannot be inferred
(`migrate` still runs on a model whose only errors are `shared-case` /
`same-code-multiple-owners`: resolving them is its job; `sets` on one whose
only errors are about the lock); `3` when `report` finds a quarantined test
case (after writing the reports; `--on-attribution-error=warn` keeps the
status) and when `sets lock` refuses to lock one. `attribution --check` and
`sets check` exit `1` on a quarantine, a missing case, lock drift or an
error-level attribution issue; `check-report` exits `1` when the report breaks
the one-owner partition, its counts disagree, or it names a JSON key twice in
one object, `2` when it is no v2 report.
`migrate apply --stage model` exits `1`, writing nothing, when the owner table
would change, the evidence holds a quarantine, a worksheet decision disagrees
with the evidence, or the new claims fail `check_claims`. `cases`,
`attribution`, `sets` and `migrate plan` exit `2` when the
`--evidence` paths hold no evidence at all. `migrate apply` exits `1`, and writes
nothing unless given `--partial`, when it refused a file or could not find a
decided case's test in the module it names, and `2` when the worksheet is
unreadable or an owner is not one of the ids its case counts toward.
`migrate verify` exits `1` when a decided case does not declare exactly its
owner or has no result, or, with `--baseline`, an undecided case's ids
changed or a case disappeared (`--allow-missing` excuses a missing case only
when its target has no result file at all: a `test.xml` with no testcase
means the target ran), and `2` when the worksheet is
unreadable or invalid, the evidence or baseline holds none, or it names no
build target while the worksheet's cases belong to build targets. `wrap`
exits with the wrapped command's status; `case` exits `2` when it cannot record
the case (no output file, more than one id, a malformed id or `--artifact`).

`migrate apply` judges the code only an `if __name__ == "__main__":` block
runs like any other code unless given `--trust-main-guard`. That opt-in
exclusion is best-effort: only use it together with a definitive check (the
collection check, or `migrate verify` against fresh test evidence before
merging).

The reference below is generated from the command's own argument parser.

```{eval-rst}
.. argparse::
   :module: rules_requirements.cli
   :func: build_parser
   :prog: rr
   :nodescription:
```
