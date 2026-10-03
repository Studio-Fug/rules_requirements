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
            + "class Meta(type):\n    def __init__(cls, name, bases, ns):\n        super().__init__(name, bases, ns)\n"
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
