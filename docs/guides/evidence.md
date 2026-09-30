# Evidence and ingestors

**Evidence** is everything that says a test ran and how it went: JUnit XML from
test suites, output captured from a test binary, a signed record of a manual
inspection. **Ingestors** turn each kind of file into the same records, so the
rest of the system never needs to know where evidence came from.

## Test cases

Every ingestor produces {py:class}`~rules_requirements.ingest.TestCase`
records:

| Field | Meaning |
| ----- | ------- |
| `name`, `classname` | Identify the case; reports show `classname::name`. |
| `status` | `passed`, `failed`, `error` or `skipped`. |
| `requirements` | Ids the case verifies. |
| `level` | Level the case provides (empty: the model's `default_provided_level`). |
| `artifact` | Identity of the artifact exercised, for [staleness](../concepts.md#staleness). |
| `message` | Failure or skip message. |
| `duration` | Seconds. |
| `target` | The build label the evidence belongs to, when known. |
| `source` | The file it was read from. |
| `properties` | Any other properties, verbatim. |

{py:func}`rules_requirements.ingest.collect` reads a list of files,
directories (walked recursively, following symlinks) and globs (`**` allowed).
Each file goes to the first registered ingestor that recognises it; files no
ingestor recognises — the `test.log` next to each `test.xml` in
`bazel-testlogs`, for example — are skipped. The result also rolls cases up per
target for [`verified_by`](../concepts.md#evidence) traces: `error` if any case
errored, else `failed` if any failed, else `passed` if any passed, else
`skipped`. A binary that exits non-zero although every case in its report
passed (a sanitizer, a crash after writing the report) gets an extra
`exit-status` error case — from `rr_evidence` and from the libtest wrapper.

## Built-in ingestors

| Name | Reads | Recognised by |
| ---- | ----- | ------------- |
| `junit` | JUnit XML | a `.xml` file whose first 4 KiB contain `<testsuite` or `<testcase` |
| `records` | evidence records | `*.rr.yaml`, `*.rr.yml`, `*.rr.json` |
| `libtest` | Rust libtest output | `*.libtest.txt`, `*.libtest` |

### JUnit

The standard. The ingestor accepts a `<testsuites>` or bare `<testsuite>` root,
nested suites, per-case `<properties>`, trace attributes on `<testcase>` and
suite-level properties inherited by the cases below them (see
{ref}`junit-properties`). A case's status is `error` if it has an `<error>`
child, `failed` for `<failure>`, `skipped` for `<skipped>` (or googletest's
`status="notrun"` / `result="skipped"`/`"suppressed"`), and `passed`
otherwise; the message comes from the element's `message` attribute or text. A
file that is not well-formed XML contributes nothing.

**Bazel test logs.** Bazel writes one `test.xml` per test target, and the
target's label is recovered from the path:

| Path | Label |
| ---- | ----- |
| `bazel-testlogs/pkg/sub/name/test.xml` | `//pkg/sub:name` |
| `bazel-out/k8-fastbuild/testlogs/pkg/name/test.xml` (what `readlink -f bazel-testlogs` gives) | `//pkg:name` |
| `bazel-testlogs/pkg/name/shard_2_of_4/test.xml`, `.../run_1_of_3/test.xml` | `//pkg:name` |
| `bazel-testlogs/external/repo+/pkg/name/test.xml` | `@repo+//pkg:name` |

That label is what `verified_by` entries match.

```{note}
For tests that Bazel retried (`--flaky_test_attempts`, `flaky = True`), the logs
of the earlier attempts are kept under `<name>/test_attempts/attempt_N.xml` and
map to the same label. Walking the whole `bazel-testlogs` directory therefore
counts a failed earlier attempt as a failure — deliberately: a test that only
passes on retry is not dependable evidence. To judge only the final attempt,
pass a glob that selects the final results:
`--evidence "$(bazel info bazel-testlogs)/**/test.xml"`.
```

An unreadable report (malformed XML) is not skipped: it becomes one `error`
case for its target, so a crashed or corrupted run shows up as a failure rather
than vanishing.

### Records

For evidence no test runner produces — an inspection, an analysis, a bench
measurement recorded by hand — write a records file:

```yaml
# evidence/panel_inspection.rr.yaml
evidence:
  - name: panel-shows-setpoint-unit
    classname: inspection.TM-2
    status: passed                 # required: passed | failed | skipped | error
    requirements: [REQ-7]          # a list or a single id
    level: inspection
    artifact: {board_rev: C}       # optional identity, for staleness
    properties:                    # anything else worth keeping
      inspector: QA reviewer
      date: "2026-09-30"
      record: "Photo QA-114 shows '21.5 °C' after entering 21.5C."
```

Entries may also give `message`, `duration` and `target`; a missing `name`
becomes `record-<n>`. A missing or unknown `status` becomes `error`: a planned
but unsigned record must never count as passed. The top level may
be `{evidence: [...]}` or a bare list; JSON works the same way. Records are
evidence like any other: in Bazel, list the file in `rr_report(evidence = ...)`.

### libtest

Reads the default ("pretty") output of a Rust test binary — the
`test path::name ... ok | FAILED | ignored` lines, plus each failure's captured
output as its message — and merges traces from a sidecar
`<stem>.rrtrace.jsonl` file next to it (the format `rr::verifies!` writes). It
is mainly used inside `rr wrap` ({doc}`hooks`); the ingestor itself handles
saved captures named `*.libtest.txt`.

## Writing an ingestor

Subclass {py:class}`~rules_requirements.ingest.Ingestor`: give it a `name`,
decide which files it understands, and yield test cases.

```python
# my_project/tap.py
from rules_requirements.ingest import Ingestor, TestCase, apply_properties


class TapIngestor(Ingestor):
    """Test Anything Protocol, with ids in a `# rr: REQ-1 REQ-2` directive."""

    name = "tap"
    suffixes = (".tap",)

    def sniff(self, path, head):  # head: the file's first 4 KiB
        return path.endswith(self.suffixes) and head.lstrip().startswith(b"TAP version")

    def ingest(self, path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if not line.startswith(("ok", "not ok")):
                    continue
                case = TestCase(
                    name=line.split("-", 1)[-1].split("#")[0].strip(),
                    status="passed" if line.startswith("ok") else "failed",
                    source=path,
                )
                ids = line.split("# rr:", 1)[1].split() if "# rr:" in line else []
                yield apply_properties(case, [("requirement", i) for i in ids])
```

The default `sniff` accepts any path ending in one of `suffixes`.
{py:func}`~rules_requirements.ingest.apply_properties` folds raw
`(name, value)` properties into the typed fields using the
{ref}`JUnit conventions <junit-properties>`, so every ingestor treats
`requirement`, `level` and `artifact.*` the same way.

Make it available in any of three ways:

- **In code:** `rules_requirements.ingest.register(TapIngestor())`.
- **From the command line:** `rr report --ingestor my_project.tap:TapIngestor ...`
  (`module:attribute`, naming a class or an instance; the attribute defaults to
  `INGESTOR`).
- **As a plugin:** publish it under the `rules_requirements.ingestors` entry
  point group, and it is loaded automatically wherever your package is
  installed:

  ```toml
  [project.entry-points."rules_requirements.ingestors"]
  tap = "my_project.tap:TapIngestor"
  ```

Ingestors are tried in registration order — the built-ins first — and the first
whose `sniff` accepts a file wins; registering under an existing name replaces
that ingestor. `rr report --format NAME` (repeatable) restricts a run to the
named ingestors, and `rr ingest PATH...` prints what the ingestors make of a set
of files, which is the quickest way to debug a new one.
