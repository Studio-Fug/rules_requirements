# SPDX-License-Identifier: AGPL-3.0-or-later
import json
import os

import pytest
from conftest import junit, write

from rules_requirements import ingest
from rules_requirements.ingest import Ingestor, TestCase
from rules_requirements.ingest.junit import JUnitIngestor, target_from_path
from rules_requirements.ingest.libtest import merge_trace, parse_libtest


@pytest.mark.parametrize(
    "path, label",
    [
        ("/x/bazel-testlogs/pkg/sub/name/test.xml", "//pkg/sub:name"),
        ("/x/bazel-out/k8-fastbuild/testlogs/pkg/name/test.xml", "//pkg:name"),
        ("/x/bazel-testlogs/name/test.xml", "//:name"),
        ("/x/bazel-testlogs/pkg/name/shard_2_of_4/test.xml", "//pkg:name"),
        ("/x/bazel-testlogs/pkg/name/run_1_of_3/test.xml", "//pkg:name"),
        ("/x/bazel-testlogs/external/repo+/pkg/name/test.xml", "@repo+//pkg:name"),
        ("/x/reports/results.xml", ""),
        ("C:\\x\\bazel-testlogs\\pkg\\name\\test.xml", "//pkg:name"),
    ],
)
def test_target_from_path(path, label):
    assert target_from_path(path) == label


def test_junit_dialects(tmp_path):
    xml = """<?xml version="1.0"?>
    <testsuite name="top">
      <properties><property name="level" value="hil"/><property name="artifact.fw" value="1.2"/></properties>
      <testcase classname="c" name="a" time="0.5">
        <properties>
          <property name="requirement" value="REQ-1"/>
          <property name="requirements" value="REQ-2, REQ-3,REQ-1"/>
          <property name="owner" value="qa"/>
        </properties>
      </testcase>
      <testcase classname="c" name="b" requirements="REQ-4" level="sil" custom="x" time="bad"/>
      <testcase classname="c" name="c" status="notrun"/>
      <testcase classname="c" name="d"><failure>long text</failure></testcase>
      <testcase classname="c" name="e"><error message="kaput"/></testcase>
      <testsuite name="nested"><testcase name="f"><skipped/></testcase></testsuite>
    </testsuite>"""
    path = write(tmp_path, "bazel-testlogs/pkg/t/test.xml", xml)
    cases = {c.name: c for c in JUnitIngestor().ingest(path)}
    a = cases["a"]
    assert a.requirements == ("REQ-1", "REQ-2", "REQ-3")
    assert a.level == "hil" and a.artifact == {"fw": "1.2"}
    assert a.properties == {"owner": "qa"}
    assert a.duration == 0.5 and a.target == "//pkg:t"
    assert a.full_name == "//pkg:t c::a"
    b = cases["b"]
    assert b.requirements == ("REQ-4",) and b.level == "sil" and b.properties == {"custom": "x"}
    assert b.duration == 0.0
    assert cases["c"].status == "skipped"
    assert cases["d"].status == "failed" and cases["d"].message == "long text" and cases["d"].is_failure
    assert cases["e"].status == "error" and cases["e"].message == "kaput"
    assert cases["f"].status == "skipped" and cases["f"].full_name == "//pkg:t f"


def test_junit_unparseable(tmp_path):
    assert JUnitIngestor().ingest(write(tmp_path, "x.xml", "<testsuite><oops")) == []


def test_collect_walks_dirs_and_skips_unknown(tmp_path):
    junit(tmp_path, "logs/a/t1/test.xml", [("x", "passed", ["REQ-1"], "")])
    junit(tmp_path, "logs/a/t2/test.xml", [("y", "failed", ["REQ-1"], ""), ("z", "passed", [], "")])
    write(tmp_path, "logs/a/t1/test.log", "running 1 test")
    write(tmp_path, "logs/other.xml", "<project/>")
    ev = ingest.collect([str(tmp_path / "logs"), "", str(tmp_path / "missing")])
    assert len(ev.files) == 2
    assert len(ev.skipped_files) == 2
    assert [c.name for c in ev.for_id("REQ-1")] == ["x", "y"]
    globbed = ingest.collect([str(tmp_path / "logs" / "**" / "test.xml")])
    assert len(globbed.cases) == 3
    assert next(ingest.iter_cases(globbed)).name == "x"


def test_target_status_worst_wins(tmp_path):
    base = tmp_path / "bazel-testlogs"
    junit(base, "p/t/test.xml", [("a", "passed", [], ""), ("b", "skipped", [], ""), ("c", "error", [], "")])
    ev = ingest.collect([str(base)])
    assert ev.target_status == {"//p:t": "error"}


LIBTEST = """
running 4 tests
test parse::rejects_empty ... ok
test parse::accepts_c ... FAILED
test slow::soak ... ignored, needs hardware
test top_level ... ok
test panics - should panic ... ok

failures:

---- parse::accepts_c stdout ----
thread 'parse::accepts_c' panicked at src/lib.rs:3:5:
assertion failed

failures:
    parse::accepts_c

test result: FAILED. 2 passed; 1 failed; 1 ignored
"""


