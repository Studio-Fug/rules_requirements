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
| `declared` | The requirement ids the evidence *names* for the case — tags, in order, without duplicates. Never an owner: which requirement a case verifies is decided only by attribution. However they are given (constructor, assignment, the alias), values are split on commas and whitespace, so `["PR-1, PR-2"]` is two ids. (`requirements` is a deprecated read/write alias, also accepted by the constructor, with a `DeprecationWarning`.) |
| `level` | Level the case provides (empty: the model's `default_provided_level`). |
| `artifact` | Identity of the artifact exercised, for [staleness](../concepts.md#staleness). |
| `message` | Failure or skip message. |
| `duration` | Seconds. |
| `target` | The build label the evidence belongs to, when known. |
| `suite` | The enclosing JUnit `<testsuite>` name, when there is one. |
| `source` | The file it was read from. |
| `properties` | Any other properties, verbatim — among them `rr.file`, the source file of the test code, which `JUnitWriter`, `CheckPlan` and `rr case --file` write ({ref}`junit-properties`). |
| `file`, `line` | The test source, workspace-relative (`rr.file`, else the testcase's `file` attribute, with any `*.runfiles/<workspace>/` or `bazel-out/<cfg>/bin/` prefix removed), and its `line` attribute (0 if unknown). |
| `suite_declared` | Ids an enclosing suite (or parent case) named. They are *not* the case's: see below. |

`TestCase.scope` is `target` for a result about the whole target run
(`rr.scope=target`) and `case` otherwise; `TestCase.synthetic` is true for a
target's single whole-run result (`rr.synthetic=true`, or Bazel's generated
report).

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
case — from `rr_evidence` and from `rr wrap`, for libtest output and for a
runner's own JUnit (`--format junit`) alike. It declares no requirement (0.2
copied every id the run traced onto it); it is target-scope, so it taints the
cases claimed on its target instead.

**A test case verifies at most one requirement.** Ingest only records what the
evidence declares; it never decides ownership. Every `requirement` /
`requirements` value — a property, a testcase attribute, a record, a Rust
trace line, a node diagnostic — is split on commas and whitespace, and an
`[rr:ID]` tag in a case's name is one more declared id. A case that ends up
declaring more than one distinct id keeps all of them: attribution then
quarantines it (`multi-tag`), so it counts for no requirement and every id it
names reads INVALID. The hooks that can write such evidence warn
({ref}`multi-id-deprecation`), and 0.4 rejects it.

<!-- rr:interim multi-tag quarantine pending -->
```{warning}
Interim development tree: ingest already records every declared id, but
attribution and the verdicts that read only attribution are not in this tree
yet, so the verdict engine here still counts a case once for each id it
declares. Do not release or deploy it on its own; 0.3.0 ships all of them
together.
```

## Built-in ingestors

| Name | Reads | Recognised by |
| ---- | ----- | ------------- |
| `junit` | JUnit XML | a `.xml` file whose first 4 KiB contain `<testsuite` or `<testcase`; and always Bazel's own `test.xml` / `test_attempts/attempt_N.xml` in a testlogs tree |
| `records` | evidence records | `*.rr.yaml`, `*.rr.yml`, `*.rr.json` |
| `libtest` | Rust libtest output | `*.libtest.txt`, `*.libtest` |

### JUnit

The standard. The ingestor accepts a `<testsuites>` or bare `<testsuite>` root,
nested suites, per-case `<properties>` and trace attributes on `<testcase>`
(see {ref}`junit-properties`). Of the properties of a `<testsuite>` or
`<testsuites>`, only `level` and `artifact.*` reach the cases below. A
suite-level `requirement` (googletest writes one for `RR_VERIFIES` in
`SetUpTestSuite`, an `Environment` or `main`) is no longer inherited by the
cases: it is kept as `suite_declared` and reported once per suite as a
`suite-level-requirement` warning (`Evidence.issues`, printed by `rr ingest`,
`rr cases` and the other commands reading `--evidence`).

A `<testcase>` holding `<testcase>` children (subtests, as some runners nest
them) is a scope, not a case: each child becomes a case whose classname is the
parent's path joined with ` > ` — `pkg > TestParse::empty`, the same shape
`rr_node_test` gives node:test subtests — whatever `classname` the child
itself carries. Subtests wrapped in a `<testsuite>` inside the `<testcase>`
are read the same way. The parent's `level`, `artifact.*` and source reach its
children; its requirement ids do not (nor does a `requirement` attribute on a
`<testsuite>`: it is suite-level too). A failure of the
parent itself that none of its children explains (a setup hook) becomes a
target-scope `<hooks>` error. A case's status is `error` if it has an `<error>`
child, `failed` for `<failure>`, `skipped` for `<skipped>` (or googletest's
`status="notrun"` / `result="skipped"`/`"suppressed"`), and `passed`
otherwise; the message comes from the element's `message` attribute or text.

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
writers also set when they have nothing per-case to report: `rr_evidence` for a
test that wrote no JUnit, `rr wrap` for a run that left no case): it says how
the whole target ended, not that any particular test passed. Such a result is
the target's one case, `[target]`, whichever of them wrote it. The `exit-status`
error case that `rr_evidence`, `rr wrap` and `rr_node_test` add when a binary
fails although no case in its report did, and `rr wrap`'s error for a missing
or non-JUnit report, carry `rr.scope=target` instead: they are about the run,
not a test case of it.

