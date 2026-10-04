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


try:  # in the Bazel dev pip hub and the `test` extra; optional in a plain checkout
    import black
except ImportError:  # pragma: no cover
    black = None


def need_black():
    """black, or skip — except where CI sets RR_REQUIRE_BLACK=1: there a
    missing black fails the test instead of silently skipping it."""
    if black is None:
        if os.environ.get("RR_REQUIRE_BLACK") == "1":
            pytest.fail("black is not importable, and RR_REQUIRE_BLACK=1 requires it")
        pytest.skip("black is not installed")
    return black


def assert_black_stable(text, line_length=88):
    """Formatting the codemod's output with black changes nothing."""
    if black is not None or os.environ.get("RR_REQUIRE_BLACK") == "1":
        fmt = need_black()
        assert fmt.format_str(text, mode=fmt.Mode(line_length=line_length)) == text


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
    need_black()
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
    need_black()
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


# --------------------------------------------------------------------------- #
# Across modules: importers and subclasses in other files                     #
# --------------------------------------------------------------------------- #

_BASE = """
import pytest


@pytest.mark.rr("A", "B")
class TestBase:
    def test_x(self):
        assert True
"""

_XMOD_SHEET = """
schema: rules_requirements/attribution-worksheet/v1
groups:
- target: //pkg:t
  group: base
  counts_toward: [A, B]
  owner: A
  cases:
  - path: pkg.test_base.TestBase::test_x
- target: //pkg:t
  group: sub
  counts_toward: [A, B]
  owner: B
  cases:
  - path: pkg.test_other.TestSub::test_x
  - path: pkg.test_other.TestSub::test_y
"""


def _xmod(tmp_path, other="from pkg.test_base import TestBase\n\n\nclass TestSub(TestBase):\n"):
    _repo(tmp_path, "pkg/__init__.py", "")
    _repo(tmp_path, "pkg/test_base.py", _BASE)
    (tmp_path / "pkg" / "test_other.py").write_text(other + "    def test_y(self):\n        assert True\n")
    return {rel: (tmp_path / rel).read_bytes() for rel in ("pkg/test_base.py", "pkg/test_other.py")}


def test_cross_module_inheritance_writes_nothing(tmp_path, capsys):
    """The verifier's repro: narrowing TestBase's marker in test_base.py to A
    would give the subclass in test_other.py A as well, against the
    worksheet. Both files are refused, nothing is written, exit 1."""
    from rules_requirements import cli

    before = _xmod(tmp_path)
    assert _runtime_traces(tmp_path, "pkg/test_other.py")["TestSub::test_x"] == ("A", "B")
    sheet = tmp_path / "ws.rrplan"
    sheet.write_text(_XMOD_SHEET, encoding="utf-8")
    rc = cli.main(["migrate", "apply", str(sheet), "--stage", "tags", "--root", str(tmp_path)])
    err = capsys.readouterr().err
    assert rc == 1
    assert "refused pkg/test_base.py" in err and "pkg/test_other.py imports or subclasses TestBase" in err
    assert "refused pkg/test_other.py" in err and "class TestSub inherits from TestBase (pkg/test_base.py)" in err
    assert {rel: (tmp_path / rel).read_bytes() for rel in before} == before


def test_partial_writes_only_files_without_a_refused_dependency(tmp_path, capsys):
    from rules_requirements import cli

    before = _xmod(tmp_path)
    unrelated = _repo(
        tmp_path,
        "pkg/test_unrelated.py",
        'import pytest\n\npytestmark = pytest.mark.rr("A", "B")\n\n\ndef test_u():\n    pass\n',
    )
    sheet = tmp_path / "ws.rrplan"
    sheet.write_text(
        _XMOD_SHEET
        + "- target: //pkg:t\n  group: u\n  counts_toward: [A, B]\n  owner: B\n"
        + "  cases:\n  - path: pkg.test_unrelated::test_u\n",
        encoding="utf-8",
    )
    argv = ["migrate", "apply", str(sheet), "--stage", "tags", "--root", str(tmp_path)]
    assert cli.main(argv) == 1  # all or nothing by default
    assert "nothing written" in capsys.readouterr().err
    assert '"A", "B"' in unrelated.read_text(encoding="utf-8")
    assert cli.main([*argv, "--partial"]) == 1
    err = capsys.readouterr().err
    assert "rewrote pkg/test_unrelated.py" in err and "1 file(s) rewritten, 2 refused" in err
    assert 'pytestmark = pytest.mark.rr("B")' in unrelated.read_text(encoding="utf-8")
    assert {rel: (tmp_path / rel).read_bytes() for rel in before} == before


@pytest.mark.parametrize(
    "other",
    [
        "from pkg.test_base import TestBase\n\n\nclass TestSub(TestBase):\n",
        "from pkg.test_base import TestBase as Base\n\n\nclass TestSub(Base):\n",
        "import pkg.test_base\n\n\nclass TestSub(pkg.test_base.TestBase):\n",
        "import pkg.test_base as tb\n\n\nclass TestSub(tb.TestBase):\n",
        "from . import test_base\n\n\nclass TestSub(test_base.TestBase):\n",
        "from .test_base import TestBase\n\n\nclass TestSub(TestBase):\n",
        "from elsewhere import *  # noqa\n\n\nclass TestSub(TestBase):\n",  # by name
        "from pkg.test_base import *  # noqa\n\n\nclass TestSub:\n",
        "from pkg.test_base import TestBase  # collected here too\n\n\nclass TestSub:\n",
    ],
)
def test_importers_and_subclasses_in_other_modules_are_refused(tmp_path, other):
    """Only test_base.py has a decision; whatever way test_other.py reaches
    TestBase, its tests would change unseen, so neither file is rewritten."""
    _xmod(tmp_path, other)
    res = tag_codemod.apply_tags({CaseKey("//pkg:t", "pkg.test_base.TestBase::test_x"): "A"}, str(tmp_path))
    assert sorted(f.path for f in res.refused) == ["pkg/test_base.py", "pkg/test_other.py"], res.files
    assert res.changed == [] and res.to_write() == [] and res.to_write(partial=True) == []


def test_an_import_of_an_unchanged_name_does_not_refuse(tmp_path):
    _repo(tmp_path, "pkg/__init__.py", "")
    _repo(tmp_path, "pkg/helpers.py", "def make():\n    return 1\n")
    _repo(
        tmp_path,
        "pkg/test_a.py",
        'import pytest\nfrom pkg.helpers import make\n\npytestmark = pytest.mark.rr("A", "B")\n\n\n'
        "def test_a():\n    assert make()\n",
    )
    res = tag_codemod.apply_tags({CaseKey("//pkg:t", "pkg.test_a::test_a"): "A"}, str(tmp_path))
    assert [f.path for f in res.to_write()] == ["pkg/test_a.py"] and not res.blocked


def test_rewritten_sources_are_rechecked_against_the_worksheet(tmp_path, monkeypatch, capsys):
    """Whatever produced a rewrite, the decided cases' attribution is
    re-derived from the rewritten sources; a difference writes nothing."""
    from rules_requirements import cli

    path = _repo(
        tmp_path,
        "pkg/test_a.py",
        'import pytest\n\npytestmark = pytest.mark.rr("A", "B")\n\n\ndef test_a():\n    pass\n',
    )
    before = path.read_bytes()
    monkeypatch.setattr(
        tag_codemod, "rewrite", lambda tf, owners, line_length=88: (tf.text.replace('"A", "B"', '"B"'), ["x"])
    )
    decided = {CaseKey("//pkg:t", "pkg.test_a::test_a"): "A"}
    res = tag_codemod.apply_tags(decided, str(tmp_path))
    assert [str(k) for k, _ in res.mismatched] == ["//pkg:t#pkg.test_a::test_a"]
    assert "gives B, the worksheet A" in res.mismatched[0][1]
    assert res.to_write() == [] and res.to_write(partial=True) == []
    sheet = tmp_path / "ws.rrplan"
    sheet.write_text(
        "schema: rules_requirements/attribution-worksheet/v1\ngroups:\n- target: //pkg:t\n  group: a\n"
        "  counts_toward: [A, B]\n  owner: A\n  cases:\n  - path: pkg.test_a::test_a\n",
        encoding="utf-8",
    )
    assert cli.main(["migrate", "apply", str(sheet), "--stage", "tags", "--root", str(tmp_path), "--partial"]) == 1
    assert "would not get their owner" in capsys.readouterr().err
    assert path.read_bytes() == before


# --------------------------------------------------------------------------- #
# Tests bound by an assignment or an import                                   #
# --------------------------------------------------------------------------- #

_HIDDEN = {
    "alias": (
        """
        import pytest

        pytestmark = pytest.mark.rr("A", "B")


        def test_a():
            pass


        test_b = test_a
        """,
        "test_b",
        "test_b is bound by an assignment",
    ),
    "import": (
        """
        import pytest

        from pkg.helpers import test_shared  # noqa: F401

        pytestmark = pytest.mark.rr("A", "B")


        def test_a():
            pass
        """,
        "test_shared",
        "test_shared is bound by an import",
    ),
    "class-attribute": (
        """
        import pytest


        class TestK:
            pytestmark = [pytest.mark.rr("A", "B")]

            def test_a(self):
                pass

            def helper(self):
                pass

            test_b = helper
        """,
        "TestK::test_b",
        "test_b is bound by an assignment",
    ),
    "aliased-decorator": (
        """
        import pytest


        @pytest.mark.rr("A", "B")
        def test_a():
            pass


        test_b = test_a
        """,
        "test_b",
        "test_b is bound by an assignment",
    ),
}

