# SPDX-License-Identifier: AGPL-3.0-or-later
"""The dynamic collection check of ``rr migrate apply --stage tags``.

Static analysis cannot prove what pytest collects. These tests drive the real
``pytest --collect-only`` check: dynamic rewrites that would misattribute a
test refuse by default and name the item; code in test/fixture/setup bodies
(which runs after collection) applies cleanly; a collection that fails refuses
unless ``--no-collect-check`` is passed.
"""

import sys
import textwrap

import pytest

from rules_requirements import cli, collect_check
from rules_requirements.case_keys import CaseKey, nodeid_to_case_path
from rules_requirements.hooks import collect_dump, pytest_plugin

PY = sys.executable  # the interpreter running the suite has pytest


def run(capsys, *argv):
    rc = cli.main(list(argv))
    out, err = capsys.readouterr()
    return rc, out, err


def _tree(root, files):
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")


def _ws(path, cases):
    lines = ["schema: rules_requirements/attribution-worksheet/v1", "groups:"]
    for i, (case_path, owner) in enumerate(cases):
        lines += [
            "- target: //pkg:t",
            f"  group: g{i}",
            "  counts_toward: [A, B]",
            f"  owner: {owner}",
            "  cases:",
            f"  - path: {case_path}",
        ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _apply(capsys, tmp_path, *extra):
    sheet = tmp_path / "ws.rrplan"
    return run(
        capsys, "migrate", "apply", str(sheet), "--stage", "tags", "--root", str(tmp_path), "--python", PY, *extra
    )


# --------------------------------------------------------------------------- #
# Dynamic rewrites the static guards cannot see: refuse by default, name the  #
# item whose ids would change (round-3/round-4 repros that exited 0 wrong).   #
# --------------------------------------------------------------------------- #

_H = (
    'import pytest\n\npytestmark = pytest.mark.rr("A", "B")\n\n\n'
    "def test_a():\n    assert True\n\n\ndef helper():\n    assert True\n\n\n"
)
_U = (
    "import unittest\n\nimport pytest\n\n\n"
    '@pytest.mark.rr("A", "B")\nclass Checks(unittest.TestCase):\n'
    "    def test_x(self):\n        pass\n\n    def test_y(self):\n        pass\n\n\n"
)
_F = 'import pytest\n\n\n@pytest.mark.rr("A", "B")\ndef test_a():\n    pass\n\n\n'

_DYNAMIC = {
    "factory": (
        {
            "pkg/test_m.py": _U
            + "def make(n):\n    class _C(Checks):\n        N = n\n\n    return _C\n\n\nCase1 = make(1)\n"
        },
        [("pkg.test_m.Checks::test_x", "A"), ("pkg.test_m.Checks::test_y", "B")],
        "Case1::test_x",
    ),
    "lambda": (
        {"pkg/test_m.py": _F + "test_b = (lambda: test_a)()\n"},
        [("pkg.test_m::test_a", "A")],
        "test_m.py::test_b",
    ),
    "init_subclass": (
        {
            "pkg/test_m.py": _H
            + "class Base:\n    def __init_subclass__(cls, **kw):\n        cls.test_s = helper\n\n\nclass TestK(Base):\n    pass\n"
        },
        [("pkg.test_m::test_a", "A")],
        "TestK::test_s",
    ),
    "metaclass": (
        {
            "pkg/test_m.py": _H
            # (no super().__init__ call: that reference alone makes the static
            # guards judge __init__, and this test is about the dynamic check)
            + "class Meta(type):\n    def __init__(cls, name, bases, ns):\n"
            "        cls.test_s = helper\n\n\nclass TestK(metaclass=Meta):\n    pass\n"
        },
        [("pkg.test_m::test_a", "A")],
        "TestK::test_s",
    ),
    "builtins_setattr": (
        {
            "pkg/test_m.py": "import builtins\n"
            + _H
            + 'class TestK:\n    pass\n\n\nbuiltins.setattr(TestK, "test_s", helper)\n'
        },
        [("pkg.test_m::test_a", "A")],
        "TestK::test_s",
    ),
    "builtins_exec": (
        {"pkg/test_m.py": "import builtins\n" + _H + 'builtins.exec("test_e = helper")\n'},
        [("pkg.test_m::test_a", "A")],
        "test_m.py::test_e",
    ),
}


@pytest.mark.parametrize("shape", sorted(_DYNAMIC))
def test_dynamic_rewrites_refuse_by_default(capsys, tmp_path, shape):
    files, cases, offending = _DYNAMIC[shape]
    files = {"pkg/__init__.py": "", **files}
    _tree(tmp_path, files)
    before = (tmp_path / "pkg/test_m.py").read_bytes()
    _ws(tmp_path / "ws.rrplan", cases)
    rc, _, err = _apply(capsys, tmp_path)
    assert rc == 1
    assert (tmp_path / "pkg/test_m.py").read_bytes() == before  # nothing written
    assert "collection check refused" in err
    assert offending in err  # the message names the item whose ids would change


def test_import_call_alias_writes_nothing(capsys, tmp_path):
    """A class reached across modules by an ``importlib.import_module`` call:
    the rewrite writes nothing (refused before anything is written)."""
    _tree(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/test_base.py": _U.replace("Checks", "BaseCase"),
            "pkg/test_other.py": "from importlib import import_module\n\nAlias = import_module"
            '("pkg.test_base").BaseCase\n',
        },
    )
    before = (tmp_path / "pkg/test_base.py").read_bytes()
    _ws(tmp_path / "ws.rrplan", [("pkg.test_base.BaseCase::test_x", "A"), ("pkg.test_base.BaseCase::test_y", "B")])
    rc, _, err = _apply(capsys, tmp_path)
    assert rc == 1 and (tmp_path / "pkg/test_base.py").read_bytes() == before


