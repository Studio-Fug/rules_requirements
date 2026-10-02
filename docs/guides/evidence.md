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
| `suite` | The enclosing JUnit `<testsuite>` name, when there is one. |
| `source` | The file it was read from. |
| `properties` | Any other properties, verbatim. |

{py:func}`rules_requirements.ingest.collect` reads a list of files,
directories (walked recursively, following symlinks) and globs (`**` allowed).
Each file goes to the first registered ingestor that recognises it; files no
ingestor recognises — the `test.log` next to each `test.xml` in
`bazel-testlogs`, for example — are skipped. The result also rolls cases up per
target for [`verified_by`](../concepts.md#evidence) traces, the most severe
status winning: `error` > `failed` > `skipped` > `passed`. A target with any
skipped case is therefore not passing whole-target evidence — the skipped part
may be exactly the hardware its `verified_by` level claims (an absent DUT). A
binary that exits non-zero although every case in its report passed (a
sanitizer, a crash after writing the report) gets an extra `exit-status` error
case carrying every requirement id the run traced, so those requirements read
FAILED — from `rr_evidence` and from the libtest wrapper.

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

**Targets without a report.** When a test writes no JUnit of its own (a plain
script, a `js_test`), Bazel's `generate-xml.sh` writes one: a `<testsuite>`
holding one `<testcase>` with the suite's name, `status="run"` and a
`<system-out>` that starts with "Generated test.log". The ingestor recognises
that fingerprint and marks the case `rr.synthetic=true` (a property our own
writers also set when they have nothing per-case to report): it says how the
whole target ended, not that any particular test passed.

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

(case-keys)=

## Case keys

Each test case has a stable identity, its *case key*
({py:class}`~rules_requirements.case_keys.CaseKey`): the target that ran it and
its path within that target, written `<target>#<path>`:

```text
//pi/server:server_test#pi.server.tests.test_proto_wire::test_client_roundtrip[configure]
//web:clocksync_test#clocksync::bestSample keeps the min-RTT sample
//web:flashEnv_test#[target]
```

- **Target**: the label recovered from the `bazel-testlogs` path, or the
  wrapper's `--target`, or a record's `target:`. Evidence that cannot be pinned
  to a label gets a pseudo-target: `record:<file stem>` for records without
  `target:`, `suite:<testsuite name>` for JUnit outside a `bazel-testlogs`
  tree.
- **Path**: `<classname>::<name>` (just `<name>` without a classname), Unicode
  NFC, with surrounding whitespace stripped. An `[rr:ID]` tag inside a name is
  removed, so re-tagging a test never renames its case. A target that only
  produced Bazel's generated report has one case, `[target]`.
- **Not identity**: retries (`test_attempts/attempt_N.xml`), repetitions
  (`run_k_of_n`), shards (`shard_i_of_n`) and a second evidence root. They are
  folded into one result per key: the final attempt decides, with an earlier
  failure under a final pass marked *flaky*; across runs and roots the worst
  status wins; one key in two shards (or twice in one report) is marked
  *duplicate*.

`rr cases` prints every key in a set of evidence, with its status, the ids its
evidence declares, flags (`synthetic`, `target_scope`, `flaky`, `duplicate`)
and the test source when known (the `rr.file` property). It needs no model:

```console
$ rr cases --evidence bazel-testlogs --target //pi/server:server_test
//pi/server:server_test#pi.server.tests.test_handler::test_configure_renegotiates_mid_capture	passed	PR-11,PR-13	-	-
...
$ rr cases --evidence bazel-testlogs --json > cases.json
```

Copy keys from here rather than guessing them. The keys do not change any
verdict.

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