# Every other way of binding a test name in a scope (round 3): the scope's
# narrowed marker would reach each of these tests unseen.
_MODULE = """
import contextlib  # noqa: F401

import pytest

from pkg.helpers import helper  # noqa: F401

pytestmark = pytest.mark.rr("A", "B")


def test_a():
    pass


{post}
"""
_CLASS = """
import pytest


class TestK:
    pytestmark = [pytest.mark.rr("A", "B")]

    def test_a(self):
        pass

    def helper(self):
        pass

{body}
"""
for _shape, _post, _hidden, _message in [
    ("for", "for test_q in [test_a]:\n    pass\n", "test_q", "test_q is bound by a for loop"),
    ("with", "with contextlib.nullcontext(test_a) as test_w:\n    pass\n", "test_w", "test_w is bound by a with"),
    ("walrus", "if test_w := test_a:\n    pass\n", "test_w", "test_w is bound by an assignment expression"),
    ("globals", 'globals()["test_g"] = test_a\n', "test_g", "test_g is bound by an item assignment"),
    ("tuple", "test_b, test_c = test_a, test_a\n", "test_b", "test_b is bound by an assignment"),
    ("starred", "test_b, *rest = test_a, test_a\n", "test_b", "test_b is bound by an assignment"),
    ("annotated", "test_b: object = test_a\n", "test_b", "test_b is bound by an assignment"),
    ("import-as", "from pkg.helpers import helper as test_h  # noqa: E402\n", "test_h", "test_h is bound by an import"),
    ("star", "from pkg.helpers import *  # noqa: E402,F403\n", "test_shared", "a star import"),
    ("global", "def _make():\n    global test_g\n    test_g = test_a\n\n\n_make()\n", "test_g", "test_g is bound by"),
    ("exec", 'exec("def test_e():\\n    pass")\n', "test_e", "exec(), which may bind tests"),
    ("indirect", "x = test_a\ntest_b = x\n", "test_b", "test_b is bound by an assignment"),
]:
    _HIDDEN[_shape] = (_MODULE.format(post=_post), _hidden, _message)
# test_b aliases test_a through a variable the codemod cannot trace: test_a's own
# decorator is as unsafe to change as the scope's.
_HIDDEN["indirect-decorated"] = (
    'import pytest\n\n\n@pytest.mark.rr("A", "B")\ndef test_a():\n    pass\n\n\nx = test_a\ntest_b = x\n',
    "test_b",
    "test_b is bound by an assignment",
)
for _shape, _body, _hidden, _message in [
    ("class-for", "    for test_q in [helper]:\n        pass\n", "TestK::test_q", "test_q is bound by a for loop"),
    ("class-tuple", "    test_b, test_c = helper, helper\n", "TestK::test_b", "test_b is bound by an assignment"),
    ("class-import", "    from pkg.helpers import test_shared  # noqa\n", "TestK::test_shared", "bound by an import"),
    (
        "class-setattr",
        '\nsetattr(TestK, "test_s", TestK.helper)\n',
        "TestK::test_s",
        "test_s is bound by setattr()",
    ),
    ("class-attribute", "\nTestK.test_s = TestK.helper\n", "TestK::test_s", "bound by an attribute assignment"),
    ("class-alias", "\nTestAlias = TestK\n", "TestAlias::test_a", "TestAlias is bound by an assignment"),
]:
    _HIDDEN[_shape] = (_CLASS.format(body=_body), _hidden, _message)
# From inside a function that runs at import time (round 4): every way a
# function body can bind a name in the module or a class.
_RUN = "\n\n_install()\n"
_AT_RUN_TIME = "may bind tests the codemod cannot name, at run time"
for _shape, _post, _hidden, _message in [
    (
        "fn-attribute",
        "class TestI:\n    pass\n\n\ndef _install():\n    TestI.test_s = helper\n" + _RUN,
        "TestI::test_s",
        "test_s is bound by an attribute assignment, at run time",
    ),
    (
        "fn-setattr",
        'class TestI:\n    pass\n\n\ndef _install():\n    setattr(TestI, "test_s", helper)\n' + _RUN,
        "TestI::test_s",
        "test_s is bound by setattr(), at run time",
    ),
    (
        "fn-setattr-computed",
        'import sys  # noqa: E402\n\n\ndef _install(n="test_" + "d"):\n'
        "    setattr(sys.modules[__name__], n, helper)\n" + _RUN,
        "test_d",
        "setattr() with a computed name " + _AT_RUN_TIME,
    ),
    (
        "fn-setattr-alias",
        'class TestI:\n    pass\n\n\ndef _install():\n    s = setattr\n    s(TestI, "test_q", helper)\n' + _RUN,
        "TestI::test_q",
        "setattr " + _AT_RUN_TIME,
    ),
    (
        "fn-dunder-setattr",
        'class TestI:\n    pass\n\n\ndef _install():\n    type.__setattr__(TestI, "test_t", helper)\n' + _RUN,
        "TestI::test_t",
        "__setattr__() " + _AT_RUN_TIME,
    ),
    (
        "fn-classmethod",
        "class TestI:\n    @classmethod\n    def install(cls):\n        cls.test_c = helper\n\n\nTestI.install()\n",
        "TestI::test_c",
        "test_c is bound by an attribute assignment, at run time",
    ),
    (
        "fn-globals-item",
        'def _install():\n    globals()["test_g"] = helper\n' + _RUN,
        "test_g",
        "globals() " + _AT_RUN_TIME,
    ),
    ("fn-globals-update", "def _install():\n    globals().update(test_u=helper)\n" + _RUN, "test_u", "globals() "),
    (
        "fn-globals-alias",
        'def _install():\n    g = globals()\n    g["test_g"] = helper\n' + _RUN,
        "test_g",
        "globals() " + _AT_RUN_TIME,
    ),
    (
        "fn-exec",
        'def _install():\n    exec("test_e = helper", {"__builtins__": {}}, globals())\n' + _RUN,
        "test_e",
        _AT_RUN_TIME,
    ),
    (
        "fn-sys-modules",
        "def _install():\n    import sys\n\n    sys.modules[__name__].test_mm = helper\n" + _RUN,
        "test_mm",
        "test_mm is bound by an attribute assignment, at run time",
    ),
    (
        "fn-module-dict",
        'def _install():\n    import sys\n\n    sys.modules[__name__].__dict__["test_md"] = helper\n' + _RUN,
        "test_md",
        "sys.modules[__name__].__dict__ " + _AT_RUN_TIME,
    ),
    (
        "fn-vars",
        'def _install():\n    import sys\n\n    vars(sys.modules[__name__])["test_v"] = helper\n' + _RUN,
        "test_v",
        "vars() " + _AT_RUN_TIME,
    ),
    (
        "module-setattr-alias",
        'class TestI:\n    pass\n\n\n_s = setattr\n_s(TestI, "test_q", helper)\n',
        "TestI::test_q",
        "setattr, which may bind tests the codemod cannot name",
    ),
]:
    _HIDDEN[_shape] = (_MODULE.format(post=_post), _hidden, _message)


@pytest.mark.parametrize("shape", sorted(_HIDDEN))
def test_tests_bound_by_assignment_or_import_are_refused(tmp_path, shape):
    """Not in the worksheet (deselected, say), such a test still carries the
    scope's ids at runtime: narrowing the scope would change it unseen."""
    text, hidden, message = _HIDDEN[shape]
    _repo(tmp_path, "pkg/__init__.py", "")
    _repo(tmp_path, "pkg/helpers.py", "def test_shared():\n    pass\n\n\ndef helper(*args):\n    pass\n")
    path = _repo(tmp_path, "pkg/test_h.py", text)
    before = path.read_bytes()
    assert _runtime_traces(tmp_path, "pkg/test_h.py")[hidden] == ("A", "B")
    qual = "pkg.test_h.TestK::test_a" if "TestK" in text else "pkg.test_h::test_a"
    res = tag_codemod.apply_tags({CaseKey("//pkg:t", qual): "A"}, str(tmp_path))
    (f,) = res.refused
    assert f.path == "pkg/test_h.py" and message in f.reasons[0], f.reasons
    assert res.to_write(partial=True) == [] and path.read_bytes() == before


# --------------------------------------------------------------------------- #
# Matching, exit codes, layout and the backstops                              #
# --------------------------------------------------------------------------- #


def test_the_longest_module_match_names_the_case(tmp_path):
    """pkg/__init__.py (pkg) and pkg/test_x.py (pkg.test_x) both match the
    case path pkg.test_x::test_a; the longer module path is the case's."""
    _repo(tmp_path, "pkg/__init__.py", "")
    _repo(
        tmp_path,
        "pkg/test_x.py",
        'import pytest\n\npytestmark = pytest.mark.rr("A", "B")\n\n\ndef test_a():\n    pass\n',
    )
    res = tag_codemod.apply_tags({CaseKey("//pkg:t", "pkg.test_x::test_a"): "A"}, str(tmp_path))
    assert [f.path for f in res.changed] == ["pkg/test_x.py"] and res.unmatched == []


def test_unmatched_cases_alone_exit_1(tmp_path, capsys):
    from rules_requirements import cli

    _repo(tmp_path, "pkg/test_y.py", 'import pytest\n\npytestmark = pytest.mark.rr("A")\n\n\ndef test_a():\n    pass\n')
    sheet = tmp_path / "ws.rrplan"
    sheet.write_text(
        "schema: rules_requirements/attribution-worksheet/v1\ngroups:\n- target: //pkg:t\n  group: y\n"
        "  counts_toward: [A, B]\n  owner: A\n  cases:\n  - path: pkg.test_y::test_gone\n",
        encoding="utf-8",
    )
    rc = cli.main(["migrate", "apply", str(sheet), "--stage", "tags", "--root", str(tmp_path)])
    err = capsys.readouterr().err
    assert rc == 1 and "0 file(s) rewritten, 0 refused" in err and "pkg.test_y::test_gone" in err