# --------------------------------------------------------------------------- #
# Code in bodies that run after collection applies cleanly (the check passes). #
# --------------------------------------------------------------------------- #

_P = (
    'import pytest\n\npytestmark = pytest.mark.rr("A", "B")\n\n\n'
    "def test_a():\n    assert True\n\n\ndef test_b():\n{body}\n"
)
_HARMLESS = {
    "eval": "    assert eval(repr(1)) == 1",
    "vars": "    import argparse\n\n    assert vars(argparse.Namespace(x=1)) == {'x': 1}",
    "local_setattr": "    class O:\n        pass\n\n    o = O()\n    for k, v in {'a': 1}.items():\n"
    "        setattr(o, k, v)\n    assert o.a == 1",
}


@pytest.mark.parametrize("shape", sorted(_HARMLESS))
def test_harmless_test_body_bindings_apply(capsys, tmp_path, shape):
    _tree(tmp_path, {"pkg/__init__.py": "", "pkg/test_m.py": _P.format(body=_HARMLESS[shape])})
    _ws(tmp_path / "ws.rrplan", [("pkg.test_m::test_a", "A"), ("pkg.test_m::test_b", "B")])
    rc, _, err = _apply(capsys, tmp_path)
    assert rc == 0, err
    text = (tmp_path / "pkg/test_m.py").read_text(encoding="utf-8")
    assert "pytestmark" not in text  # the shared marker was split per test
    assert '@pytest.mark.rr("A")' in text and '@pytest.mark.rr("B")' in text


def test_fixture_and_setup_bodies_apply(capsys, tmp_path):
    """A test-named binding inside a fixture or a ``setUp`` runs after
    collection, so it no longer refuses and the check confirms it is safe."""
    _tree(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/test_f.py": "import sys\n\nimport pytest\n\n"
            'pytestmark = pytest.mark.rr("A", "B")\n\n\n'
            "def helper():\n    pass\n\n\n"
            "@pytest.fixture\ndef f():\n"
            '    setattr(sys.modules[__name__], "test_leak", helper)\n    yield\n\n\n'
            "def test_a():\n    assert True\n\n\ndef test_b():\n    assert True\n",
            "pkg/test_s.py": "import sys\n\nimport unittest\n\nimport pytest\n\n\n"
            '@pytest.mark.rr("A", "B")\nclass Checks(unittest.TestCase):\n'
            "    def setUp(self):\n"
            '        setattr(sys.modules[__name__], "test_leak_s", lambda: None)\n\n'
            "    def test_x(self):\n        pass\n\n    def test_y(self):\n        pass\n",
        },
    )
    _ws(
        tmp_path / "ws.rrplan",
        [
            ("pkg.test_f::test_a", "A"),
            ("pkg.test_f::test_b", "B"),
            ("pkg.test_s.Checks::test_x", "A"),
            ("pkg.test_s.Checks::test_y", "B"),
        ],
    )
    rc, _, err = _apply(capsys, tmp_path)
    assert rc == 0, err
    assert "pytestmark" not in (tmp_path / "pkg/test_f.py").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# A collection that fails: refuse by default, proceed with --no-collect-check. #
