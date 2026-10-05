# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every attribution issue is a gap, so ``--fail-on gaps`` fails on each one.

The docs that describe ``--fail-on gaps`` or the 0.2 -> 0.3 upgrade list the
attribution issues that are gaps. These tests pin that list three ways, so
code and docs cannot drift apart silently:

* every code an attribution issue is built with (scanned from the source) is
  in :data:`rules_requirements.trace.ATTRIBUTION_ISSUE_CODES`;
* :func:`~rules_requirements.trace.find_gaps` turns each of them into a gap;
* the table in docs/guides/outputs.md (severity, configurability) and the
  lists in the release notes, the migration guide and the changelog name
  exactly these codes.
"""

import ast
import os
import re
from dataclasses import replace

import pytest
from conftest import junit, write

from rules_requirements import attribution, ingest, trace
from rules_requirements import config as cfg
from rules_requirements.annotations import MULTI_VERIFIES
from rules_requirements.attribution import AttributionIssue
from rules_requirements.model import read_model
from rules_requirements.trace import ATTRIBUTION_ISSUE_CODES, build_matrix, find_gaps

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOTS = (os.path.dirname(os.path.dirname(_HERE)), os.path.join(os.environ.get("TEST_SRCDIR", ""), "_main"))
CODES = set(ATTRIBUTION_ISSUE_CODES)
CODE = re.compile(r"`([a-z]+(?:-[a-z]+)+)`")


def _doc(rel):
    for root in _ROOTS:
        path = os.path.join(root, rel)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                return fh.read()
    raise AssertionError(f"{rel} not found (under Bazel it must be in the test's data)")


def _emitted_codes():
    """The literal codes the source builds attribution issues with."""
    pkg = os.path.dirname(os.path.abspath(trace.__file__))
    found, unresolved = set(), []
    for rel in ("attribution.py", "trace.py", os.path.join("ingest", "__init__.py")):
        with open(os.path.join(pkg, rel), encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else ""
            index = {"_issue": 1, "issue": 0, "AttributionIssue": 0, "IngestIssue": 0}.get(name)
            if index is None or len(node.args) <= index:
                continue
            arg = node.args[index]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                found.add(arg.value)
            elif isinstance(arg, ast.Name) and arg.id == "MULTI_VERIFIES":
                found.add(MULTI_VERIFIES)
            elif ast.unparse(arg) == "ingest_issue.code":
                continue  # an IngestIssue's code: collected at IngestIssue(...)
            elif isinstance(arg, ast.Name) and arg.id == "code":
                continue  # inside the _issue() / issue() wrappers: their callers are scanned
            else:
                unresolved.append(f"{rel}:{node.lineno}: {ast.unparse(arg)}")
    return found, unresolved


def test_every_code_an_attribution_issue_is_built_with_is_listed():
    found, unresolved = _emitted_codes()
    assert not unresolved, f"name the code literally, or teach this test: {unresolved}"
    assert found == CODES, f"emitted but not listed: {found - CODES}; listed but never emitted: {CODES - found}"
    assert len(ATTRIBUTION_ISSUE_CODES) == len(CODES)


@pytest.mark.parametrize("code", sorted(CODES - set(trace._AGGREGATED_ISSUES)))
def test_each_attribution_issue_is_a_gap(tmp_path, code):
    model, _ = read_model(write(tmp_path, "m.yaml", "user_needs: [{id: UN-1, title: n}]\n"))
    matrix = build_matrix(model, ingest.Evidence())
    assert not [g for g in matrix.gaps if g.kind == code]
    issue = AttributionIssue(code, f"a {code} issue", "warning")
    forged = replace(matrix, attribution=replace(matrix.attribution, issues=(issue,)))
    assert [g.kind for g in find_gaps(forged) if g.kind == code] == [code]


def test_misdirected_and_unknown_ids_are_one_gap_per_id(tmp_path):
    model, _ = read_model(
        write(
            tmp_path,
            "m.yaml",
            "user_needs: [{id: UN-1, title: n}]\nrisks: [{id: RISK-1, title: r}]\n",
        )
    )
    junit(tmp_path, "j/test.xml", [("a", "passed", ["RISK-1"], ""), ("b", "passed", ["REQ-9"], ""),
                                   ("c", "passed", ["REQ-9"], "")])  # fmt: skip
    matrix = build_matrix(model, ingest.collect([str(tmp_path / "j")]))
    codes = {i.code for i in matrix.attribution.issues}
    assert {"misdirected-evidence", "unknown-id"} <= codes
    kinds = [(g.kind, g.entity) for g in matrix.gaps if g.kind in trace._AGGREGATED_ISSUES]
    assert sorted(kinds) == [("misdirected-evidence", "RISK-1"), ("unknown-id", "REQ-9")]


def _table(text, heading):
    """The rows of the first Markdown table after ``heading``."""
    rest = text[text.index(heading) :]
    rows = []
    for line in rest.splitlines()[1:]:
        if line.startswith("|"):
            rows.append([cell.strip() for cell in line.strip().strip("|").split("|")])
        elif rows:
            break
    return rows[2:]  # past the header and the rule


def test_the_outputs_table_lists_exactly_the_attribution_issues_with_their_severity():
    rows = _table(_doc("docs/guides/outputs.md"), "### Attribution issues are gaps")
    listed = [CODE.fullmatch(row[0]).group(1) for row in rows]
    assert listed == list(ATTRIBUTION_ISSUE_CODES)  # the code's order, so a new code shows up here
    for code, severity, configurable, do in rows:
        code = code.strip("`")
        want = attribution._FIXED_SEVERITY.get(code) or cfg.DEFAULT_RULES.get(code)
        if code == "lock-invalid":
            want = "error"  # also raised by build_matrix for an unreadable lock
        assert severity == want, code
        assert configurable == ("yes" if code in cfg.DEFAULT_RULES else "no"), code
        assert do.split(":")[0].split(" (")[0] in ("Lock", "Resolve"), code


def _bullet(text, start):
    """The Markdown list item (or step) that begins with ``start``."""
    begin = text.index(start)
    indent = begin - text.rindex("\n", 0, begin) - 1
    lines = text[begin:].splitlines()
    out = [lines[0]]
    for line in lines[1:]:
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            break
        out.append(line)
    return "\n".join(out)


@pytest.mark.parametrize(
    "doc, start",
    [
        ("docs/release-notes.md", "- **`--fail-on gaps` fails on more gaps.**"),
        ("docs/guides/migrating-to-per-case.md", "2. Replace each shared whole-target reference"),
        ("CHANGELOG.md", "- Verdicts that move at the bump:"),
    ],
)
def test_the_upgrade_docs_list_exactly_the_attribution_issues_that_are_gaps(doc, start):
    item = _bullet(_doc(doc), start)
    assert "--fail-on gaps" in item
    named = set(CODE.findall(item)) & (CODES | set(cfg.HARD_ERRORS) | set(cfg.QUARANTINE_CODES))
    assert named == CODES, f"{doc}: missing {sorted(CODES - named)}, extra {sorted(named - CODES)}"


def test_the_gap_queue_table_has_a_row_for_every_attribution_issue():
    rows = _table("\n" + _doc("docs/concepts.md"), "\n| Gap kind | Raised for | Route |")
    kinds = {k for row in rows for k in CODE.findall(row[0])}
    assert kinds >= CODES, sorted(CODES - kinds)


# The unscoped-evidence fix changes case keys (suite:<name>#... to the build
# target's), so a lock written first keeps stale suite: entries: every place
# that tells an upgrader to move the JUnit also says to do it before locking,
# or how to drop them after.
@pytest.mark.parametrize(
    "doc, start",
    [
        ("docs/guides/outputs.md", "| `unscoped-evidence` |"),
        ("docs/release-notes.md", "- **`--fail-on gaps` fails on more gaps.**"),
        ("docs/guides/migrating-to-per-case.md", "2. Replace each shared whole-target reference"),
    ],
)
def test_the_unscoped_evidence_fix_says_to_move_before_locking_or_relock(doc, start):
    item = _bullet(_doc(doc), start)
    assert "before lock" in item or "first" in item, doc
    assert "rr sets lock --write --allow-removals" in item and "`lock-stale`" in item, doc


def test_the_model_guide_says_what_allow_removals_drops_for_an_absent_target():
    text = " ".join(_doc("docs/guides/model.md").split())
    begin = text.index("`rr sets lock` refuses while a case is quarantined")
    para = text[begin : text.index("`rr sets check` exits 1", begin)]
    assert "keeps an entry even with `--allow-removals`" in para
    assert "`suite:`/`record:` pseudo-target" in para and "`lock-stale`" in para


def test_the_release_notes_validation_bullet_is_whole():
    """The rule bullets that follow 'Validation of claims' must not split it:
    its last sentence (rr validate's per-family JUnit) stays in it."""
    text = _doc("docs/release-notes.md")
    validation = _bullet(text, "- **Validation of claims**")
    assert "`rr validate` writes one JUnit case per check family" in " ".join(validation.split())
    for rule in ("multi-parent-refines", "multi-parent-implements"):
        assert "XML_OUTPUT_FILE" not in _bullet(text, f"- **`{rule}`**"), rule
