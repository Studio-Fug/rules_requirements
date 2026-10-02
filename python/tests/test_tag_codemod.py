# SPDX-License-Identifier: AGPL-3.0-or-later
"""`rr migrate apply --stage tags`: the ast codemod that splits multi-id tags."""

import os
import subprocess
import sys
import textwrap

import pytest
from test_migrate import EXPECTED, REPO, fixture_repo

from rules_requirements import ingest, migrate, tag_codemod
from rules_requirements.case_keys import CaseKey
from rules_requirements.model import read_model
from rules_requirements.tag_codemod import TestFile, Unsupported, rewrite

CHANGED = ("test_config.py", "test_legacy.py", "test_link.py", "test_obsolete.py")


def src(text):
    return textwrap.dedent(text).lstrip()


try:  # in the Bazel dev pip hub; optional in a plain checkout
    import black
except ImportError:  # pragma: no cover
    black = None


def assert_black_stable(text, line_length=88):
    """Formatting the codemod's output with black changes nothing."""
    if black is not None:
        assert black.format_str(text, mode=black.Mode(line_length=line_length)) == text


def traces(text):
    tf = TestFile(text)
    return {t.qualname: tf.trace(t) for t in tf.tests}


def test_fixture_repository(tmp_path, monkeypatch):
    root = fixture_repo(tmp_path, monkeypatch)
    doc = migrate.load_worksheet("decided.rrplan")
    res = tag_codemod.apply_tags(migrate.decisions(doc), str(root))
    status = {f.path: f.status for f in res.files}
    assert status == {
        "app/tests/test_config.py": "changed",
        "app/tests/test_legacy.py": "changed",
        "app/tests/test_link.py": "changed",
        "app/tests/test_obsolete.py": "changed",
        "app/tests/test_modes.py": "refused",
    }
    (modes,) = res.refused
    assert "decided differently (REQ-1, REQ-3)" in modes.reasons[0] and "pytest.param" in modes.reasons[0]
    for f in res.changed:
        with open(os.path.join(EXPECTED, f.path), encoding="utf-8") as fh:
            expected = fh.read()
        if os.environ.get("RR_UPDATE_FIXTURES"):
            with open(os.path.join(EXPECTED, f.path), "w", encoding="utf-8") as fh:
                fh.write(f.new_text)
            expected = f.new_text
        assert f.new_text == expected, f.path
    config = next(f for f in res.changed if f.path.endswith("test_config.py"))
    assert config.changes == [
        "test_parses_minimal_file: REQ-1, REQ-2 -> REQ-1",
        "test_rejects_missing_key: REQ-1, REQ-2 -> REQ-2",
        "test_rejects_garbage: REQ-1, REQ-2 -> REQ-2",
        "TestRoundTrip.test_dump_then_load: REQ-1, REQ-2 -> REQ-1",
    ]
    assert dict(res.unresolved) == {
        CaseKey("//cc:codec_test", "Codec::RoundTrip"): (
            "no Python test found (another language, or outside the scanned files)"
        ),
        CaseKey("//web:clock_test", "[target]"): "not a Python test case path",
    }
    # test_boots already carries exactly its decided tag: nothing to rewrite.
    assert "app/tests/test_smoke.py" not in status


def test_fixture_output_is_black_stable():
    pytest.importorskip("black")
    for name in CHANGED:
        with open(os.path.join(EXPECTED, "app", "tests", name), encoding="utf-8") as fh:
            assert_black_stable(fh.read())


def test_rewritten_tests_carry_one_id_each():
    for name in CHANGED:
        with open(os.path.join(EXPECTED, "app", "tests", name), encoding="utf-8") as fh:
            for qualname, (ids, _, _) in traces(fh.read()).items():
                assert len(ids) <= 1, (name, qualname, ids)
    with open(os.path.join(EXPECTED, "app", "tests", "test_legacy.py"), encoding="utf-8") as fh:
        assert traces(fh.read()) == {
            "LegacyTest.test_reads_old_format": (("REQ-1",), "simulation", ()),
            "LegacyTest.test_rejects_old_garbage": (("REQ-2",), "simulation", ()),
        }