# --------------------------------------------------------------------------- #


def _failing_tree(tmp_path):
    _tree(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/test_g.py": _H,  # a clean, decided file
            "pkg/test_bad.py": "import a_module_that_does_not_exist  # noqa: F401\n",
        },
    )
    _ws(tmp_path / "ws.rrplan", [("pkg.test_g::test_a", "A")])


def test_collection_failure_refuses(capsys, tmp_path):
    _failing_tree(tmp_path)
    before = (tmp_path / "pkg/test_g.py").read_bytes()
    rc, _, err = _apply(capsys, tmp_path)
    assert rc == 1
    assert "collection failed" in err
    assert (tmp_path / "pkg/test_g.py").read_bytes() == before  # nothing written


def test_no_collect_check_opt_out_proceeds_with_a_warning(capsys, tmp_path):
    _failing_tree(tmp_path)
    rc, _, err = _apply(capsys, tmp_path, "--no-collect-check")
    assert rc == 0, err
    assert "--no-collect-check" in err and "best-effort" in err
    text = (tmp_path / "pkg/test_g.py").read_text(encoding="utf-8")
    assert 'pytest.mark.rr("A")' in text and '"B"' not in text  # narrowed to its one owner


def test_unittest_classes_rewrite_cleanly(capsys, tmp_path):
    """A plain ``unittest.TestCase`` marker split: the check maps the class's
    methods to their nodeids and passes."""
    _tree(tmp_path, {"pkg/__init__.py": "", "pkg/test_m.py": _U})
    _ws(tmp_path / "ws.rrplan", [("pkg.test_m.Checks::test_x", "A"), ("pkg.test_m.Checks::test_y", "B")])
    rc, _, err = _apply(capsys, tmp_path)
    assert rc == 0, err
    text = (tmp_path / "pkg/test_m.py").read_text(encoding="utf-8")
    assert '@pytest.mark.rr("A")' in text and '@pytest.mark.rr("B")' in text


# --------------------------------------------------------------------------- #
# Mapping and comparison, without a pytest subprocess.                        #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "nodeid, path",
    [
        ("pkg/test_m.py::test_a", "pkg.test_m::test_a"),
        ("pkg/test_m.py::TestK::test_s", "pkg.test_m.TestK::test_s"),
        ("pkg/sub/test_m.py::Checks::test_x", "pkg.sub.test_m.Checks::test_x"),
        ("pkg/test_m.py::test_a[x-1]", "pkg.test_m::test_a[x-1]"),
        ("pkg/test_m.py::TestK::test_s[a]", "pkg.test_m.TestK::test_s[a]"),
    ],
)
def test_nodeid_maps_to_case_path(nodeid, path):
    assert nodeid_to_case_path(nodeid) == path


def _cmp(before, after, decided):
    return collect_check.compare(before, after, {CaseKey("//pkg:t", p): o for p, o in decided.items()})


def test_compare_passes_a_clean_split():
    before = {"pkg/test_m.py::test_a": ["A", "B"], "pkg/test_m.py::test_b": ["A", "B"]}
    after = {"pkg/test_m.py::test_a": ["A"], "pkg/test_m.py::test_b": ["B"]}
    assert _cmp(before, after, {"pkg.test_m::test_a": "A", "pkg.test_m::test_b": "B"}) == []


def test_compare_flags_an_undecided_item_that_changed():
    before = {"pkg/test_m.py::test_a": ["A", "B"], "pkg/test_m.py::test_b": ["A", "B"]}
    after = {"pkg/test_m.py::test_a": ["A"], "pkg/test_m.py::test_b": ["A"]}
    offs = _cmp(before, after, {"pkg.test_m::test_a": "A"})
    assert [o.item for o in offs] == ["pkg/test_m.py::test_b"]
    assert offs[0].expected is None and "undecided" in offs[0].reason