def test_call_arguments_hug_on_one_indented_line():
    """Without black installed too: a declaration too long for one line, whose
    arguments fit on one indented line, hugs them there (as black does)."""
    text = 'import pytest\n\n\n@pytest.mark.rr("A-1", "B-2", level="hardware-in-the-loop")\ndef test_a():\n    pass\n'
    new, _ = rewrite(TestFile(text), {"test_a": "A-1"}, line_length=48)
    assert new == (
        'import pytest\n\n\n@pytest.mark.rr(\n    "A-1", level="hardware-in-the-loop"\n)\ndef test_a():\n    pass\n'
    )
    assert_black_stable(new, line_length=48)


def test_rewrite_refuses_a_test_of_a_distrusted_class():
    """rewrite() itself refuses a decided test whose class may inherit
    declarations, even when apply_tags' own screening is bypassed."""
    text = src(
        """
        import pytest
        from pkg.base import Base


        class TestImpl(Base):
            @pytest.mark.rr("A-1", "B-2")
            def test_own(self):
                pass
        """
    )
    assert rewrite(TestFile(text), {"TestImpl.test_own": "A-1"})[1]
    tf = TestFile(text)
    tf.mark_unseen(["TestImpl"], "TestImpl may inherit declarations")
    with pytest.raises(Unsupported, match="may inherit declarations"):
        rewrite(tf, {"TestImpl.test_own": "A-1"})


def test_rewrite_refuses_to_add_a_declaration_to_a_blind_test():
    text = 'import pytest\n\npytestmark = pytest.mark.rr("A-1", "B-2")\n\n\ndef test_a():\n    pass\n\n\ndef test_b():\n    pass\n'
    assert rewrite(TestFile(text), {"test_a": "A-1", "test_b": "B-2"})[1]
    tf = TestFile(text)
    tf.scope_blind[id(tf.find([], "test_a").node)] = "test_a is reached unseen"
    with pytest.raises(Unsupported, match="reached unseen"):
        rewrite(tf, {"test_a": "A-1", "test_b": "B-2"})


# --------------------------------------------------------------------------- #
# Fail closed: whatever the codemod cannot resolve is a refusal               #
# --------------------------------------------------------------------------- #

_PLAIN = 'import pytest\n\npytestmark = pytest.mark.rr("A", "B")\n\n\ndef test_a():\n    pass\n\n\n'

# Bindings of a test name that need no run to check (or cannot run).
_STATIC_SHAPES = {
    "except-as": "try:\n    raise ValueError\nexcept ValueError as test_e:\n    pass\n",
    "del": "test_x = 1\ndel test_x\n",
    "augmented": "test_b = []\ntest_b += [test_a]\n",
    "walrus-in-a-literal": "x = [(test_w := test_a)]\n",
    "walrus-in-a-default": "def helper(f=(test_w := test_a)):\n    pass\n",
    "computed-setattr": "import sys\n\nfor name in ['x']:\n    setattr(sys.modules[__name__], name, test_a)\n",
    "vars": 'vars()["test_v"] = test_a\n',
    "module-dict": 'import sys\n\nsys.modules[__name__].__dict__["test_d"] = test_a\n',
    "match": "match test_a:\n    case test_m:\n        pass\n",
    "class-walrus": "class TestK:\n    if (test_w := test_a):\n        pass\n",
    "class-decorator-walrus": "@(test_w := pytest.mark.slow)\nclass TestK:\n    pass\n",
    "class-keyword-walrus": "class TestK(metaclass=(test_m := type)):\n    pass\n",
}


@pytest.mark.parametrize("shape", sorted(_STATIC_SHAPES))
def test_every_other_binding_of_a_test_name_is_refused(shape):
    if shape == "match" and sys.version_info < (3, 10):
        pytest.skip("match needs Python 3.10")
    with pytest.raises(Unsupported, match="migrate this file by hand"):
        rewrite(TestFile(_PLAIN + _STATIC_SHAPES[shape]), {"test_a": "A"})


@pytest.mark.parametrize(
    "post",
    [
        "test_cases = [1, 2]\n",  # a literal is never a test
        "import pkg.helpers as test_mod  # noqa: E402\n",  # nor is a module
        "def check(x):\n    test_local = x\n    return test_local\n",  # a function's own variable
        'CONFIG = {}\nCONFIG["test_mode"] = True\n',  # a dict key
        # A method's own instance (pytest collects from the class, not an instance).
        "class Helper:\n    def __init__(self, name):\n        self.test_data = 1\n        setattr(self, name, 2)\n"
        '        setattr(self, "test_s", 3)\n        object.__setattr__(self, "test_f", 4)\n'
        '        super().__setattr__("test_y", 5)\n        self.__dict__.update(x=1)\n',
        # A closure still sees the method's instance.
        "class Helper:\n    def run(self):\n        def inner():\n            self.test_data = 1\n\n        inner()\n",
        # pytest's monkeypatch, at test time only.
        'def _patch(monkeypatch):\n    monkeypatch.setattr("os.sep", "/")\n',
        # A class that may be a TestCase, instantiated or read, is not aliased.
        "import unittest  # noqa: E402\n\n\nclass Base(unittest.TestCase):\n    LIMIT = 1\n\n\n"
        "CASE = Base()\nLIMIT = Base.LIMIT\n",
    ],
)
def test_bindings_that_cannot_hold_a_test_do_not_refuse(post):
    assert rewrite(TestFile(_PLAIN + post), {"test_a": "A"})[1] == ["test_a: A, B -> A"]


_OPAQUE = {
    "variable": ('AB = pytest.mark.rr("A", "B")\n\n\n@AB\ndef test_x():\n    pass\n', "the decorator @AB"),
    "attribute-alias": ('m = pytest.mark\n\n\n@m.rr("A", "B")\ndef test_x():\n    pass\n', "the decorator @m.rr"),
    "helper": (
        'def tagged(*ids):\n    return pytest.mark.rr(*ids)\n\n\n@tagged("A", "B")\ndef test_x():\n    pass\n',
        "the decorator @tagged",
    ),
    "pytestmark-element": (
        'AB = pytest.mark.rr("A", "B")\npytestmark = [pytest.mark.slow, AB]\n\n\ndef test_x():\n    pass\n',
        "the pytestmark element AB",
    ),
    "class": (
        'AB = pytest.mark.rr("A", "B")\n\n\n@AB\nclass TestK:\n    def test_x(self):\n        pass\n',
        "the decorator @AB",
    ),
    "param-marks": (
        'AB = pytest.mark.rr("A", "B")\n\n\n@pytest.mark.parametrize("n", [pytest.param(1, marks=AB)])\n'
        "def test_x(n):\n    pass\n",
        "the decorator @pytest.mark.parametrize",
    ),
    "augmented-pytestmark": (
        'AB = pytest.mark.rr("A", "B")\npytestmark = []\npytestmark += [AB]\n\n\ndef test_x():\n    pass\n',
        "pytestmark bound by an assignment",
    ),
    # An annotated pytestmark holding a literal is no data to exempt (round 4).
    "annotated-pytestmark": (
        'pytestmark: list = [pytest.mark.rr("A", "B")]\n\n\ndef test_x():\n    pass\n',
        "pytestmark bound by an assignment",
    ),
    "class-annotated-pytestmark": (
        'class TestK:\n    pytestmark: list = [pytest.mark.rr("A", "B")]\n\n    def test_x(self):\n        pass\n',
        "pytestmark bound by an assignment",
    ),
    "second-pytestmark": (
        'pytestmark = pytest.mark.rr("C")\npytestmark = pytest.mark.rr("A", "B")\n\n\ndef test_x():\n    pass\n',
        "a second pytestmark assignment in one scope",
    ),
    "with-args": (
        '@pytest.mark.rr.with_args("A", "B")\ndef test_x():\n    pass\n',
        "the decorator @pytest.mark.rr.with_args",
    ),
}


@pytest.mark.parametrize("shape", sorted(_OPAQUE))
def test_a_marker_the_codemod_cannot_read_is_refused_not_untagged(tmp_path, capsys, shape):
    """A decided test under a decorator or pytestmark element the codemod
    cannot read may carry ids at runtime it does not see: refuse (exit 1),
    never report it as 'declares no id' (exit 0)."""
    from rules_requirements import cli

    body, message = _OPAQUE[shape]
    path = _repo(tmp_path, "pkg/test_o.py", "import pytest\n\n" + body)
    before = path.read_bytes()
    runtime = _runtime_traces(tmp_path, "pkg/test_o.py")
    ((nodeid, ids),) = runtime.items()
    assert ids == ("A", "B")
    *classes, name = nodeid.split("::")
    sheet = tmp_path / "ws.rrplan"
    sheet.write_text(
        "schema: rules_requirements/attribution-worksheet/v1\ngroups:\n- target: //pkg:t\n  group: o\n"
        f"  counts_toward: [A, B]\n  owner: A\n  cases:\n  - path: {'.'.join(['pkg.test_o', *classes])}::{name}\n",
        encoding="utf-8",
    )
    rc = cli.main(["migrate", "apply", str(sheet), "--stage", "tags", "--root", str(tmp_path)])
    err = capsys.readouterr().err
    assert rc == 1 and "refused pkg/test_o.py" in err and message in err, err
    assert "declare no id" not in err and path.read_bytes() == before


