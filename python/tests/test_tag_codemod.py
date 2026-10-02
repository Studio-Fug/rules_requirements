# SPDX-License-Identifier: AGPL-3.0-or-later
"""`rr migrate apply --stage tags`: the ast codemod that splits multi-id tags."""

import os
import re
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
            "no Python source of this module was scanned (another language?)"
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
    assert "no Python source of this module" in unresolved[CaseKey("//t:g", "Suite::Test")]
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


# --------------------------------------------------------------------------- #
# What the static view cannot see: refuse, never mis-attribute                #
# --------------------------------------------------------------------------- #


def _repo(tmp_path, rel, text):
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(src(text), encoding="utf-8")
    return path


def _runtime_traces(tmp_path, rel):
    """(ids) per test id as the real pytest plugin collects them."""
    probe = tmp_path / "conftest.py"
    probe.write_text(
        "from rules_requirements.hooks.pytest_plugin import trace_of\n\n\n"
        "def pytest_collection_modifyitems(items):\n"
        "    for item in items:\n"
        "        print('TRACE', item.nodeid, ','.join(trace_of(item)[0]))\n",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-s", "-p", "no:cacheprovider", "-p", "no:randomly"]
        + ["--rootdir", str(tmp_path), "-c", os.devnull, rel],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
    )
    probe.unlink()
    if "No module named" in proc.stderr:
        pytest.skip(f"pytest is not importable in a subprocess: {proc.stderr.strip()}")
    out = {}
    for line in proc.stdout.splitlines():
        if line.startswith("TRACE "):
            _, nodeid, ids = line.split(" ", 2)
            out[nodeid.split("::", 1)[1]] = tuple(i for i in ids.split(",") if i)
    assert out, proc.stdout + proc.stderr
    return out


def test_inherited_tests_are_refused_not_misattributed(tmp_path):
    """TestSub inherits test_x and the class marker of TestBase: narrowing
    TestBase's marker to A-1 would silently give TestSub's tests A-1."""
    path = _repo(
        tmp_path,
        "pkg/test_inh.py",
        """
        import pytest


        @pytest.mark.rr("A-1", "B-2")
        class TestBase:
            def test_x(self):
                pass


        class TestSub(TestBase):
            def test_y(self):
                pass
        """,
    )
    # Why: at runtime the subclass's tests carry the base's ids too.
    assert _runtime_traces(tmp_path, "pkg/test_inh.py") == {
        "TestBase::test_x": ("A-1", "B-2"),
        "TestSub::test_x": ("A-1", "B-2"),
        "TestSub::test_y": ("A-1", "B-2"),
    }
    decided = {
        CaseKey("//pkg:t", "pkg.test_inh.TestBase::test_x"): "A-1",
        CaseKey("//pkg:t", "pkg.test_inh.TestSub::test_x"): "B-2",
        CaseKey("//pkg:t", "pkg.test_inh.TestSub::test_y"): "B-2",
    }
    res = tag_codemod.apply_tags(decided, str(tmp_path))
    (f,) = res.files
    assert f.status == "refused" and "class TestSub inherits tests or declarations from TestBase" in f.reasons[0]
    assert res.untagged == []  # TestSub::test_y is tagged at runtime
    assert [str(k) for k, _ in res.unmatched] == ["//pkg:t#pkg.test_inh.TestSub::test_x"]
    assert path.read_text(encoding="utf-8").count('"A-1", "B-2"') == 1
    # Through rewrite() directly, too.
    with pytest.raises(Unsupported, match="inherits"):
        rewrite(TestFile(path.read_text(encoding="utf-8")), {"TestBase.test_x": "A-1"})


def test_tests_inside_blocks_are_refused(tmp_path):
    _repo(
        tmp_path,
        "pkg/test_if.py",
        """
        import sys

        import pytest

        pytestmark = pytest.mark.rr("A-1", "B-2")


        def test_a():
            pass


        if sys.platform:

            def test_hidden():
                pass
        """,
    )
    assert _runtime_traces(tmp_path, "pkg/test_if.py")["test_hidden"] == ("A-1", "B-2")
    decided = {
        CaseKey("//pkg:t", "pkg.test_if::test_a"): "A-1",
        CaseKey("//pkg:t", "pkg.test_if::test_hidden"): "B-2",
    }
    for cases in (decided, {k: v for k, v in decided.items() if "hidden" not in k.path}):
        res = tag_codemod.apply_tags(cases, str(tmp_path))
        (f,) = res.files
        assert f.status == "refused" and "test_hidden is defined inside a if block" in f.reasons[0], f.reasons
    assert [str(k) for k, _ in tag_codemod.apply_tags(decided, str(tmp_path)).unmatched] == [
        "//pkg:t#pkg.test_if::test_hidden"
    ]