def test_compare_flags_a_decided_case_that_keeps_the_wrong_owner():
    before = {"pkg/test_m.py::test_a": ["A", "B"]}
    after = {"pkg/test_m.py::test_a": ["A", "B"]}  # not narrowed
    offs = _cmp(before, after, {"pkg.test_m::test_a": "A"})
    assert offs and offs[0].expected == ["A"] and "does not end with its owner" in offs[0].reason


def test_compare_flags_a_decided_case_matched_by_nothing():
    offs = _cmp({"pkg/test_m.py::test_a": ["A"]}, {"pkg/test_m.py::test_a": ["A"]}, {"pkg.test_m::missing": "A"})
    assert offs and offs[0].item == "pkg.test_m::missing" and "matched no collected item" in offs[0].reason


def test_compare_flags_a_changed_nodeid_set():
    before = {"pkg/test_m.py::test_a": ["A"], "pkg/test_m.py::test_gone": ["A"]}
    after = {"pkg/test_m.py::test_a": ["A"], "pkg/test_m.py::test_new": ["A"]}
    offs = _cmp(before, after, {})
    reasons = {o.item: o.reason for o in offs}
    assert "test_m.py::test_gone" in str(reasons) and "test_m.py::test_new" in str(reasons)
    assert all("nodeid set changed" in r for r in reasons.values())


def test_decided_none_expects_no_id():
    before = {"pkg/test_m.py::test_a": ["A", "B"]}
    after = {"pkg/test_m.py::test_a": []}
    assert _cmp(before, after, {"pkg.test_m::test_a": "none"}) == []
    assert _cmp(before, {"pkg/test_m.py::test_a": ["A"]}, {"pkg.test_m::test_a": "none"})


def test_dump_plugin_uses_the_same_trace_of_as_the_junit_hook():
    """If the dump plugin reimplemented the trace instead of importing it,
    the two could disagree and the check would vouch for a wrong rewrite."""
    assert collect_dump.trace_of is pytest_plugin.trace_of
    assert collect_dump._trylast is pytest_plugin._trylast


# --------------------------------------------------------------------------- #
# Which cases the check holds to their owner: only those the written files     #
# settle. --partial and --dry-run run the check too.                          #
# --------------------------------------------------------------------------- #

_F2 = 'import pytest\n\npytestmark = pytest.mark.rr("A", "B")\n\n\ndef test_a():\n    assert True\n\n\ndef test_b():\n    assert True\n'
_D2 = [("pkg.test_m::test_a", "A"), ("pkg.test_m::test_b", "B")]
_FACTORY = _U + "def make(n):\n    class _C(Checks):\n        N = n\n\n    return _C\n\n\nCase1 = make(1)\n"
_REFUSED = (
    'import pytest\n\npytestmark = pytest.mark.rr("A", "B")\n\n\ndef test_c():\n    pass\n\n\ndef test_d():\n    pass\n'
)


def test_partial_writes_run_the_collection_check(capsys, tmp_path):
    """--partial writes only when something is refused: the check must run
    then too (it used to be skipped whenever the run was blocked)."""
    _tree(tmp_path, {"pkg/__init__.py": "", "pkg/test_m.py": _FACTORY, "pkg/test_r.py": _REFUSED})
    _ws(tmp_path / "ws.rrplan", [("pkg.test_m.Checks::test_x", "A"), ("pkg.test_m.Checks::test_y", "B")])
    before = (tmp_path / "pkg/test_m.py").read_bytes()
    rc, _, err = _apply(capsys, tmp_path, "--partial")
    assert rc == 1
    assert (tmp_path / "pkg/test_m.py").read_bytes() == before  # the factory subclass would lose an id
    assert "collection check refused" in err and "Case1::test_x" in err


def test_partial_writes_a_clean_file_after_the_check(capsys, tmp_path):
    """The refused file's cases are undecided for the check: they keep their
    before-ids, and the clean file is written."""
    _tree(tmp_path, {"pkg/__init__.py": "", "pkg/test_m.py": _F2, "pkg/test_r.py": _REFUSED})
    _ws(tmp_path / "ws.rrplan", _D2)
    rc, _, err = _apply(capsys, tmp_path, "--partial")
    assert rc == 1  # test_r.py is refused
    assert "collection check refused" not in err and "collection failed" not in err
    assert 'pytest.mark.rr("A")' in (tmp_path / "pkg/test_m.py").read_text(encoding="utf-8")
    assert (tmp_path / "pkg/test_r.py").read_text(encoding="utf-8") == _REFUSED