def test_applying_twice_changes_nothing(tmp_path, monkeypatch):
    root = fixture_repo(tmp_path, monkeypatch)
    decided = migrate.decisions(migrate.load_worksheet("decided.rrplan"))
    for f in tag_codemod.apply_tags(decided, str(root)).changed:
        (root / f.path).write_text(f.new_text, encoding="utf-8")
    again = tag_codemod.apply_tags(decided, str(root))
    assert again.changed == [] and [f.path for f in again.refused] == ["app/tests/test_modes.py"]


def test_end_to_end_evidence_after_the_codemod(tmp_path, monkeypatch):
    """Run the fixture's tests before and after: tags no longer double-count."""
    root = fixture_repo(tmp_path, monkeypatch)
    (root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    model, _ = read_model("requirements")
    decided = migrate.decisions(migrate.load_worksheet("decided.rrplan"))

    def run_tests():
        xml = root / "evidence" / "testlogs" / "app" / "tests" / "unit_test" / "test.xml"
        files = [f"app/tests/{n}" for n in (*CHANGED, "test_modes.py")]
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "rules_requirements.hooks.pytest_plugin", "-p", "no:randomly"]
            + ["-p", "no:cacheprovider", "-o", "junit_family=xunit2", f"--junitxml={xml}", *files],
            cwd=root,
            env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
            capture_output=True,
            text=True,
            check=False,
        )
        if "No module named" in proc.stderr:
            pytest.skip(f"pytest is not importable in a subprocess: {proc.stderr.strip()}")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        attempt = root / "evidence" / "testlogs" / "app" / "tests" / "unit_test" / "test_attempts" / "attempt_1.xml"
        if attempt.exists():
            attempt.unlink()
        return migrate.census(model, ingest.collect(["evidence"]))

    before = {str(u.row.key) for u in run_tests().contested}
    for f in tag_codemod.apply_tags(decided, str(root)).changed:
        (root / f.path).write_text(f.new_text, encoding="utf-8")
    after = run_tests()
    tag_contested = sorted(str(u.row.key) for u in after.contested if len(u.tags) > 1)
    assert len(before) == 16
    assert tag_contested == [
        "//app/tests:unit_test#app.tests.test_modes::test_modes[fast]",
        "//app/tests:unit_test#app.tests.test_modes::test_modes[slow]",
        "//cc:codec_test#Codec::RoundTrip",
    ]
    owner = {str(u.row.key): u.tags for u in after.units}
    assert owner["//app/tests:unit_test#app.tests.test_config::test_rejects_garbage[{]"] == ("REQ-2",)
    assert owner["//app/tests:unit_test#app.tests.test_obsolete::test_helper_shape"] == ()


# --------------------------------------------------------------------------- #
# Shapes                                                                      #
# --------------------------------------------------------------------------- #


def test_module_marker_narrowed_when_one_owner():
    text = src(
        """
        import pytest

        pytestmark = pytest.mark.requirements("A-1", "B-2", level="hil", artifact={"dut": "x"})


        def test_a():
            pass


        def test_b():
            pass
        """
    )
    new, changes = rewrite(TestFile(text), {"test_a": "B-2", "test_b": "B-2"})
    assert 'pytestmark = pytest.mark.requirements("B-2", level="hil", artifact={"dut": "x"})\n' in new
    assert len(changes) == 2 and new.count("requirements(") == 1
    assert traces(new)["test_a"] == (("B-2",), "hil", (("dut", "x"),))
    assert_black_stable(new)


def test_class_scope_and_function_union():
    text = src(
        """
        import pytest


        @pytest.mark.rr("A-1", "B-2")
        class TestThing:
            pytestmark = [pytest.mark.rr("C-3")]

            def test_one(self):
                pass

            @pytest.mark.rr("A-1", level="sil")  # chosen by hand
            def test_two(self):
                pass


        @pytest.mark.rr("D-4")
        def test_free():
            pass
        """
    )
    new, _ = rewrite(TestFile(text), {"TestThing.test_one": "C-3", "TestThing.test_two": "A-1"})
    assert traces(new) == {
        "TestThing.test_one": (("C-3",), "", ()),
        "TestThing.test_two": (("A-1",), "sil", ()),
        "test_free": (("D-4",), "", ()),
    }
    assert '    @pytest.mark.rr("A-1", level="sil")  # chosen by hand\n' in new
    assert '@pytest.mark.rr("A-1", "B-2")' not in new and "pytestmark" not in new
    assert_black_stable(new)