def test_case_of_a_module_whose_test_is_not_defined_there(tmp_path):
    """A test inherited from another module: decided, but invisible here.
    The module marker that reaches it must not change."""
    _repo(
        tmp_path,
        "pkg/test_impl.py",
        """
        import pytest
        from pkg.base import Base

        pytestmark = pytest.mark.rr("A-1", "B-2")


        class TestImpl(Base):
            def test_own(self):
                pass
        """,
    )
    decided = {
        CaseKey("//pkg:t", "pkg.test_impl.TestImpl::test_own"): "A-1",
        CaseKey("//pkg:t", "pkg.test_impl.TestImpl::test_inherited"): "B-2",
    }
    res = tag_codemod.apply_tags(decided, str(tmp_path))
    (f,) = res.files
    assert f.status == "refused" and "TestImpl.test_inherited was decided" in f.reasons[0]
    ((key, why),) = res.unmatched
    assert key.path.endswith("test_inherited") and "no TestImpl.test_inherited defined in pkg/test_impl.py" in why


def test_verifies_on_an_outer_class_does_not_reach_a_nested_class():
    """@rr.verifies sets an attribute read from item.cls (the innermost class);
    pytest markers, by contrast, reach nested classes."""
    text = src(
        """
        import pytest
        from rules_requirements import rr


        @rr.verifies("A-1", "B-2")
        @pytest.mark.rr("C-3")
        class TestOuter:
            def test_a(self):
                pass

            class TestInner:
                def test_b(self):
                    pass
        """
    )
    assert traces(text) == {
        "TestOuter.test_a": (("A-1", "B-2", "C-3"), "", ()),
        "TestOuter.TestInner.test_b": (("C-3",), "", ()),
    }
    new, _ = rewrite(TestFile(text), {"TestOuter.test_a": "C-3"})
    assert traces(new) == {"TestOuter.test_a": (("C-3",), "", ()), "TestOuter.TestInner.test_b": (("C-3",), "", ())}


def test_runtime_agrees_on_nested_verifies(tmp_path):
    _repo(
        tmp_path,
        "pkg/test_nested.py",
        """
        from rules_requirements import rr


        @rr.verifies("A-1")
        class TestOuter:
            def test_a(self):
                pass

            class TestInner:
                def test_b(self):
                    pass
        """,
    )
    assert _runtime_traces(tmp_path, "pkg/test_nested.py") == {
        "TestOuter::test_a": ("A-1",),
        "TestOuter::TestInner::test_b": (),
    }


def test_comments_inside_a_rewritten_declaration_are_not_lost():
    text = src(
        """
        import pytest

        pytestmark = [
            pytest.mark.rr("A-1", "B-2"),  # the protocol pair
            pytest.mark.slow,
        ]


        def test_a():
            pass
        """
    )
    with pytest.raises(Unsupported, match="comments inside"):
        rewrite(TestFile(text), {"test_a": "A-1"})


def test_crlf_files_keep_their_line_endings(tmp_path):
    body = 'import pytest\n\npytestmark = pytest.mark.rr("A-1", "B-2")\n\n\ndef test_a():\n    pass\n'
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "test_crlf.py").write_bytes(body.replace("\n", "\r\n").encode())
    res = tag_codemod.apply_tags({CaseKey("//pkg:t", "pkg.test_crlf::test_a"): "A-1"}, str(tmp_path))
    (f,) = res.changed
    assert f.new_text.count("\r\n") == f.new_text.count("\n") and "\n" in f.new_text
    assert f.old_text == body.replace("\n", "\r\n")
    changed = [a for a, b in zip(f.old_text.splitlines(), f.new_text.splitlines()) if a != b]
    assert changed == ['pytestmark = pytest.mark.rr("A-1", "B-2")']