def test_libtest_parse_and_trace():
    cases = {f"{c.classname}::{c.name}": c for c in parse_libtest(LIBTEST, target="//r:t")}
    assert cases["parse::rejects_empty"].status == "passed"
    failed = cases["parse::accepts_c"]
    assert failed.status == "failed" and "assertion failed" in failed.message
    assert cases["slow::soak"].status == "skipped" and cases["slow::soak"].message == "needs hardware"
    assert cases["::top_level"].classname == ""
    assert cases["::panics"].status == "passed"
    trace = "\n".join(
        [
            json.dumps({"test": "parse::rejects_empty", "requirements": ["REQ-1"], "level": "sil"}),
            json.dumps({"test": "top_level", "requirements": ["REQ-2"], "artifact": {"k": "v"}}),
            json.dumps({"test": "unknown::test", "requirements": ["REQ-9"]}),
            "not json",
            "",
        ]
    )
    merge_trace(list(cases.values()), trace)
    assert cases["parse::rejects_empty"].requirements == ("REQ-1",)
    assert cases["parse::rejects_empty"].level == "sil"
    assert cases["::top_level"].artifact == {"k": "v"}


def test_libtest_ingestor_reads_sidecar(tmp_path):
    path = write(tmp_path, "run.libtest.txt", LIBTEST)
    write(tmp_path, "run.rrtrace.jsonl", json.dumps({"test": "top_level", "requirements": ["REQ-7"]}) + "\n")
    ev = ingest.collect([path])
    assert [c.name for c in ev.for_id("REQ-7")] == ["top_level"]
    alone = write(tmp_path, "solo/x.libtest", LIBTEST)
    assert len(ingest.collect([alone]).cases) == 5


def test_records_ingestor(tmp_path):
    yaml_path = write(
        tmp_path,
        "signoff.rr.yaml",
        """
        evidence:
          - name: label-legible
            requirements: REQ-12
            level: inspection
            artifact: {board_rev: C}
            properties: {signed_by: J. Doe}
          - name: weird
            status: exploded
          - not-a-mapping
        """,
    )
    json_path = write(
        tmp_path, "bench.rr.json", json.dumps([{"requirements": ["REQ-1"], "status": "failed", "duration": 2}])
    )
    ev = ingest.collect([yaml_path, json_path])
    by_name = {c.name: c for c in ev.cases}
    lbl = by_name["label-legible"]
    assert lbl.requirements == ("REQ-12",) and lbl.level == "inspection"
    assert lbl.artifact == {"board_rev": "C"} and lbl.properties == {"signed_by": "J. Doe"}
    assert by_name["weird"].status == "error"
    assert by_name["record-1"].status == "failed" and by_name["record-1"].duration == 2


class TapIngestor(Ingestor):
    name = "tap"
    suffixes = (".tap",)

    def ingest(self, path):
        with open(path) as fh:
            for line in fh:
                if line.startswith(("ok", "not ok")):
                    ok = line.startswith("ok")
                    name = line.split("-", 1)[1].split("#")[0].strip()
                    reqs = tuple(line.split("# rr:")[1].split()) if "# rr:" in line else ()
                    yield TestCase(name=name, status="passed" if ok else "failed", requirements=reqs, source=path)


def test_custom_ingestor_registration(tmp_path):
    path = write(tmp_path, "r.tap", "TAP version 13\nok 1 - adds # rr: REQ-1\nnot ok 2 - subtracts\n")
    assert ingest.collect([path]).cases == []  # not registered yet
    ingest.register(TapIngestor())
    try:
        ev = ingest.collect([path])
        assert [(c.name, c.status) for c in ev.cases] == [("adds", "passed"), ("subtracts", "failed")]
        assert ev.for_id("REQ-1")[0].name == "adds"
        only = ingest.collect([path], only=["junit"])
        assert only.cases == []
    finally:
        ingest._REGISTRY.pop("tap", None)


def test_load_ingestor_by_spec(tmp_path, monkeypatch):
    mod = tmp_path / "my_ingestors.py"
    mod.write_text(
        "from rules_requirements.ingest import Ingestor\n"
        "class Csv(Ingestor):\n    name = 'csv'\n    suffixes = ('.csv',)\n    def ingest(self, path):\n        return []\n"
        "INGESTOR = Csv()\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        assert ingest.load_ingestor("my_ingestors:Csv").name == "csv"
        assert ingest.load_ingestor("my_ingestors").name == "csv"
        assert "csv" in ingest.ingestors()
    finally:
        ingest._REGISTRY.pop("csv", None)


def test_register_requires_name_and_base_ingest_abstract(tmp_path):
    with pytest.raises(ValueError):
        ingest.register(Ingestor())
    with pytest.raises(NotImplementedError):
        Ingestor().ingest("x")
    assert Ingestor().sniff("x.xml", b"") is False
    assert ingest.ingestor_for(str(tmp_path / "nope.xml")) is None
    assert os.path.exists(tmp_path)
