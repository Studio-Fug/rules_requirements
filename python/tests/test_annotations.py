# SPDX-License-Identifier: AGPL-3.0-or-later
import subprocess

import pytest
from conftest import write

from rules_requirements.annotations import (
    candidate_files,
    extract,
    is_test_path,
    scan,
    unknown_references,
)
from rules_requirements.config import Config, parse_config

SRC = '''
# @rr(REQ-0001): Implements isolated access to secure data
class SecureStore:
    pass

@rr.implements(REQ-2, REQ-3)
def helper():  # @rr(MIT-1)
    """Docstring @rr(REQ-4): inline description."""

// @rr.verifies(REQ-5)
TEST(Parser, RejectsEmpty) {
  RR_VERIFIES("REQ-6", "REQ-7");
}

#[test]
fn it_cuts_off() {
    rr::verifies!("REQ-8"; level = "sil");
}

@pytest.mark.rr("REQ-9", level="hil")
@pytest.mark.requirements("REQ-10")
def test_x(): ...

# @rr(nothing here) and @rr() are ignored
/* @rr(UN-1, RISK-2): multiple kinds */
'''


def test_extract_all_forms():
    refs = extract(SRC, "src/lib.py", Config())
    got = [(r.ids, r.relation, r.line, r.text, r.symbol) for r in refs]
    assert got[0] == (("REQ-0001",), "implements", 2, "Implements isolated access to secure data", "class SecureStore")
    assert (("REQ-2", "REQ-3"), "implements", 6, "", "def helper") in got
    assert (("MIT-1",), "implements", 7, "", "def helper") in got
    assert any(g[0] == ("REQ-4",) and g[3] == "inline description." for g in got)
    assert (("REQ-5",), "verifies", 10, "", "Parser.RejectsEmpty") in got
    assert (("REQ-6", "REQ-7"), "verifies", 12, "", "Parser.RejectsEmpty") in got
    assert (("REQ-8",), "verifies", 17, "", "fn it_cuts_off") in got
    assert any(g[0] == ("REQ-9",) and g[1] == "verifies" and g[4] == "def test_x" for g in got)
    assert any(g[0] == ("REQ-10",) for g in got)
    assert (("UN-1", "RISK-2"), "implements", 25, "multiple kinds", "") in got
    assert len(refs) == 10


def test_symbol_binding_stops_at_unrelated_code():
    refs = extract("# @rr(REQ-1)\nx = 1\ndef later(): pass\n", "a.py", Config())
    assert refs[0].symbol == ""
    refs = extract("# @rr(REQ-1)\n\ndef later(): pass\n", "a.py", Config())
    assert refs[0].symbol == ""
    refs = extract('RR_VERIFIES("REQ-1");\n', "a_test.cc", Config())
    assert refs[0].symbol == ""
    refs = extract("# @rr(REQ-1)\n#[inline]\npub(crate) async fn go() {}\n", "a.rs", Config())
    assert refs[0].symbol == "fn go"


def test_rr_case_tag_names_its_case():
    src = (
        "RR_CASE(plain_case) { RR_CHECK(true); }\n"
        'RR_CASE(tagged_case, "REQ-7") {\n'
        "  RR_CHECK(true);\n"
        "}\n"
        "// @rr(REQ-8): the case below\n"
        "RR_CASE(commented_case) {}\n"
    )
    got = [(r.ids, r.relation, r.line, r.symbol) for r in extract(src, "fw/codec_test.cc", Config())]
    # An untagged RR_CASE is no reference; a tagged one verifies, in any file.
    assert got == [(("REQ-7",), "verifies", 2, "tagged_case"), (("REQ-8",), "verifies", 5, "commented_case")]
    assert extract('RR_CASE(x, "REQ-1") {}', "src/lib.cc", Config())[0].relation == "verifies"


@pytest.mark.parametrize(
    "path, is_test",
    [
        ("tests/foo.py", True),
        ("pkg/test_foo.py", True),
        ("pkg/foo_test.cc", True),
        ("web/foo.test.ts", True),
        ("web/foo.spec.ts", True),
        ("pkg/foo_tests.rs", True),
        ("src/testing_utils/x.py", False),
        ("src/contest.py", False),
        ("src/foo.py", False),
    ],
)
def test_is_test_path(path, is_test):
    assert is_test_path(path) is is_test


def test_default_relation_follows_path():
    assert extract("# @rr(REQ-1)", "tests/t.py", Config())[0].relation == "verifies"
    assert extract("# @rr(REQ-1)", "src/t.py", Config())[0].relation == "implements"


def test_custom_prefixes_and_legacy_patterns():
    c = parse_config(
        {"prefixes": {"requirement": "PR"}, "annotation_patterns": [r"Requirements:\s*([A-Z0-9,\s-]+)"]}, []
    )
    refs = extract('"""Requirements: PR-1, PR-22\n"""\n# @rr(PR-3)\n# @rr(REQ-4)', "BUILD.bazel", c)
    assert [r.ids for r in refs] == [("PR-1", "PR-22"), ("PR-3",)]


def test_scan_walk_and_git(tmp_path):
    write(tmp_path, "src/a.py", "# @rr(REQ-1)\n")
    write(tmp_path, "src/b.md", "# @rr(REQ-2)\n")
    write(tmp_path, "BUILD.bazel", "# @rr(REQ-3)\n")
    write(tmp_path, "node_modules/x.js", "// @rr(REQ-4)\n")
    write(tmp_path, "src/_vendor/v.py", "# @rr(REQ-5)\n")
    (tmp_path / "src" / "bin.py").write_bytes(b"\xff\xfe@rr(REQ-6)")
    ids = sorted(i for r in scan(str(tmp_path), Config()) for i in r.ids)
    assert ids == ["REQ-1", "REQ-3"]
    assert candidate_files(str(tmp_path), include=["src/*.md"]) == ["src/b.md"]
    assert candidate_files(str(tmp_path), exclude=["src/*"]) == ["BUILD.bazel"]
    explicit = scan(str(tmp_path), Config(), files=["src/b.md", "missing.py"])
    assert [r.ids for r in explicit] == [("REQ-2",)]
    # git-aware: ignored files are skipped
    if subprocess.run(["git", "--version"], capture_output=True).returncode == 0:
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        write(tmp_path, ".gitignore", "src/a.py\n")
        ids = sorted(i for r in scan(str(tmp_path), Config()) for i in r.ids)
        assert ids == ["REQ-3"]


def test_unknown_references(model):
    refs = extract("# @rr(REQ-1, REQ-77)\n# @rr(UN-1)", "a.py", Config())
    assert [(r.line, i) for r, i in unknown_references(refs, model)] == [(1, "REQ-77")]
    assert refs[0].to_dict() == {"ids": ["REQ-1", "REQ-77"], "relation": "implements", "path": "a.py", "line": 1}