def test_aliases_and_from_imports():
    text = src(
        """
        import pytest as pt
        from pytest import mark
        from rules_requirements.rr import verifies


        @mark.requirements("A-1", "B-2")
        def test_m():
            pass


        @verifies("A-1", "B-2")
        def test_v():
            pass


        @pt.mark.rr("A-1", "B-2")
        def test_p():
            pass
        """
    )
    new, _ = rewrite(TestFile(text), {"test_m": "A-1", "test_v": "B-2", "test_p": None})
    assert '@mark.requirements("A-1")\n' in new and '@verifies("B-2")\n' in new and "pt.mark" not in new
    assert "import pytest as pt" not in new  # the rewrite took its last use
    assert traces(new) == {"test_m": (("A-1",), "", ()), "test_v": (("B-2",), "", ()), "test_p": ((), "", ())}


def test_tuple_pytestmark_and_comma_ids():
    text = src(
        """
        import pytest

        pytestmark = (pytest.mark.rr("A-1,B-2"), pytest.mark.slow)


        def test_a():
            pass
        """
    )
    new, _ = rewrite(TestFile(text), {"test_a": None})
    assert "pytestmark = (pytest.mark.slow,)\n" in new
    assert_black_stable(new)


def test_long_declarations_wrap_like_black():
    text = src(
        """
        import pytest


        @pytest.mark.requirements("REQ-0001", "REQ-0002", level="hardware-in-the-loop", artifact={"board": "rev-c"})
        def test_a():
            pass


        def test_b():
            pass
        """
    )
    new, _ = rewrite(TestFile(text), {"test_a": "REQ-0002"}, line_length=60)
    assert_black_stable(new, line_length=60)
    new88, _ = rewrite(TestFile(text), {"test_a": "REQ-0002"})
    assert_black_stable(new88)
    assert (
        traces(new)
        == traces(new88)
        == {
            "test_a": (("REQ-0002",), "hardware-in-the-loop", (("board", "rev-c"),)),
            "test_b": ((), "", ()),
        }
    )


def test_level_only_declarations_stay():
    text = src(
        """
        import pytest

        pytestmark = [pytest.mark.rr(level="hil"), pytest.mark.rr("A-1", "B-2")]


        def test_a():
            pass


        def test_b():
            pass
        """
    )
    new, _ = rewrite(TestFile(text), {"test_a": None, "test_b": "B-2"})
    assert 'pytestmark = [pytest.mark.rr(level="hil")]\n' in new
    assert traces(new) == {"test_a": ((), "hil", ()), "test_b": (("B-2",), "hil", ())}
    assert_black_stable(new)


def test_imports_dropped_only_when_the_rewrite_took_the_last_use():
    text = src(
        """
        import os

        import pytest
        from rules_requirements import rr

        pytestmark = pytest.mark.rr("A-1", "B-2")


        @rr.verifies("A-1", "B-2")
        def test_a():
            assert os.sep
        """
    )
    new, _ = rewrite(TestFile(text), {"test_a": None})
    assert new == src(
        """
        import os


        def test_a():
            assert os.sep
        """
    )
    assert_black_stable(new)


@pytest.mark.parametrize(
    ("snippet", "message"),
    [
        ('IDS = ("A-1", "B-2")\npytestmark = pytest.mark.rr(*IDS)\n', "not a string literal"),
        ('pytestmark = pytest.mark.rr("A-1", "B-2", level=LEVEL)\n', "level that is not a string literal"),
        ('pytestmark = pytest.mark.rr("A-1", "B-2", artifact=ART)\n', "artifact that is not a dict literal"),
        (
            '@pytest.mark.parametrize("x", [pytest.param(1, marks=pytest.mark.rr("A-1"))])\ndef test_p(x):\n    pass\n',
            "outside a decorator or pytestmark",
        ),
        ("import pytest; pytestmark = pytest.mark.rr('A-1', 'B-2')\n", "shares its line"),
    ],
)
def test_unsupported_shapes_are_refused(snippet, message):
    text = "import pytest\n\n" + snippet + "\n\ndef test_a():\n    pass\n"
    with pytest.raises(Unsupported, match=message):
        rewrite(TestFile(text), {"test_a": "A-1"})