def test_only_counts_cases_outside_instead_of_listing_them(tmp_path):
    for name in ("test_a", "test_b"):
        _repo(
            tmp_path,
            f"pkg/{name}.py",
            'import pytest\n\npytestmark = pytest.mark.rr("A-1", "B-2")\n\n\ndef test_t():\n    pass\n',
        )
    decided = {CaseKey("//pkg:t", f"pkg.{n}::test_t"): "A-1" for n in ("test_a", "test_b")}
    decided[CaseKey("//cc:t", "Codec::RoundTrip")] = "A-1"
    res = tag_codemod.apply_tags(decided, str(tmp_path), only=["pkg/test_a.py"])
    assert [f.path for f in res.changed] == ["pkg/test_a.py"]
    assert res.unresolved == [] and len(res.outside) == 2


# --------------------------------------------------------------------------- #
# Black stability, swept over lengths                                         #
# --------------------------------------------------------------------------- #

_SHAPES = {
    "decorator": '@pytest.mark.rr("A-1", "B-2", level="{f}")\ndef test_a():\n    pass\n',
    "decorator-artifact": '@pytest.mark.rr("A-1", "B-2", artifact={{"board": "{f}", "rev": "c"}})\ndef test_a():\n    pass\n',
    "single": 'pytestmark = pytest.mark.requirements("A-1", "B-2", level="{f}")\n',
    "list": 'pytestmark = [pytest.mark.rr("A-1", "B-2"), pytest.mark.usefixtures("{f}")]\n',
    "list-one": 'pytestmark = [pytest.mark.rr("A-1", "B-2", level="{f}")]\n',
    "list-magic": 'pytestmark = [pytest.mark.rr("A-1", "B-2", level="{f}"),]\n',
    "tuple-one": 'pytestmark = (pytest.mark.rr("A-1", "B-2", level="{f}"),)\n',
    "list-long-element": (
        'pytestmark = [pytest.mark.requirements("A-1", "B-2", level="hardware-in-the-loop", '
        'artifact={{"board": "{f}"}}), pytest.mark.slow]\n'
    ),
}


@pytest.mark.parametrize("shape", sorted(_SHAPES))
@pytest.mark.parametrize("line_length", [88, 100])
def test_rewrite_lays_out_like_black_across_lengths(shape, line_length):
    """For every length of a black-formatted input, narrowing a declaration
    gives exactly what black makes of the same source with the id deleted by
    hand — so black afterwards changes nothing, and the layout is black's
    own (hugged call arguments, exploded collections, magic commas kept)."""
    pytest.importorskip("black")
    mode = black.Mode(line_length=line_length)
    wrong = []
    for n in range(1, 90, 2):
        body = _SHAPES[shape].format(f="x" * n)
        if not body.startswith("@"):
            body += "\n\ndef test_a():\n    pass\n"
        text = black.format_str("import pytest\n\n\n" + body, mode=mode)
        new, _ = rewrite(TestFile(text), {"test_a": "A-1"}, line_length=line_length)
        assert traces(new)["test_a"][0] == ("A-1",)
        by_hand = black.format_str(re.sub(r'"A-1",(\s*)"B-2"', '"A-1"', text), mode=mode)
        if new != by_hand or black.format_str(new, mode=mode) != new:
            wrong.append(n)
    assert wrong == []


def test_the_internal_check_refuses_a_wrong_rewrite(monkeypatch):
    """The re-parse check is the last line of defence: a rewrite whose result
    does not trace as intended is refused, whatever produced it."""
    text = 'import pytest\n\npytestmark = pytest.mark.rr("A-1", "B-2")\n\n\ndef test_a():\n    pass\n'
    monkeypatch.setattr(tag_codemod, "_apply", lambda tf, *a: tf.text.replace('"A-1", "B-2"', '"B-2"'))
    with pytest.raises(Unsupported, match="internal check"):
        rewrite(TestFile(text), {"test_a": "A-1"})
    monkeypatch.setattr(
        tag_codemod,
        "_apply",
        lambda tf, *a: tf.text.replace('"A-1", "B-2"', '"A-1"') + "\n\ndef test_new():\n    pass\n",
    )
    with pytest.raises(Unsupported, match="set of tests"):
        rewrite(TestFile(text), {"test_a": "A-1"})