def test_rewrite_itself_refuses_under_a_marker_it_cannot_read():
    """rewrite() refuses a decided test under an unreadable decorator even
    when apply_tags' own screening is bypassed."""
    text = 'import pytest\nfrom helpers import tagged\n\n\n@tagged("C")\n@pytest.mark.rr("A", "B")\ndef test_x():\n    pass\n'
    with pytest.raises(Unsupported, match="the decorator @tagged"):
        rewrite(TestFile(text), {"test_x": "A"})
    assert rewrite(TestFile(text.replace('@tagged("C")\n', "")), {"test_x": "A"})[1] == ["test_x: A, B -> A"]


def test_markers_the_codemod_can_read_do_not_refuse():
    text = src(
        """
        from unittest import mock

        import pytest


        class TestK:
            @pytest.mark.parametrize("n", [1, pytest.param(2, marks=[pytest.mark.slow])])
            @mock.patch("os.getcwd")
            @pytest.mark.rr("A", "B")
            def test_x(self, getcwd, n):
                pass

            @staticmethod
            @pytest.fixture
            def thing():
                return 1
        """
    )
    tf = TestFile(text)
    assert tf.opaque == {}
    assert rewrite(tf, {"TestK.test_x": "A"})[1] == ["TestK.test_x: A, B -> A"]


def _latin(tmp_path, rel, body):
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(("# -*- coding: latin-1 -*-\n# été\n" + body).encode("latin-1"))
    return path


def test_a_file_with_a_coding_cookie_is_indexed(tmp_path, capsys):
    """The verifier's repro: test_other.py is latin-1 (a PEP 263 cookie) and
    subclasses TestBase. It must be indexed like any other file: narrowing
    TestBase's marker would change its inherited tests."""
    from rules_requirements import cli

    _xmod(tmp_path)
    _latin(
        tmp_path, "pkg/test_other.py", "from pkg.test_base import TestBase\n\n\nclass TestSub(TestBase):\n    pass\n"
    )
    before = {rel: (tmp_path / rel).read_bytes() for rel in ("pkg/test_base.py", "pkg/test_other.py")}
    assert _runtime_traces(tmp_path, "pkg/test_other.py")["TestSub::test_x"] == ("A", "B")
    sheet = tmp_path / "ws.rrplan"
    sheet.write_text(
        "schema: rules_requirements/attribution-worksheet/v1\ngroups:\n- target: //pkg:t\n  group: base\n"
        "  counts_toward: [A, B]\n  owner: A\n  cases:\n  - path: pkg.test_base.TestBase::test_x\n",
        encoding="utf-8",
    )
    rc = cli.main(["migrate", "apply", str(sheet), "--stage", "tags", "--root", str(tmp_path), "--partial"])
    err = capsys.readouterr().err
    assert rc == 1 and "refused pkg/test_base.py" in err and "refused pkg/test_other.py" in err, err
    assert {rel: (tmp_path / rel).read_bytes() for rel in before} == before


def test_a_rewritten_file_keeps_its_encoding(tmp_path):
    from rules_requirements import cli

    path = _latin(
        tmp_path,
        "pkg/test_lat.py",
        'import pytest\n\npytestmark = pytest.mark.rr("A", "B")\n\n\ndef test_a():\n    pass\n',
    )
    sheet = tmp_path / "ws.rrplan"
    sheet.write_text(
        "schema: rules_requirements/attribution-worksheet/v1\ngroups:\n- target: //pkg:t\n  group: lat\n"
        "  counts_toward: [A, B]\n  owner: A\n  cases:\n  - path: pkg.test_lat::test_a\n",
        encoding="utf-8",
    )
    assert cli.main(["migrate", "apply", str(sheet), "--stage", "tags", "--root", str(tmp_path)]) == 0
    data = path.read_bytes()
    assert data.startswith(b"# -*- coding: latin-1 -*-\n# \xe9t\xe9\n") and b'pytestmark = pytest.mark.rr("A")' in data


def test_a_rewrite_its_encoding_cannot_hold_is_refused(tmp_path):
    """An id spelled with an escape (\\u0141) in a latin-1 file would be
    written as the character itself, which latin-1 cannot hold."""
    path = _latin(
        tmp_path,
        "pkg/test_lat.py",
        'import pytest\n\npytestmark = pytest.mark.rr("\\u0141-1", "B")\n\n\ndef test_a():\n    pass\n\n\n'
        "def test_b():\n    pass\n",
    )
    decided = {CaseKey("//pkg:t", "pkg.test_lat::test_a"): "\u0141-1", CaseKey("//pkg:t", "pkg.test_lat::test_b"): "B"}
    res = tag_codemod.apply_tags(decided, str(tmp_path))
    (f,) = res.refused
    assert "cannot be written in iso-8859-1" in f.reasons[0], f.reasons
    assert res.to_write(partial=True) == [] and b"\\u0141-1" in path.read_bytes()


@pytest.mark.parametrize("bad", [b"def broken(:\n    pass\n", b"x = '\xff'\n"])
def test_an_unreadable_file_anywhere_refuses_every_change(tmp_path, bad):
    """A file the codemod cannot read or parse (even outside --only) may
    import or subclass anything: when a declaration would change, it is
    refused by name, with the changed file."""
    path = _repo(tmp_path, "pkg/test_a.py", _PLAIN)
    (tmp_path / "pkg" / "other.py").write_bytes(bad)
    decided = {CaseKey("//pkg:t", "pkg.test_a::test_a"): "A"}
    res = tag_codemod.apply_tags(decided, str(tmp_path), only=["pkg/test_a.py"])
    refused = {f.path: f.reasons for f in res.refused}
    assert set(refused) == {"pkg/other.py", "pkg/test_a.py"}, res.files
    assert "the codemod cannot index this file" in refused["pkg/other.py"][0]
    assert "pkg/other.py cannot be read" in refused["pkg/test_a.py"][0]
    assert res.to_write() == [] and res.to_write(partial=True) == [] and res.changed == []
    assert path.read_text(encoding="utf-8") == _PLAIN
    # Nothing would change: nothing is refused.
    path.write_text(_PLAIN.replace('"A", "B"', '"A"'), encoding="utf-8")
    assert not tag_codemod.apply_tags(decided, str(tmp_path)).blocked


@pytest.mark.parametrize(
    "other",
    [
        "import importlib\n\nB = importlib.import_module('pkg.test_base').TestBase\n\n\nclass TestSub(B):\n",
        "B = __import__('pkg.test_base', fromlist=['x']).TestBase\n\n\nclass TestSub(B):\n",
        "import pkg.test_base as m\n\n\nclass TestSub(getattr(m, 'TestBase')):\n",
    ],
)
def test_a_base_class_the_index_cannot_resolve_may_be_any_class(tmp_path, capsys, other):
    """TestSub's base is reached dynamically: it may be TestBase (it is). Its
    file and every file whose classes would change are refused, and its
    decided test is refused rather than reported as declaring no id."""
    from rules_requirements import cli

    before = _xmod(tmp_path, other)
    runtime = _runtime_traces(tmp_path, "pkg/test_other.py")
    assert runtime == {"TestSub::test_x": ("A", "B"), "TestSub::test_y": ("A", "B")}
    sheet = tmp_path / "ws.rrplan"
    sheet.write_text(_XMOD_SHEET.replace("  - path: pkg.test_other.TestSub::test_x\n", ""), encoding="utf-8")
    argv = ["migrate", "apply", str(sheet), "--stage", "tags", "--root", str(tmp_path)]
    for extra in ([], ["--partial"]):
        rc = cli.main(argv + extra)
        err = capsys.readouterr().err
        assert rc == 1 and "refused pkg/test_base.py" in err and "refused pkg/test_other.py" in err, err
        assert "declare no id" not in err
        assert {rel: (tmp_path / rel).read_bytes() for rel in before} == before


def test_an_unresolved_base_out_of_reach_of_any_test_does_not_refuse(tmp_path):
    _repo(tmp_path, "pkg/__init__.py", "")
    _repo(tmp_path, "pkg/models.py", "Base = object\n\n\nclass User(Base):\n    pass\n")
    _repo(tmp_path, "pkg/test_a.py", _BASE)
    decided = {CaseKey("//pkg:t", "pkg.test_a.TestBase::test_x"): "A"}
    res = tag_codemod.apply_tags(decided, str(tmp_path))
    assert [f.path for f in res.to_write()] == ["pkg/test_a.py"] and not res.blocked
    # Once a test module imports it, it may be a TestCase collected there.
    _repo(tmp_path, "pkg/test_b.py", "from pkg.models import User  # noqa: F401\n")
    res = tag_codemod.apply_tags(decided, str(tmp_path))
    assert sorted(f.path for f in res.refused) == ["pkg/models.py", "pkg/test_a.py"], res.files
    assert "class User derives from Base" in res.refused[0].reasons[0]