def test_untagged_decided_cases_do_not_block_the_check(capsys, tmp_path):
    """A decided case that declares no id is settled by verified_by in the
    model, not by the write: the check holds it to its before-ids ([])."""
    _tree(
        tmp_path,
        {"pkg/__init__.py": "", "pkg/test_m.py": _F2, "pkg/test_u.py": "def test_u():\n    pass\n"},
    )
    _ws(tmp_path / "ws.rrplan", [*_D2, ("pkg.test_u::test_u", "A")])
    rc, _, err = _apply(capsys, tmp_path)
    assert rc == 0, err
    assert "declare no id" in err
    assert '@pytest.mark.rr("A")' in (tmp_path / "pkg/test_m.py").read_text(encoding="utf-8")


def test_only_does_not_hold_cases_outside_it(capsys, tmp_path):
    _tree(
        tmp_path,
        {"pkg/__init__.py": "", "other/__init__.py": "", "pkg/test_m.py": _F2, "other/test_o.py": _F2},
    )
    _ws(tmp_path / "ws.rrplan", [*_D2, ("other.test_o::test_a", "A"), ("other.test_o::test_b", "B")])
    rc, _, err = _apply(capsys, tmp_path, "--only", "pkg")
    assert rc == 0, err
    assert '@pytest.mark.rr("A")' in (tmp_path / "pkg/test_m.py").read_text(encoding="utf-8")
    assert (tmp_path / "other/test_o.py").read_text(encoding="utf-8") == _F2


def test_dry_run_runs_the_collection_check(capsys, tmp_path):
    """--dry-run reports what apply would do: a rewrite the check refuses is
    not reported as 'would rewrite'."""
    _tree(tmp_path, {"pkg/__init__.py": "", "pkg/test_m.py": _FACTORY})
    _ws(tmp_path / "ws.rrplan", [("pkg.test_m.Checks::test_x", "A"), ("pkg.test_m.Checks::test_y", "B")])
    before = (tmp_path / "pkg/test_m.py").read_bytes()
    rc, out, err = _apply(capsys, tmp_path, "--dry-run")
    assert rc == 1
    assert "collection check refused" in err and "Case1::test_x" in err
    assert "would rewrite" not in err and "would hold back pkg/test_m.py" in err
    assert "+++ b/pkg/test_m.py" in out  # the diff is still shown
    assert (tmp_path / "pkg/test_m.py").read_bytes() == before


# --------------------------------------------------------------------------- #
# Static guards: a hook-named function run at import time is judged.          #
# --------------------------------------------------------------------------- #

_IMPORT_TIME_HOOKS = {
    "setUp_called": "def setUp():\n    globals()['test_z'] = helper\n\n\nsetUp()\n",
    "setup_module_called": "def setup_module():\n    globals()['test_z'] = helper\n\n\nsetup_module()\n",
    "setup_class_called": "class TestK:\n    @classmethod\n    def setup_class(cls):\n"
    "        setattr(cls, 'test_s', helper)\n\n\nTestK.setup_class()\n",
    "setUp_getattr": "import sys\n\n\ndef setUp():\n    globals()['test_z'] = helper\n\n\n"
    "getattr(sys.modules[__name__], 'setUp')()\n",
    "setUp_via_helper": "def setUp():\n    globals()['test_z'] = helper\n\n\ndef _go():\n    setUp()\n\n\n_go()\n",
    "setUp_as_decorator": "def setUp(f):\n    globals()['test_z'] = helper\n    return f\n\n\n@setUp\ndef other():\n"
    "    pass\n",
}