An unreadable report (malformed XML) is not skipped: it becomes one `error`
result for its target, `<unreadable>`, with `rr.scope=target` — so a crashed or
corrupted run taints everything claimed on that target rather than vanishing
or reading as one more test case. Bazel's own report files (`test.xml`, and
`test_attempts/attempt_N.xml`, in a testlogs tree) are always read as JUnit,
whatever their first bytes: an empty, binary or non-JUnit one is
`<unreadable>` too, and one whose root element comes after a long prolog is
read in full.

### Records

For evidence no test runner produces — an inspection, an analysis, a bench
measurement recorded by hand — write a records file:

```yaml
# evidence/panel_inspection.rr.yaml
target: record:panel_inspection    # optional; entries may override it
evidence:
  - name: panel-shows-setpoint-unit
    classname: inspection.TM-2
    status: passed                 # required: passed | failed | skipped | error
    requirement: REQ-7             # the one id this record verifies (a tag)
    level: inspection
    artifact: {board_rev: C}       # optional identity, for staleness
    properties:                    # anything else worth keeping
      inspector: QA reviewer
      date: "2026-09-30"
      record: "Photo QA-114 shows '21.5 °C' after entering 21.5C."
```

Entries may also give `message`, `duration` and their own `target`; without
any `target:`, a record's case key uses the pseudo-target `record:<file stem>`.
The legacy `requirements: [REQ-7]` list is still read; a record naming more
than one id keeps them all, which makes it a `multi-tag` case. A missing
`name` becomes `record-<n>`. A missing or unknown `status` becomes `error`: a planned
but unsigned record must never count as passed. The top level may
be `{evidence: [...]}` or a bare list; JSON works the same way. Records are
evidence like any other: in Bazel, list the file in `rr_report(evidence = ...)`.

### libtest

Reads the default ("pretty") output of a Rust test binary — the
`test path::name ... ok | FAILED | ignored` lines, plus each failure's captured
output as its message — and merges traces from a sidecar
`<stem>.rrtrace.jsonl` file next to it (the format `rr::verifies!` writes:
one JSON line per call, with a single `requirement` or the 0.2 `requirements`
list; every id becomes a declared id of the test). It
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

- **Target**: the label recovered from the `bazel-testlogs` path (or from an
  `rr_evidence` output tree), or a record's `target:`. Evidence that cannot be
  pinned to a label gets a pseudo-target: `record:<file stem>` for records
  without `target:`, `suite:<testsuite name>` for JUnit outside a
  `bazel-testlogs` tree — also the JUnit `rr wrap` or `rr_node_test` wrote,
  once copied out of it: neither writes its label into the report. A test in
  another repository gets that repository's canonical name, which Bazel 7
  spells `@repo~` and Bazel 8 `@repo+`, so until 0.3 normalizes labels its
  keys differ between the two.
- **Path**: `<classname>::<name>` (just `<name>` without a classname), Unicode
  NFC, with surrounding whitespace stripped. An `[rr:ID]` tag inside a name is
  removed (and read as a declared id), so re-tagging a test never renames its
  case. A target that only
  produced Bazel's generated report, or a result our writers mark
  `rr.synthetic=true`, has one case, `[target]`.
- **Not identity**: retries (`test_attempts/attempt_N.xml`), repetitions
  (`run_k_of_n`), shards (`shard_i_of_n`, or `shard_i_of_n_run_k_of_m` for a
  sharded test run several times) and a second evidence root. They are
  folded into one result per key: the final attempt decides, with an earlier
  failure under a final pass marked *flaky*; across runs and roots the worst
  status wins; one key in two shards (or twice in one report) is marked
  *duplicate* — except a whole-run result (`[target]`, `rr.scope=target`),
  which every shard has. A key seen only in an earlier attempt of a run that has a
  final report (typically the `[target]` result of an attempt that crashed)
  is not a case: its failure marks that run's passing cases *flaky*.
- A case whose classname and name are both empty gets the path `[unnamed]`.

`rr cases` prints every key in a set of evidence, with its status, the ids its
evidence declares, flags (`synthetic`, `target_scope`, `flaky`, `duplicate`)
and the test source when known (the `rr.file` property, else the testcase's
`file` attribute, as `rr_case.h` and googletest write it). It needs no model:

```console
$ rr cases --evidence bazel-testlogs --target //pi/server:server_test
//pi/server:server_test#pi.server.tests.test_handler::test_configure_renegotiates_mid_capture	passed	PR-11,PR-13	-	-
...
$ rr cases --evidence bazel-testlogs --json > cases.json
```

In the tab-separated output, a tab, newline or backslash inside a field is
written `\t`, `\n` or `\\`; the JSON output keeps names as they are. An
`--evidence` path that holds no evidence file is a warning, and no evidence at
all is an error (exit status 2).

Copy keys from here rather than guessing them. The keys do not change any
verdict today; they are what {doc}`migrating-to-per-case` assigns owners to.

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