def test_partial_holds_back_a_file_linked_to_a_refused_one(tmp_path, capsys):
    """test_g.py imports a class of test_r.py, which is refused (an undecided
    test). Nothing of test_r.py changes, so nothing refuses test_g.py, but
    --partial writes no file linked to a refused one, and says so."""
    from rules_requirements import cli

    _repo(tmp_path, "pkg/__init__.py", "")
    _repo(
        tmp_path,
        "pkg/test_r.py",
        'import pytest\n\n\nclass Helper:\n    pass\n\n\n@pytest.mark.rr("A", "B")\ndef test_r():\n    pass\n',
    )
    g = _repo(
        tmp_path,
        "pkg/test_g.py",
        'import pytest\n\nfrom pkg.test_r import Helper\n\npytestmark = pytest.mark.rr("A", "B")\n\n\n'
        "def test_g():\n    assert Helper\n",
    )
    before = g.read_bytes()
    decided = {CaseKey("//pkg:t", "pkg.test_g::test_g"): "A"}
    res = tag_codemod.apply_tags(decided, str(tmp_path))
    assert [f.path for f in res.refused] == ["pkg/test_r.py"] and [f.path for f in res.changed] == ["pkg/test_g.py"]
    assert res.depends["pkg/test_g.py"] == {"pkg/test_r.py"}
    assert res.to_write(partial=True) == []
    assert res.held_back(partial=True) == {"pkg/test_g.py": "it is linked to pkg/test_r.py (refused)"}
    sheet = tmp_path / "ws.rrplan"
    sheet.write_text(
        "schema: rules_requirements/attribution-worksheet/v1\ngroups:\n- target: //pkg:t\n  group: g\n"
        "  counts_toward: [A, B]\n  owner: A\n  cases:\n  - path: pkg.test_g::test_g\n",
        encoding="utf-8",
    )
    assert cli.main(["migrate", "apply", str(sheet), "--stage", "tags", "--root", str(tmp_path), "--partial"]) == 1
    err = capsys.readouterr().err
    assert "held back pkg/test_g.py (left unchanged: it is linked to pkg/test_r.py (refused))" in err, err
    assert g.read_bytes() == before


def test_a_plain_helper_import_does_not_link_files(tmp_path):
    """The verifier's p3: a refused file importing a plain helper function
    from a rewritten one does not hold the rewritten one back."""
    _repo(tmp_path, "pkg/__init__.py", "")
    _repo(tmp_path, "pkg/test_h.py", _PLAIN + "def helper():\n    return 1\n")
    _repo(
        tmp_path,
        "pkg/test_r.py",
        'import pytest\n\nfrom pkg.test_h import helper\n\n\n@pytest.mark.rr("A", "B")\ndef test_r():\n'
        "    assert helper()\n",
    )
    res = tag_codemod.apply_tags({CaseKey("//pkg:t", "pkg.test_h::test_a"): "A"}, str(tmp_path))
    assert [f.path for f in res.refused] == ["pkg/test_r.py"] and "pkg/test_h.py" not in res.depends
    assert [f.path for f in res.to_write(partial=True)] == ["pkg/test_h.py"]


def test_held_back_files_name_their_cause(tmp_path, capsys):
    """The verifier's p2: no file is refused, a decided case is not found.
    The messages say so; --partial holds back only the file holding it."""
    from rules_requirements import cli

    multi = '@pytest.mark.rr("A", "B")\ndef {}():\n    pass\n'
    u = _repo(tmp_path, "pkg/test_u.py", "import pytest\n\n\n" + multi.format("test_u"))
    _repo(tmp_path, "pkg/test_v.py", "import pytest\n\n\n" + multi.format("test_v"))
    before = u.read_bytes()
    sheet = tmp_path / "ws.rrplan"
    rows = [("pkg.test_u::test_u", "B"), ("pkg.test_v::test_v", "A"), ("pkg.test_u::test_ghost", "A")]
    sheet.write_text(
        "schema: rules_requirements/attribution-worksheet/v1\ngroups:\n"
        + "".join(
            f"- target: //pkg:t\n  group: g{i}\n  counts_toward: [A, B]\n  owner: {o}\n  cases:\n  - path: {p}\n"
            for i, (p, o) in enumerate(rows)
        ),
        encoding="utf-8",
    )
    argv = ["migrate", "apply", str(sheet), "--stage", "tags", "--root", str(tmp_path)]
    assert cli.main(argv) == 1
    err = capsys.readouterr().err
    assert "held back pkg/test_v.py (left unchanged: all or nothing: 1 decided case(s) were not found" in err
    assert "nothing written: 2 rewritten file(s) held back because 1 decided case(s) were not found" in err
    assert "refusals" not in err and "refused file" not in err
    assert cli.main([*argv, "--partial"]) == 1
    err = capsys.readouterr().err
    assert "rewrote pkg/test_v.py" in err
    assert "held back pkg/test_u.py (left unchanged: it holds a decided case the codemod did not find in it)" in err
    assert u.read_bytes() == before


@pytest.mark.parametrize(
    "other",
    [
        "import pkg.test_base as tb\n\npytestmark = tb.pytestmark\n",
        "import pkg.test_base\n\npytestmark = pkg.test_base.pytestmark\n",
        "from pkg import test_base\n\npytestmark = test_base.pytestmark\n",
    ],
)
def test_a_module_attribute_reference_links_the_files(tmp_path, other):
    """test_other.py reuses test_base.py's module pytestmark through the
    module object: narrowing it would change test_y unseen."""
    _repo(tmp_path, "pkg/__init__.py", "")
    _repo(tmp_path, "pkg/test_base.py", _PLAIN)
    _repo(tmp_path, "pkg/test_other.py", other + "\n\ndef test_y():\n    pass\n")
    assert _runtime_traces(tmp_path, "pkg/test_other.py")["test_y"] == ("A", "B")
    res = tag_codemod.apply_tags({CaseKey("//pkg:t", "pkg.test_base::test_a"): "A"}, str(tmp_path))
    assert sorted(f.path for f in res.refused) == ["pkg/test_base.py", "pkg/test_other.py"], res.files
    assert res.to_write(partial=True) == []


def test_a_re_exported_class_refuses_its_subclasses_too(tmp_path):
    """helpers.py re-exports TestBase as Base; test_other.py subclasses Base.
    The refusal propagates through the re-export to test_other.py."""
    _xmod(tmp_path, "from pkg.helpers import Base\n\n\nclass TestSub(Base):\n")
    _repo(tmp_path, "pkg/helpers.py", "from pkg.test_base import TestBase as Base  # noqa: F401\n")
    res = tag_codemod.apply_tags({CaseKey("//pkg:t", "pkg.test_base.TestBase::test_x"): "A"}, str(tmp_path))
    refused = {f.path: f.reasons for f in res.refused}
    assert sorted(refused) == ["pkg/helpers.py", "pkg/test_base.py", "pkg/test_other.py"], res.files
    assert any("imports or subclasses Base from pkg/helpers.py" in r for r in refused["pkg/test_other.py"])


def test_configured_test_names(tmp_path, monkeypatch):
    (tmp_path / "pytest.ini").write_text("[pytest]\npython_functions = check_*\npython_classes = Suite\n")
    names = tag_codemod.test_names(str(tmp_path))
    assert names.function("check_x") and names.function("test_x") and names.cls("SuiteA") and names.cls("TestA")
    assert not names.any("helper") and names.test_module("pkg/test_a.py")
    (tmp_path / "pytest.ini").unlink()
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\npython_functions = [\n  "verify_*",\n]\npython_files = "check_*.py"\n'
    )
    real = tag_codemod.importlib.import_module

    def no_tomllib(name, *args):
        if name == "tomllib":
            raise ImportError(name)
        return real(name, *args)

    for _ in range(2):  # with tomllib, then with the Python 3.9/3.10 fallback
        names = tag_codemod.test_names(str(tmp_path))
        assert names.function("verify_x") and names.test_module("pkg/check_x.py") and not names.function("check_x")
        monkeypatch.setattr(tag_codemod.importlib, "import_module", no_tomllib)


def test_configured_test_names_count_as_tests(tmp_path):
    """With python_functions = check_*, check_a is a test (its case is found)
    and check_b = check_a binds another one: the file is refused."""
    (tmp_path / "setup.cfg").write_text("[tool:pytest]\npython_functions = check_*\n")
    _repo(
        tmp_path,
        "pkg/test_c.py",
        'import pytest\n\npytestmark = pytest.mark.rr("A", "B")\n\n\ndef check_a():\n    pass\n\n\ncheck_b = check_a\n',
    )
    res = tag_codemod.apply_tags({CaseKey("//pkg:t", "pkg.test_c::check_a"): "A"}, str(tmp_path))
    assert res.unmatched == []
    (f,) = res.refused
    assert "check_b is bound by an assignment" in f.reasons[0], f.reasons


def test_a_test_case_class_bound_to_another_name_is_refused(tmp_path):
    """pytest collects a unittest.TestCase whatever name it is bound to:
    ``Alias = Checks`` collects Alias::test_x and Alias::test_y, which the
    class's narrowed marker would reach unseen."""
    text = (
        'import unittest\n\nimport pytest\n\n\n@pytest.mark.rr("A", "B")\nclass Checks(unittest.TestCase):\n'
        "    def test_x(self):\n        pass\n\n    def test_y(self):\n        pass\n\n\nAlias = Checks\n"
    )
    path = _repo(tmp_path, "pkg/test_u.py", text)
    assert _runtime_traces(tmp_path, "pkg/test_u.py")["Alias::test_x"] == ("A", "B")
    decided = {
        CaseKey("//pkg:t", "pkg.test_u.Checks::test_x"): "A",
        CaseKey("//pkg:t", "pkg.test_u.Checks::test_y"): "B",
    }
    res = tag_codemod.apply_tags(decided, str(tmp_path))
    (f,) = res.refused
    assert "Alias is bound by an assignment to Checks, which may be a unittest.TestCase" in f.reasons[0], f.reasons
    assert res.to_write(partial=True) == [] and path.read_text(encoding="utf-8") == src(text)
    # Without the alias, the same decisions are rewritten.
    path.write_text(src(text.replace("\n\n\nAlias = Checks\n", "\n")), encoding="utf-8")
    assert [f.path for f in tag_codemod.apply_tags(decided, str(tmp_path)).to_write()] == ["pkg/test_u.py"]


