# SPDX-License-Identifier: AGPL-3.0-or-later
"""Label normalization: one spelling per target, so a claim matches its
evidence and two spellings of one target are never two targets."""

import pytest

from rules_requirements.labels import BadTarget, is_pseudo, normalize_label, read_known_targets, try_normalize


@pytest.mark.parametrize(
    "label, want",
    [
        ("//p:n", "//p:n"),
        ("//p", "//p:p"),
        ("//a/b/c", "//a/b/c:c"),
        ("//:n", "//:n"),
        ("@@//p:n", "//p:n"),
        ("@//p:n", "//p:n"),
        ("@@//p", "//p:p"),
        ("  //p:n  ", "//p:n"),
        ("//p:sub/dir/n.test", "//p:sub/dir/n.test"),
        # the main repository under its apparent name
        ("@splanc//web:clocksync_test", "//web:clocksync_test"),
        ("@@splanc//web", "//web:web"),
        ("@splanc", "//:splanc"),
        # another module repository: apparent names, canonical decorations dropped
        ("@rules_requirements//p:n", "@rules_requirements//p:n"),
        ("@@rules_requirements+//p:n", "@rules_requirements//p:n"),  # Bazel 8
        ("@@rules_requirements~//p:n", "@rules_requirements//p:n"),  # Bazel 7.1+
        ("@@rules_requirements~0.3.0//p:n", "@rules_requirements//p:n"),  # Bazel 7.0
        ("@rules_requirements~//p:n", "@rules_requirements//p:n"),  # bazel-testlogs/external/<repo>~/
        ("@foo", "@foo//:foo"),
        ("@foo//p", "@foo//p:p"),
        # extension repositories keep their canonical name, always as @@
        ("@@rules_python~~pip~pypi//p:n", "@@rules_python~~pip~pypi//p:n"),
        ("@rules_python++pip+pypi//p:n", "@@rules_python++pip+pypi//p:n"),
        # pseudo-targets pass through
        ("suite:my suite", "suite:my suite"),
        ("record:usability_study", "record:usability_study"),
    ],
)
def test_normalize_label(label, want):
    assert normalize_label(label, main_repo="splanc") == want
    assert normalize_label(want, main_repo="splanc") == want  # idempotent


def test_main_repo_is_only_dropped_when_configured():
    assert normalize_label("@splanc//p:n") == "@splanc//p:n"
    assert normalize_label("@splanc//p:n", "splanc") == "//p:n"
    assert normalize_label("@@splanc+//p:n", "splanc") == "//p:n"


@pytest.mark.parametrize(
    "label",
    [
        "",
        "   ",
        ":n",
        "p:n",
        "n",
        "//",
        "//p:",
        "///p:n",
        "//p/:n",
        "//p//q:n",
        "//p/...",
        "//p/...:all",
        "//p:a#b",
        "//p:a b",
        "//p:a*",
        "//p:a:b",
        "//p:../n",
        "@",
        "@@",
        "@x y//p:n",
        "@x/y//p:n",
        "suite:",
        "record:",
        "record:a#b",
    ],
)
def test_bad_targets(label):
    with pytest.raises(BadTarget):
        normalize_label(label)
    assert try_normalize(label) is None


def test_non_string_is_bad():
    with pytest.raises(BadTarget):
        normalize_label(3)  # type: ignore[arg-type]


def test_pseudo_targets():
    assert is_pseudo("suite:x") and is_pseudo("record:y") and not is_pseudo("//suite:x")


def test_read_known_targets():
    text = "//a:b\n\n# comment\n@@//c:d\n  //e  \nnot a label\n"
    known, bad = read_known_targets(text)
    assert known == ["//a:b", "@@//c:d", "//e"]
    assert bad == ["not a label"]