def test_conflicting_levels_in_one_scope_are_refused():
    text = src(
        """
        import pytest


        @pytest.mark.rr("A-1", level="sil")
        @pytest.mark.rr("B-2", level="hil")
        def test_a():
            pass
        """
    )
    with pytest.raises(Unsupported, match="different levels"):
        rewrite(TestFile(text), {"test_a": "A-1"})


def test_undecided_multi_id_tests(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "test_u.py").write_text(
        src(
            """
            import pytest

            pytestmark = pytest.mark.rr("A-1", "B-2")


            def test_decided():
                pass


            def test_never_ran():
                pass
            """
        ),
        encoding="utf-8",
    )
    decided = {CaseKey("//pkg:t", "pkg.test_u::test_decided"): "A-1"}
    res = tag_codemod.apply_tags(decided, str(tmp_path))
    assert res.refused[0].reasons == ["test_never_ran: names A-1, B-2 and its owner is undecided"]
    res = tag_codemod.apply_tags(decided, str(tmp_path), unassigned="drop")
    (f,) = res.changed
    assert traces(f.new_text) == {"test_decided": (("A-1",), "", ()), "test_never_ran": ((), "", ())}
    # --only limits the files considered.
    assert tag_codemod.apply_tags(decided, str(tmp_path), only=["other"]).files == []


def test_case_to_source_matching(tmp_path):
    for rel in ("a/tests/test_x.py", "b/tests/test_x.py", "c/test_y.py", "c/test_untagged.py"):
        (tmp_path / os.path.dirname(rel)).mkdir(parents=True, exist_ok=True)
    body = 'import pytest\n\npytestmark = pytest.mark.rr("A-1", "B-2")\n\n\ndef test_t():\n    pass\n'
    (tmp_path / "a/tests/test_x.py").write_text(body, encoding="utf-8")
    (tmp_path / "b/tests/test_x.py").write_text(body, encoding="utf-8")
    (tmp_path / "c/test_y.py").write_text(body, encoding="utf-8")
    (tmp_path / "c/test_untagged.py").write_text(
        "import pytest\n\n\n@pytest.mark.skip\ndef test_u():\n    pass\n", encoding="utf-8"
    )
    decided = {
        # Rootdir-relative classnames: "tests.test_x" matches two files.
        CaseKey("//t:x", "tests.test_x::test_t"): "A-1",
        # A longer module path disambiguates.
        CaseKey("//t:a", "a.tests.test_x::test_t[1]"): "A-1",
        CaseKey("//t:y", "c.test_y::test_t"): "B-2",
        CaseKey("//t:u", "c.test_untagged::test_u"): "A-1",
        CaseKey("//t:g", "Suite::Test"): "A-1",
    }
    res = tag_codemod.apply_tags(decided, str(tmp_path), unassigned="drop")
    unresolved = dict(res.unresolved)
    assert unresolved[CaseKey("//t:x", "tests.test_x::test_t")].startswith("ambiguous: matches tests in")
    assert "no Python test found" in unresolved[CaseKey("//t:g", "Suite::Test")]
    assert res.untagged == [CaseKey("//t:u", "c.test_untagged::test_u")]
    assert sorted(f.path for f in res.changed) == ["a/tests/test_x.py", "b/tests/test_x.py", "c/test_y.py"]
    assert traces(next(f for f in res.changed if f.path == "c/test_y.py").new_text)["test_t"][0] == ("B-2",)
    assert traces(next(f for f in res.changed if f.path == "b/tests/test_x.py").new_text)["test_t"][0] == ()


def test_python_files_skips_tool_directories(tmp_path):
    for rel in ("src/test_a.py", ".git/x.py", "bazel-out/y.py", "node_modules/z.py", "venv/w.py", "src/notes.txt"):
        os.makedirs(tmp_path / os.path.dirname(rel), exist_ok=True)
        (tmp_path / rel).write_text("", encoding="utf-8")
    assert tag_codemod.python_files(str(tmp_path)) == ["src/test_a.py"]
    assert tag_codemod.python_files(str(tmp_path), only=["src/"]) == ["src/test_a.py"]
    assert tag_codemod.python_files(str(tmp_path), only=["lib"]) == []


def test_fixture_sources_are_untouched_by_the_tests():
    with open(os.path.join(REPO, "app", "tests", "test_config.py"), encoding="utf-8") as fh:
        assert 'pytestmark = pytest.mark.requirements("REQ-1", "REQ-2")' in fh.read()