_UNITTEST_BASE = (
    'import unittest\n\nimport pytest\n\n\n@pytest.mark.rr("A", "B")\nclass BaseCase(unittest.TestCase):\n'
    "    def test_x(self):\n        pass\n\n    def test_y(self):\n        pass\n"
)
# BaseCase at run time (pytest imports test_base.py first), named nowhere.
_DYNAMIC_BASE = (
    'import unittest\n\nB = next(c for c in unittest.TestCase.__subclasses__() if c.__name__ == "BaseCase")\n\n\n'
)


@pytest.mark.parametrize(
    "files, unknown",
    [
        # Checks is not named like a test class, but sits in a test module.
        ({"pkg/test_other.py": _DYNAMIC_BASE + "class Checks(B):\n    pass\n"}, "pkg/test_other.py"),
        # Mid sits in a helper no test module imports, but Deeper subclasses it.
        (
            {
                "pkg/helpers.py": _DYNAMIC_BASE + "class Mid(B):\n    pass\n",
                "pkg/helpers2.py": "from pkg.helpers import Mid\n\n\nclass Deeper(Mid):\n    pass\n",
                "pkg/test_other.py": "from pkg.helpers2 import Deeper as Collected  # noqa: F401\n",
            },
            "pkg/helpers.py",
        ),
    ],
)
def test_an_unresolved_base_reached_by_a_test_refuses(tmp_path, files, unknown):
    """Each clause of _Index.counting_unknown: a class with an unresolved base
    in a test module, or one another class subclasses, may be BaseCase."""
    _repo(tmp_path, "pkg/__init__.py", "")
    _repo(tmp_path, "pkg/test_base.py", _UNITTEST_BASE)
    for rel, text in files.items():
        _repo(tmp_path, rel, text)
    runtime = _runtime_traces(tmp_path, "pkg")  # test_base.py, then test_other.py
    assert runtime.get("Checks::test_x", runtime.get("Collected::test_x")) == ("A", "B"), runtime
    decided = {
        CaseKey("//pkg:t", "pkg.test_base.BaseCase::test_x"): "A",
        CaseKey("//pkg:t", "pkg.test_base.BaseCase::test_y"): "B",
    }
    res = tag_codemod.apply_tags(decided, str(tmp_path))
    assert sorted(f.path for f in res.refused) == sorted(["pkg/test_base.py", unknown]), res.files
    assert res.to_write(partial=True) == []


@pytest.mark.parametrize(
    "head",
    [b"# -*- coding: nonexistent-enc -*-\n", b"def broken(:\n    pass\n\n\n"],
    ids=["unknown-coding", "syntax-error"],
)
def test_a_decided_case_in_a_file_that_cannot_be_read_is_refused(tmp_path, capsys, head):
    """The decided case's own file cannot be decoded (an unknown coding
    cookie) or parsed (syntax this interpreter does not know): refuse it by
    name and exit 1, never report the case as having no Python test."""
    from rules_requirements import cli

    body = b'import pytest\n\npytestmark = pytest.mark.rr("A", "B")\n\n\ndef test_a():\n    pass\n'
    bad = tmp_path / "pkg" / "test_m.py"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(head + body)
    ok = _repo(tmp_path, "pkg/test_ok.py", _PLAIN)
    before = {p: p.read_bytes() for p in (bad, ok)}
    sheet = tmp_path / "ws.rrplan"
    sheet.write_text(
        "schema: rules_requirements/attribution-worksheet/v1\ngroups:\n- target: //pkg:t\n  group: m\n"
        "  counts_toward: [A, B]\n  owner: A\n  cases:\n  - path: pkg.test_m::test_a\n  - path: pkg.test_ok::test_a\n",
        encoding="utf-8",
    )
    rc = cli.main(["migrate", "apply", str(sheet), "--stage", "tags", "--root", str(tmp_path), "--partial"])
    err = capsys.readouterr().err
    assert rc == 1 and "refused pkg/test_m.py" in err and "refused pkg/test_ok.py" in err, err
    assert "pkg.test_m::test_a was decided, but this file cannot be read or parsed (" in err, err
    assert "pkg/test_m.py cannot be read or parsed (" in err and "(cannot be read" not in err, err
    assert "no Python test to rewrite" not in err
    assert {p: p.read_bytes() for p in before} == before


@pytest.mark.parametrize(
    "files, message",
    [
        # A constant module name resolves like ``import pkg.test_base``.
        (
            {"pkg/test_other.py": 'import importlib\n\nB = importlib.import_module("pkg.test_base").BaseCase\n'},
            "imports or subclasses",
        ),
        ({"pkg/test_other.py": 'B = __import__("pkg.test_base", fromlist=["x"]).BaseCase\n'}, "imports or subclasses"),
        (
            {
                "pkg/test_other.py": 'import importlib\n\nB = importlib.import_module(".test_base", __package__).BaseCase\n'
            },
            "imports or subclasses",
        ),
        (
            {"pkg/test_other.py": 'B = __import__("test_base", globals(), None, ["x"], 1).BaseCase\n'},
            "imports or subclasses",
        ),
        (
            {"pkg/test_other.py": 'B = __import__("", globals(), None, ["test_base"], 1).test_base.BaseCase\n'},
            "imports or subclasses",
        ),
        # Anything else may import any file.
        (
            {"pkg/test_other.py": 'import importlib\n\nN = "pkg.test_base"\nB = importlib.import_module(N).BaseCase\n'},
            "importlib.import_module(N) imports a module the codemod cannot name",
        ),
        (
            {
                "pkg/loader.py": "import importlib\n\n\ndef load(n):\n    return importlib.import_module(n)\n",
                "pkg/test_other.py": 'from pkg.loader import load\n\nB = load("pkg.test_base").BaseCase\n',
            },
            "importlib.import_module(n) imports a module the codemod cannot name",
        ),
        (
            {"pkg/test_other.py": 'exec("from pkg.test_base import BaseCase as B")\n'},
            "imports a module the codemod cannot name",
        ),
        # A submodule reached as an attribute of its package.
        ({"pkg/test_other.py": "import pkg\n\nB = pkg.test_base.BaseCase\n"}, "imports or subclasses"),
        ({"pkg/test_other.py": "import pkg as p\n\nB = p.test_base.BaseCase\n"}, "imports or subclasses"),
        ({"pkg/test_other.py": 'B = __import__("pkg").test_base.BaseCase\n'}, "imports or subclasses"),
        (
            {"pkg/util.py": "", "pkg/test_other.py": 'B = __import__("pkg.util").test_base.BaseCase\n'},
            "imports or subclasses",
        ),
        ({"pkg/test_other.py": "import pkg\n\nB = getattr(pkg, 'test_base').BaseCase\n"}, "imports or subclasses"),
        (
            {"pkg/test_other.py": 'import importlib\n\nB = importlib.import_module("pkg").test_base.BaseCase\n'},
            "imports or subclasses",
        ),
    ],
)
def test_a_test_case_imported_by_a_call_is_refused(tmp_path, files, message):
    """pkg/test_other.py collects BaseCase again as B (a unittest.TestCase is
    collected whatever its name), reached through an import call or as an
    attribute of its package: narrowing BaseCase's marker would change B's
    tests unseen."""
    _repo(tmp_path, "pkg/__init__.py", "")
    _repo(tmp_path, "pkg/test_base.py", _UNITTEST_BASE)
    for rel, text in files.items():
        _repo(tmp_path, rel, text)
    assert _runtime_traces(tmp_path, "pkg")["B::test_x"] == ("A", "B")  # test_base.py, then test_other.py
    decided = {
        CaseKey("//pkg:t", "pkg.test_base.BaseCase::test_x"): "A",
        CaseKey("//pkg:t", "pkg.test_base.BaseCase::test_y"): "B",
    }
    res = tag_codemod.apply_tags(decided, str(tmp_path))
    refused = {f.path: f.reasons for f in res.refused}
    assert "pkg/test_base.py" in refused and set(refused) & set(files), res.files
    assert any(message in r for rs in refused.values() for r in rs), refused
    assert res.to_write(partial=True) == []


def test_an_import_call_out_of_the_tree_does_not_refuse(tmp_path):
    _repo(tmp_path, "pkg/__init__.py", "")
    _repo(tmp_path, "pkg/test_base.py", _UNITTEST_BASE)
    _repo(
        tmp_path,
        "pkg/test_other.py",
        'import importlib\n\n\ndef test_json():\n    assert importlib.import_module("json")\n',
    )
    decided = {
        CaseKey("//pkg:t", "pkg.test_base.BaseCase::test_x"): "A",
        CaseKey("//pkg:t", "pkg.test_base.BaseCase::test_y"): "B",
    }
    res = tag_codemod.apply_tags(decided, str(tmp_path))
    assert [f.path for f in res.to_write()] == ["pkg/test_base.py"] and not res.blocked