@pytest.mark.parametrize("shape", sorted(_IMPORT_TIME_HOOKS))
def test_hook_named_function_called_at_import_time_is_refused_statically(capsys, tmp_path, shape):
    """Even with --no-collect-check: the post-collection exemption is for
    hooks pytest runs, not for a function named like one that the module
    calls itself."""
    _tree(tmp_path, {"pkg/__init__.py": "", "pkg/test_m.py": _H + _IMPORT_TIME_HOOKS[shape]})
    _ws(tmp_path / "ws.rrplan", [("pkg.test_m::test_a", "A")])
    before = (tmp_path / "pkg/test_m.py").read_bytes()
    rc, _, err = _apply(capsys, tmp_path, "--no-collect-check")
    assert rc == 1, err
    assert "refused pkg/test_m.py" in err
    assert (tmp_path / "pkg/test_m.py").read_bytes() == before


def test_super_setup_call_keeps_the_exemption(capsys, tmp_path):
    """``super().setUp()`` from another setUp runs after collection too."""
    src = (
        "import unittest\n\nimport pytest\n\n\nclass Base(unittest.TestCase):\n"
        "    def setUp(self):\n        exec('y = 2')\n\n\n"
        '@pytest.mark.rr("A", "B")\nclass TestChecks(Base):\n'
        "    def setUp(self):\n        super().setUp()\n        exec('z = 3')\n\n"
        "    def test_x(self):\n        pass\n\n    def test_y(self):\n        pass\n"
    )
    _tree(tmp_path, {"pkg/__init__.py": "", "pkg/test_m.py": src})
    _ws(tmp_path / "ws.rrplan", [("pkg.test_m.TestChecks::test_x", "A"), ("pkg.test_m.TestChecks::test_y", "B")])
    rc, _, err = _apply(capsys, tmp_path)
    assert rc == 0, err


# --------------------------------------------------------------------------- #
# The collection runs as the project's own would (no spurious refusals).      #
# --------------------------------------------------------------------------- #


def test_ini_testpaths_pick_what_is_collected(capsys, tmp_path):
    _tree(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/test_m.py": _F2,
            "pytest.ini": "[pytest]\ntestpaths = pkg\n",
            "scripts/test_hw.py": "import hwlib_not_installed  # noqa: F401\n",
        },
    )
    _ws(tmp_path / "ws.rrplan", _D2)
    rc, _, err = _apply(capsys, tmp_path)
    assert rc == 0, err


@pytest.mark.parametrize("form", ["two-tokens", "equals"])
def test_pytest_args_takes_a_dash_leading_value(capsys, tmp_path, form):
    _tree(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/test_m.py": _F2,
            "scripts/test_hw.py": "import hwlib_not_installed  # noqa: F401\n",
        },
    )
    _ws(tmp_path / "ws.rrplan", _D2)
    args = ["--pytest-args", "--ignore=scripts"] if form == "two-tokens" else ["--pytest-args=--ignore=scripts"]
    rc, _, err = _apply(capsys, tmp_path, *args)
    assert rc == 0, err


def test_argline_values_are_joined():
    assert cli._join_argline_values(["migrate", "apply", "--pytest-args", "-x -q", "w"]) == [
        "migrate",
        "apply",
        "--pytest-args=-x -q",
        "w",
    ]
    assert cli._join_argline_values(["--", "--pytest-args", "-x"]) == ["--", "--pytest-args", "-x"]


def test_symlinked_dirs_are_ignored_in_both_trees(capsys, tmp_path):
    """Bazel's convenience symlinks are not copied; they must not be
    collected in the original tree either, or the nodeid sets differ."""
    _tree(tmp_path, {"pkg/__init__.py": "", "pkg/test_m.py": _F2})
    try:
        (tmp_path / "bazel-ws").symlink_to(tmp_path / "pkg", target_is_directory=True)
    except OSError:  # pragma: no cover - no symlinks on this platform
        pytest.skip("symlinks unsupported")
    _ws(tmp_path / "ws.rrplan", _D2)
    rc, _, err = _apply(capsys, tmp_path)
    assert rc == 0, err


def test_a_dir_named_like_a_venv_is_still_copied(capsys, tmp_path):
    _tree(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/test_m.py": _F2,
            "tools_venvutil/__init__.py": "",
            "tools_venvutil/test_v.py": "def test_v():\n    pass\n",
        },
    )
    _ws(tmp_path / "ws.rrplan", _D2)
    rc, _, err = _apply(capsys, tmp_path)
    assert rc == 0, err