def test_only_pytest_test_modules_and_the_named_source_resolve(tmp_path):
    """A C++ case whose path reads like a Python module never resolves to a
    helper module (tools/codec.py) or to a non-test module that happens to
    define a matching function; a case whose evidence names a non-Python
    source, or a record, is "not a Python test"."""
    files = {
        "app/tests/test_config.py": 'import pytest\n\n\n@pytest.mark.rr("REQ-1", "REQ-2")\ndef test_load():\n    pass\n',
        "tools/codec.py": "def encode():\n    pass\n",
        "pylib/codec.py": 'import pytest\n\n\n@pytest.mark.rr("REQ-1", "REQ-2")\ndef test_round_trip():\n    pass\n',
    }
    for rel, body in files.items():
        (tmp_path / os.path.dirname(rel)).mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(body, encoding="utf-8")
    py = CaseKey("//app/tests:t", "app.tests.test_config::test_load")
    cc = CaseKey("//fw:codec_test", "codec::round_trip")
    cc2 = CaseKey("//fw:codec_test", "codec::test_round_trip")
    rec = CaseKey("record:bench", "test_config::test_load")
    named = CaseKey("//fw:codec2_test", "test_config::test_load")
    decided = {py: "REQ-1", cc: "REQ-2", cc2: "REQ-2", rec: "REQ-2", named: "REQ-2"}
    res = tag_codemod.apply_tags(decided, str(tmp_path), files_of={named: "fw/codec_test.cc"})
    unresolved = dict(res.unresolved)
    assert "no Python source of this module" in unresolved[cc]
    assert "no Python source of this module" in unresolved[cc2]
    assert unresolved[rec] == "not a Python test (a record)"
    assert unresolved[named] == "not a Python test (its evidence names fw/codec_test.cc)"
    assert res.unmatched == [] and res.unmatched_files == set()
    assert [f.path for f in res.changed] == ["app/tests/test_config.py"]
    assert traces(res.changed[0].new_text)["test_load"][0] == ("REQ-1",)
    # A .py source named by the evidence narrows the candidates to that file.
    other = CaseKey("//app/tests:t2", "test_config::test_load")
    res = tag_codemod.apply_tags({other: "REQ-2"}, str(tmp_path), files_of={other: "app/other/test_config.py"})
    assert "app/other/test_config.py, which is not a scanned pytest test module" in dict(res.unresolved)[other]
    assert res.changed == []


# The shape of a script-style test module (splanc's improv_codec_test.py): an
# import call inside a helper that only main() calls, and main() called only
# from the ``if __name__ == "__main__":`` block. pytest imports the module
# under its module name, so none of that runs at collection -- unless a name
# computed at run time reaches it, which static analysis cannot rule out. So
# by default it is judged like any other code (refused, as in v0.2.0), and
# only trust_main_guard (--trust-main-guard) leaves it out, best-effort.
_SCRIPT_TEST = (
    '"""Run: python3 pkg/codec_test.py"""\n\n'
    "import importlib.util\nimport pathlib\nimport sys\n\n"
    "HERE = pathlib.Path(__file__).resolve().parent\n\n\n"
    "def _load_onboard():\n"
    '    path = HERE.parents[1] / "tools" / "onboard.py"\n'
    '    spec = importlib.util.spec_from_file_location("onboard", path)\n'
    "    mod = importlib.util.module_from_spec(spec)\n"
    "    spec.loader.exec_module(mod)\n"
    "    return mod\n\n\n"
    "def main() -> int:\n"
    "    ob = _load_onboard()\n"
    "    assert ob is not None\n"
    "    return 0\n\n\n"
    "GUARD\n"
    "    sys.exit(main())\n"
)
_SCRIPT_DECIDED = {
    CaseKey("//pkg:t", "pkg.test_base.BaseCase::test_x"): "A",
    CaseKey("//pkg:t", "pkg.test_base.BaseCase::test_y"): "A",
}


def _script_repo(tmp_path, script, others=None, trust_main_guard=False):
    _repo(tmp_path, "pkg/__init__.py", "")
    _repo(tmp_path, "pkg/test_base.py", _UNITTEST_BASE)
    _repo(tmp_path, "pkg/codec_test.py", script)
    for rel, text in (others or {}).items():
        _repo(tmp_path, rel, text)
    return tag_codemod.apply_tags(_SCRIPT_DECIDED, str(tmp_path), trust_main_guard=trust_main_guard)


def _assert_script_refused(res):
    refused = {f.path: f.reasons for f in res.refused}
    assert "pkg/codec_test.py" in refused and "pkg/test_base.py" in refused, res.files
    assert any("spec_from_file_location" in r for r in refused["pkg/codec_test.py"]), refused
    assert res.to_write(partial=True) == []


@pytest.mark.parametrize(
    "guard",
    ['if __name__ == "__main__":', 'if "__main__" == __name__:', "if __name__ == '__main__':"],
    ids=["name-first", "main-first", "single-quotes"],
)
def test_an_import_call_only_a_main_block_reaches_refuses_by_default(tmp_path, guard):
    """By default the static guards judge a __main__ block and what only it
    reaches like any other code (as v0.2.0 did): fail closed."""
    _assert_script_refused(_script_repo(tmp_path, _SCRIPT_TEST.replace("GUARD", guard)))


@pytest.mark.parametrize(
    "guard",
    ['if __name__ == "__main__":', 'if "__main__" == __name__:', "if __name__ == '__main__':"],
    ids=["name-first", "main-first", "single-quotes"],
)
def test_an_import_call_only_a_main_block_reaches_applies_with_trust_main_guard(tmp_path, guard):
    """With trust_main_guard the static guards leave out the __main__ block
    and the functions only it reaches, which never run under pytest."""
    res = _script_repo(tmp_path, _SCRIPT_TEST.replace("GUARD", guard), trust_main_guard=True)
    assert [f.path for f in res.to_write()] == ["pkg/test_base.py"] and not res.blocked, res.files
    assert "pkg/codec_test.py" not in {f.path for f in res.refused}


@pytest.mark.parametrize(
    "script, others",
    [
        # main() also runs at import time, so _load_onboard does too.
        (_SCRIPT_TEST.replace("GUARD", 'main()\n\nif __name__ == "__main__":'), {}),
        # The else branch of the guard runs under pytest.
        (_SCRIPT_TEST.replace("GUARD", 'if __name__ == "__main__":\n    pass\nelse:'), {}),
        # A module-level reference that may call it (by name, attribute or string).
        (_SCRIPT_TEST.replace("GUARD", 'ENTRY = main\n\nif __name__ == "__main__":'), {}),
        (_SCRIPT_TEST.replace("GUARD", 'ENTRY = globals()["_load_onboard"]\n\nif __name__ == "__main__":'), {}),
        # A test reaches the helper too.
        (
            _SCRIPT_TEST.replace("GUARD", 'def test_onboard():\n    _load_onboard()\n\n\nif __name__ == "__main__":'),
            {},
        ),
        # A default argument is evaluated when the def is reached.
        (_SCRIPT_TEST.replace("GUARD", 'def helper(x=main()):\n    pass\n\n\nif __name__ == "__main__":'), {}),
        # Not a __main__ guard.
        (_SCRIPT_TEST.replace("GUARD", 'if __name__ != "__main__":'), {}),
        # Another scanned file takes main from it, and may call it at its import.
        (
            _SCRIPT_TEST.replace("GUARD", 'if __name__ == "__main__":'),
            {"pkg/test_other.py": "from pkg.codec_test import main\n\nmain()\n"},
        ),
        (
            _SCRIPT_TEST.replace("GUARD", 'if __name__ == "__main__":'),
            {"pkg/test_other.py": "import pkg.codec_test as c\n\nc.main()\n"},
        ),
        # __name__ rebound: the guard itself may run at import.
        (_SCRIPT_TEST.replace("GUARD", '__name__ = "__main__"\n\nif __name__ == "__main__":'), {}),
        (_SCRIPT_TEST.replace("GUARD", 'globals()["__name__"] = "__main__"\n\nif __name__ == "__main__":'), {}),
        # A lookup by a computed name at import time may reach any function.
        (_SCRIPT_TEST.replace("GUARD", 'globals()["ma" + "in"]()\n\nif __name__ == "__main__":'), {}),
        (
            _SCRIPT_TEST.replace(
                "GUARD",
                "for _n, _f in list(globals().items()):\n"
                '    if _n.startswith("_lo"):\n'
                "        _f()\n\n"
                'if __name__ == "__main__":',
            ),
            {},
        ),
        (_SCRIPT_TEST.replace("GUARD", 'eval("ma" + "in")()\n\nif __name__ == "__main__":'), {}),
        (
            _SCRIPT_TEST.replace(
                "GUARD", 'getattr(sys.modules[__name__], "ma" + "in")()\n\nif __name__ == "__main__":'
            ),
            {},
        ),
        (
            _SCRIPT_TEST.replace(
                "GUARD",
                "import inspect\n\n"
                "for _n, _f in inspect.getmembers(sys.modules[__name__], inspect.isfunction):\n"
                '    if _n.startswith("_lo"):\n'
                "        _f()\n\n"
                'if __name__ == "__main__":',
            ),
            {},
        ),
        (_SCRIPT_TEST.replace("GUARD", 'sys._getframe(0).f_globals["ma" + "in"]()\n\nif __name__ == "__main__":'), {}),
        # Another file reaches it without importing it: sys.modules, a
        # pytest item's module (by name or by a computed name), a
        # pytest_pycollect_makeitem hook handed every object of the module.
        (
            _SCRIPT_TEST.replace("GUARD", 'if __name__ == "__main__":'),
            {"pkg/test_other.py": "import sys\n\nsys.modules['pkg.codec_test'].main()\n"},
        ),
        (
            _SCRIPT_TEST.replace("GUARD", 'if __name__ == "__main__":'),
            {
                "pkg/conftest.py": "import sys\n\n\ndef pytest_collection_modifyitems(items):\n"
                "    sys.modules['pkg.codec_test'].main()\n"
            },
        ),
        (
            _SCRIPT_TEST.replace("GUARD", 'if __name__ == "__main__":'),
            {
                "pkg/conftest.py": "def pytest_collection_modifyitems(items):\n"
                "    for it in items:\n"
                "        getattr(it.module, 'main')()\n"
            },
        ),
        (
            _SCRIPT_TEST.replace("GUARD", 'if __name__ == "__main__":'),
            {
                "pkg/conftest.py": "def pytest_collection_modifyitems(items):\n"
                "    for it in items:\n"
                "        getattr(it.module, 'ma' + 'in')()\n"
            },
        ),
        (
            _SCRIPT_TEST.replace("GUARD", 'if __name__ == "__main__":'),
            {
                "pkg/conftest.py": "import sys\n\n\ndef pytest_collection_modifyitems(items):\n"
                "    for name, mod in list(sys.modules.items()):\n"
                "        if name.endswith('_test'):\n"
                "            mod.main()\n"
            },
        ),
        (
            _SCRIPT_TEST.replace("GUARD", 'if __name__ == "__main__":'),
            {
                "pkg/conftest.py": "def pytest_pycollect_makeitem(collector, name, obj):\n"
                "    if name.startswith('_lo'):\n"
                "        obj()\n"
            },
        ),
        # The block launches the tests in-process (a py_test whose main is the
        # file): what runs before the launch runs before collection.
        (
            _SCRIPT_TEST.replace(
                "GUARD", 'if __name__ == "__main__":\n    main()\n    pytest.main([__file__])'
            ).replace("import sys\n", "import sys\n\nimport pytest\n"),
            {},
        ),
        (
            _SCRIPT_TEST.replace(
                "GUARD", 'if __name__ == "__main__":\n    _load_onboard()\n    unittest.main()'
            ).replace("import sys\n", "import sys\nimport unittest\n"),
            {},
        ),
    ],
    ids=[
        "main-called-at-module-level",
        "else-branch",
        "aliased",
        "globals-string",
        "called-by-a-test",
        "default-argument",
        "not-main",
        "imported-by-another-file",
        "module-passed-around",
        "name-rebound",
        "name-rebound-via-globals",
        "globals-computed-name",
        "globals-iterated",
        "eval",
        "getattr-computed-name",
        "inspect-getmembers",
        "frame-globals",
        "other-file-sys-modules",
        "conftest-sys-modules",
        "conftest-item-module",
        "conftest-item-module-computed",
        "conftest-sys-modules-iterated",
        "conftest-pycollect-makeitem",
        "pytest-main-launcher",
        "unittest-main-launcher",
    ],
)
@pytest.mark.parametrize("trust_main_guard", [False, True], ids=["default", "trust-main-guard"])
def test_an_import_call_also_reached_at_import_time_still_refuses(tmp_path, script, others, trust_main_guard):
    """Refused by default, and the hardened exclusion still refuses each
    with trust_main_guard."""
    _assert_script_refused(_script_repo(tmp_path, script, others, trust_main_guard))


_G = 'if __name__ == "__main__":'


# Shapes that run main() (or the guard's body) at import time and that the
# best-effort exclusion does NOT see: refused only because the exclusion is
# off by default. trust_main_guard lets them through, which is why it needs
# a definitive check after it.
@pytest.mark.parametrize(
    "guard, others",
    [
        ('import builtins\n\nbuiltins.globals()["ma" + "in"]()\n\n' + _G, {}),
        ('import builtins\n\nbuiltins.eval("ma" + "in")()\n\n' + _G, {}),
        ('__builtins__["ev" + "al"]("ma" + "in")()\n\n' + _G, {}),
        ('import pkg.codec_test as _me\n\n_me.__getattribute__("ma" + "in")()\n\n' + _G, {}),
        ('__import__("pkg.codec_test", fromlist=["x"]).__getattribute__("ma" + "in")()\n\n' + _G, {}),
        ('object.__getattribute__(sys.modules.get("pkg.codec_test"), "ma" + "in")()\n\n' + _G, {}),
        (_G, {"pkg/test_other.py": "import pkg.codec_test as c\n\nc.__getattribute__('ma' + 'in')()\n"}),
        (_G, {"pkg/test_other.py": "import pkg.codec_test as c\n\nc.__dict__['ma' + 'in']()\n"}),
        (
            _G,
            {
                "pkg/conftest.py": "def pytest_collection_modifyitems(items):\n"
                "    for it in items:\n"
                "        it.module.__getattribute__('ma' + 'in')()\n"
            },
        ),
        (_G, {"pkg/test_other.py": "import runpy\n\nrunpy.run_path('pkg/codec_test.py', run_name='__main__')\n"}),
        (_G, {"pkg/test_other.py": "import runpy\n\nrunpy.run_module('pkg.codec_test', run_name='__main__')\n"}),
        (
            _G,
            {
                "pkg/test_other.py": "import importlib.util\n\n"
                "_s = importlib.util.spec_from_file_location('__main__', 'pkg/codec_test.py')\n"
                "_m = importlib.util.module_from_spec(_s)\n"
                "_s.loader.exec_module(_m)\n"
            },
        ),
    ],
    ids=[
        "builtins-globals",
        "builtins-eval",
        "dunder-builtins-eval",
        "self-import-getattribute",
        "dunder-import-getattribute",
        "object-getattribute",
        "other-getattribute",
        "other-dict",
        "conftest-hook-getattribute",
        "other-runpy-path",
        "other-runpy-module",
        "other-spec-main",
    ],
)
def test_a_main_only_function_reached_by_a_computed_name_refuses_by_default(tmp_path, guard, others):
    _assert_script_refused(_script_repo(tmp_path, _SCRIPT_TEST.replace("GUARD", guard), others))


@pytest.mark.parametrize(
    "guard, others",
    [
        # sys.modules tested or written, not read, at import time.
        ('if "bleak" not in sys.modules:\n    sys.modules["bleak"] = sys\n\nif __name__ == "__main__":', {}),
        # Another file reads a module of sys.modules by a literal name: not this one.
        ('if __name__ == "__main__":', {"pkg/test_other.py": "import sys\n\nE = sys.modules['bleak.exc'].E\n"}),
        # Another file names a "main" without a handle on this module.
        (
            'if __name__ == "__main__":',
            {"pkg/test_other.py": "BRANCH = 'main'\n\n\ndef test_x(app):\n    app.main()\n"},
        ),
        # The guard's body is not a launcher: unittest.main elsewhere is not run here.
        (
            'if __name__ == "__main__":',
            {"pkg/test_other.py": "import unittest\n\nif __name__ == '__main__':\n    unittest.main()\n"},
        ),
    ],
    ids=["sys-modules-written", "other-reads-another-module", "other-names-main", "other-launcher"],
)
def test_what_cannot_reach_a_main_only_function_does_not_refuse(tmp_path, guard, others):
    """With trust_main_guard only; by default the __main__ code is judged."""
    res = _script_repo(tmp_path, _SCRIPT_TEST.replace("GUARD", guard), others, trust_main_guard=True)
    assert "pkg/codec_test.py" not in {f.path for f in res.refused}, res.files
    assert "pkg/test_base.py" in [f.path for f in res.to_write()] and not res.blocked, res.files
    _assert_script_refused(_script_repo(tmp_path, _SCRIPT_TEST.replace("GUARD", guard), others))


def test_bindings_a_main_block_makes_refuse_by_default():
    """By default a test name bound in a __main__ block, or by a function
    only it calls, is judged like any other binding."""
    head = 'import sys\n\nimport pytest\n\npytestmark = pytest.mark.rr("A", "B")\n\n\ndef test_a():\n    pass\n\n\n'
    helper = 'def install():\n    setattr(sys.modules[__name__], "test_z", test_a)\n\n\n'
    main = 'if __name__ == "__main__":\n    globals()["test_y"] = test_a\n    install()\n'
    blind = " ".join(TestFile(head + helper + main).scope_blind.values())
    assert "test_z is bound by setattr()" in blind or "test_y is bound by an item assignment" in blind, blind


def test_bindings_a_main_block_makes_do_not_refuse_with_trust_main_guard():
    """With trust_main_guard, a test name bound in a __main__ block, or by a
    function only it calls, is never bound under pytest; the same binding at
    import time still is."""
    head = 'import sys\n\nimport pytest\n\npytestmark = pytest.mark.rr("A", "B")\n\n\ndef test_a():\n    pass\n\n\n'
    helper = 'def install():\n    setattr(sys.modules[__name__], "test_z", test_a)\n\n\n'
    main = 'if __name__ == "__main__":\n    globals()["test_y"] = test_a\n    install()\n'
    tf = TestFile(head + helper + main, trust_main_guard=True)
    assert not tf.scope_blind and not tf.problems, tf.scope_blind
    # Called at import time, install reads sys.modules there: nothing is
    # excluded, the __main__ block's binding included (fail closed).
    tf = TestFile(head + helper + "install()\n\n" + main, trust_main_guard=True)
    blind = " ".join(tf.scope_blind.values())
    assert "test_z is bound by setattr()" in blind or "test_y is bound by an item assignment" in blind, blind
    tf = TestFile(
        head + helper + 'if __name__ == "__main__":\n    pass\nelse:\n    globals()["test_y"] = test_a\n',
        trust_main_guard=True,
    )
    assert any("test_y is bound by an item assignment" in why for why in tf.scope_blind.values()), tf.scope_blind
    # Taken by another file (which may call it at its own import): judged.
    tf = TestFile(head + helper + main, reached=["install"], trust_main_guard=True)
    blind = " ".join(tf.scope_blind.values())
    assert "test_z is bound by setattr()" in blind or "test_y is bound by an item assignment" in blind, blind