def test_a_real_virtualenv_is_not_copied(tmp_path):
    _tree(tmp_path / "src", {"pkg/test_m.py": _F2, "env/pyvenv.cfg": "home = /usr\n", "env/lib/x.py": ""})
    ignored = collect_check._copy_tree(str(tmp_path / "src"), str(tmp_path / "dst"))
    assert "env" in ignored and not (tmp_path / "dst/env").exists()
    assert (tmp_path / "dst/pkg/test_m.py").exists()


def test_parametrize_ids_with_absolute_paths(capsys, tmp_path):
    """An id built from ``__file__`` differs between the trees only by the
    copy's location: it must not read as a changed nodeid."""
    src = (
        "import os\n\nimport pytest\n\nHERE = os.path.dirname(os.path.abspath(__file__))\n\n\n"
        '@pytest.mark.rr("C")\n@pytest.mark.parametrize("f", [os.path.join(HERE, "d1.json")])\n'
        "def test_p(f):\n    pass\n\n\n"
        '@pytest.mark.rr("A", "B")\ndef test_b():\n    pass\n'
    )
    _tree(tmp_path, {"pkg/__init__.py": "", "pkg/test_m.py": src})
    _ws(tmp_path / "ws.rrplan", [("pkg.test_m::test_b", "B")])
    rc, _, err = _apply(capsys, tmp_path)
    assert rc == 0, err


def test_strict_markers_collect_with_the_dump_plugin(capsys, tmp_path):
    """The dump plugin registers rr's markers as the JUnit hook does."""
    _tree(
        tmp_path,
        {"pkg/__init__.py": "", "pkg/test_m.py": _F2, "pytest.ini": "[pytest]\naddopts = --strict-markers\n"},
    )
    _ws(tmp_path / "ws.rrplan", _D2)
    rc, _, err = _apply(capsys, tmp_path)
    assert rc == 0, err
    assert collect_dump.pytest_configure is pytest_plugin.pytest_configure


def test_collection_leaves_no_bytecode_in_the_users_tree(capsys, tmp_path, monkeypatch):
    monkeypatch.delenv("PYTHONDONTWRITEBYTECODE", raising=False)
    _tree(tmp_path, {"pkg/__init__.py": "", "pkg/test_m.py": _F2})
    _ws(tmp_path / "ws.rrplan", _D2)
    rc, _, err = _apply(capsys, tmp_path)
    assert rc == 0, err
    assert not list(tmp_path.rglob("__pycache__"))


def test_collection_pythonpath_holds_only_rules_requirements(capsys, tmp_path):
    """rr's own site-packages must not leak into the --python interpreter:
    every PYTHONPATH entry the check adds is the tree or a directory holding
    nothing but rules_requirements."""
    log = tmp_path.parent / f"{tmp_path.name}-pythonpath.log"
    conftest = (
        "import os\n\n\ndef pytest_configure(config):\n"
        f"    with open({str(log)!r}, 'a', encoding='utf-8') as fh:\n"
        "        for p in os.environ['PYTHONPATH'].split(os.pathsep):\n"
        "            if os.path.isdir(p):\n"
        "                fh.write(p + '\\t' + ','.join(sorted(os.listdir(p))) + '\\n')\n"
    )
    _tree(tmp_path, {"conftest.py": conftest, "pkg/__init__.py": "", "pkg/test_m.py": _F2})
    _ws(tmp_path / "ws.rrplan", _D2)
    rc, _, err = _apply(capsys, tmp_path)
    assert rc == 0, err
    rows = [line.split("\t") for line in log.read_text(encoding="utf-8").splitlines()]
    assert rows
    for path, listing in rows:
        if "rr-collect-" in path and not path.endswith("after"):
            assert listing == "rules_requirements", (path, listing)
    assert any("rr-collect-" in path and not path.endswith("after") for path, _ in rows)


def test_collection_time_skips_are_reported(capsys, tmp_path):
    _tree(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/test_m.py": _F2,
            "pkg/test_hw.py": 'import pytest\n\npytest.importorskip("hwlib_not_installed")\n',
        },
    )
    _ws(tmp_path / "ws.rrplan", _D2)
    rc, _, err = _apply(capsys, tmp_path)
    assert rc == 0, err
    assert "skipped at collection time" in err and "pkg/test_hw.py" in err
