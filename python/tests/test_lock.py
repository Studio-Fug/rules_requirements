# SPDX-License-Identifier: AGPL-3.0-or-later
"""The verification-set lock: one owner per locked case, expectations only.

Parsing (and every way a lock can be invalid) is also exercised through
validation in test_validate.py; here: the reader's API, the writer
(render_lock / write_lock round-trips), plan_lock (what ``rr sets lock``
writes), and that a lock never creates ownership.
"""

import os

import pytest
from conftest import write

from rules_requirements import lock as rr_lock
from rules_requirements.attribution import CaseKey, attribute
from rules_requirements.ingest import Evidence, TestCase
from rules_requirements.lock import Lock, LockEntry, LockError, parse_lock, plan_lock, render_lock, write_lock
from rules_requirements.model import read_model

HEAD = "schema: rules_requirements/verification-lock/v1\ncases:\n"


def entries(lock):
    return [(e.target, e.path, e.owner) for e in lock.entries]


def test_reader_api():
    lock = parse_lock(
        HEAD
        + '  "@@//web:a_test":\n    "a::one": REQ-1\n    "a::two": REQ-2\n  //req:model_test:\n    "[target]": REQ-2\n',
        "verification.rrlock",
    )
    assert entries(lock) == [
        ("//web:a_test", "a::one", "REQ-1"),
        ("//web:a_test", "a::two", "REQ-2"),
        ("//req:model_test", "[target]", "REQ-2"),
    ]
    assert lock.owner_of("//web:a_test", "a::two") == "REQ-2" and lock.owner_of("//web:a_test", "a::x") is None
    assert [e.case for e in lock.of("REQ-2")] == ["//web:a_test#a::two", "//req:model_test#[target]"]
    assert lock.entry("//web:a_test", "a::one").line == 4 and lock.entry("//web:a_test", "a::one").target_line == 3
    assert (
        len(lock) == 3 and lock.targets() == ["//req:model_test", "//web:a_test"] and lock.path == "verification.rrlock"
    )
    assert entries(parse_lock(HEAD.replace("cases:\n", "cases:\n") + "")) == []  # no cases at all


def test_a_hand_built_lock_cannot_name_two_owners_for_one_case():
    with pytest.raises(LockError, match="locked twice"):
        Lock((LockEntry("//a:t", "c::x", "REQ-1"), LockEntry("//a:t", "c::x", "REQ-2")))


@pytest.mark.parametrize(
    "text, fragment",
    [
        (HEAD + "  //a:t:\n    c::x: [REQ-1, REQ-2]\n", "exactly one owner"),
        (HEAD + "  //a:t:\n    c::x: REQ-1\n    c::x: REQ-2\n", "duplicate key"),
        (HEAD + "  //a:t:\n    c::x: REQ-1 REQ-2\n", "not one id"),
        (HEAD + "  //a:t: {c::x: REQ-1}\n  '//a': {c::x: REQ-2}\n", None),  # two spellings of one target: fine
        (HEAD + "  //a:t: {c::x: REQ-1}\n  '@@//a:t': {c::x: REQ-2}\n", "locked twice"),
        ("schema: other\n", "schema must be"),
        (HEAD + "  //a:t: [c::x]\n", "must map case paths"),
        (HEAD + "  'not a label': {c::x: REQ-1}\n", "not an absolute label"),
        ("[1, 2]\n", "one YAML mapping"),
    ],
)
def test_invalid_locks(text, fragment):
    if fragment is None:
        assert len(parse_lock(text)) == 2
        return
    with pytest.raises(LockError, match=fragment):
        parse_lock(text)


def test_render_round_trips_and_is_deterministic():
    lock = Lock(
        (
            LockEntry("//web:b_test", "b::z", "REQ-10"),
            LockEntry("//web:b_test", "b::a: colon, 'quotes' \"and\" \\ backslash", "REQ-2"),
            LockEntry("@rules_requirements//x:y", "[target]", "MIT-1"),
            LockEntry("record:panel inspection", "inspection.TM-2::panel shows °C", "UN-1"),
            LockEntry("//web:a_test", "a::#1 [x] *", "REQ-1"),
            LockEntry("//web:a_test", "a::on", "REQ-2"),
        )
    )
    text = render_lock(lock)
    assert text.startswith(rr_lock.HEADER + "schema: rules_requirements/verification-lock/v1\ncases:\n")
    assert '  //web:a_test:\n    "a::#1 [x] *": REQ-1\n    "a::on": REQ-2\n' in text
    assert '  "@rules_requirements//x:y":\n    "[target]": MIT-1\n' in text
    back = parse_lock(text)
    assert sorted(entries(back)) == sorted(entries(lock))
    assert render_lock(back) == text  # stable
    assert render_lock(reversed(lock.entries)) == text  # order-independent
    assert render_lock(()) == rr_lock.HEADER + "schema: rules_requirements/verification-lock/v1\ncases: {}\n"
    assert entries(parse_lock(render_lock(()))) == []


@pytest.mark.parametrize("char", ["\x85", "\u2028", "\u2029", "\x7f", "\x9b", "\uffff", "\ufffe", "\ud800"])
def test_render_escapes_what_yaml_cannot_carry_raw(char):
    """XML 1.0 allows NEL, U+2028, DEL, C1 controls and U+FFFF-ish characters in
    a case name; the written lock must still parse back to the same entries."""
    lock = Lock((LockEntry("//p:t", f"c::n{char}x", "REQ-1"), LockEntry("//p:t", "c::😀 é\tx", "REQ-2")))
    text = render_lock(lock)
    assert char not in text and "😀 é" in text  # escaped only where needed
    assert entries(parse_lock(text)) == entries(lock)


def test_render_quotes_ids_yaml_would_misread():
    text = render_lock([LockEntry("//a:t", "c::x", "no"), LockEntry("//a:t", "c::y", "REQ-1")])
    assert '"c::x": "no"\n' in text and '"c::y": REQ-1\n' in text
    assert parse_lock(text).owner_of("//a:t", "c::x") == "no"


def test_write_lock_is_atomic(tmp_path, monkeypatch):
    path = tmp_path / "verification.rrlock"
    write_lock(str(path), [LockEntry("//a:t", "c::x", "REQ-1")])
    first = path.read_text()
    assert parse_lock(first).owner_of("//a:t", "c::x") == "REQ-1"

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(rr_lock.os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        write_lock(str(path), [LockEntry("//a:t", "c::x", "REQ-2")])
    assert path.read_text() == first and os.listdir(tmp_path) == ["verification.rrlock"]


def test_configured_lock(tmp_path):
    def model(config):
        model_, _ = read_model(write(tmp_path, "req/model.yaml", f"config: {config}\nrequirements: []\n"))
        return model_

    assert rr_lock.configured_lock(model("{}")) is None
    with pytest.raises(LockError, match="does not exist"):
        rr_lock.configured_lock(model("{sets_lock: missing.rrlock}"))
    write(tmp_path, "req/sets/verification.rrlock", HEAD + "  '@app//p:t': {c::x: REQ-1}\n")
    lock = rr_lock.configured_lock(model("{sets_lock: sets/verification.rrlock, main_repo: app}"))
    assert entries(lock) == [("//p:t", "c::x", "REQ-1")] and lock.path.endswith(
        os.path.join("sets", "verification.rrlock")
    )


# --------------------------------------------------------------------------- #
# Expectations only, never ownership                                          #
# --------------------------------------------------------------------------- #

MODEL = """
config: {attribution: ATTRIBUTION}
user_needs: [{id: UN-1, title: n}]
requirements:
  - {id: REQ-1, title: a, satisfies: [UN-1], verified_by: [{target: //a:t, cases: ['c::*']}]}
  - {id: REQ-2, title: b, satisfies: [UN-1], verified_by: [{target: //b:t, cases: ['c::lit', 'c::glob*']}, {target: //w:t, whole: true, reason: r}]}
  - {id: REQ-3, title: c, satisfies: [UN-1]}
"""


def model_of(tmp_path, mode="model"):
    model, _ = read_model(write(tmp_path, f"{mode}.yaml", MODEL.replace("ATTRIBUTION", mode)))
    assert not model.parse_errors
    return model


def ev(*cases):
    out = Evidence()
    for case in cases:
        out.add(case)
    return out


def test_a_lock_never_creates_ownership(tmp_path):
    lock = Lock(
        (
            LockEntry("//a:t", "c::x", "REQ-3"),  # claimed by REQ-1: the claim decides
            LockEntry("//z:t", "c::free", "REQ-3"),  # nobody claims or tags it
        )
    )
    evidence = ev(TestCase("x", "passed", "c", target="//a:t"), TestCase("free", "passed", "c", target="//z:t"))
    att = attribute(model_of(tmp_path, "hybrid"), evidence, lock=lock)
    assert dict(att.owner) == {CaseKey("//a:t", "c::x"): "REQ-1"}
    assert [(str(m.key), m.state, m.via) for m in att.members_of("REQ-3")] == [
        ("//a:t#c::x", "moved", "lock"),
        ("//z:t#c::free", "moved", "lock"),
    ]
    assert all(not m.owned for m in att.members_of("REQ-3"))


def test_plan_lock_targets_that_ran_are_locked_to_what_attribution_decided(tmp_path):
    model = model_of(tmp_path)
    previous = Lock(
        (
            LockEntry("//a:t", "c::x", "REQ-2"),  # the claim now gives it to REQ-1: an owner change
            LockEntry("//a:t", "c::gone", "REQ-1"),  # the target ran without it: a removal
            LockEntry("//b:t", "c::glob1", "REQ-2"),  # //b:t did not run, still claimed: kept
            LockEntry("//b:t", "c::old", "REQ-2"),  # //b:t did not run, no claim selects it: stale
        ),
        "verification.rrlock",
    )
    evidence = ev(TestCase("x", "passed", "c", target="//a:t"), TestCase("y", "passed", "c", target="//a:t"))
    att = attribute(model, evidence, lock=previous)
    plan = plan_lock(model, att, previous)
    assert not plan.refused and plan.blocked  # removals were not allowed
    assert entries(plan.lock) == [
        ("//a:t", "c::gone", "REQ-1"),  # kept until --allow-removals
        ("//a:t", "c::x", "REQ-1"),
        ("//a:t", "c::y", "REQ-1"),
        ("//b:t", "c::glob1", "REQ-2"),
        ("//b:t", "c::lit", "REQ-2"),  # a literal selector pins itself
        ("//b:t", "c::old", "REQ-2"),
        ("//w:t", "[target]", "REQ-2"),  # a whole claim on a target that did not run
    ]
    assert [e.case for e in plan.removed] == ["//a:t#c::gone", "//b:t#c::old"]
    assert [(old.owner, new.owner) for old, new in plan.changed] == [("REQ-2", "REQ-1")]
    assert [e.case for e in plan.added] == ["//a:t#c::y", "//b:t#c::lit", "//w:t#[target]"]
    allowed = plan_lock(model, att, previous, allow_removals=True)
    assert not allowed.blocked and "c::gone" not in {e.path for e in allowed.lock.entries}
    assert {e.path for e in allowed.lock.entries} == {"c::x", "c::y", "c::glob1", "c::lit", "[target]"}
    assert plan_lock(model, att).lock.path == ""  # a first lock


def test_plan_lock_refuses_over_a_quarantine(tmp_path):
    model = model_of(tmp_path)
    evidence = ev(TestCase("x", "passed", "c", declared=("REQ-1", "REQ-3"), target="//a:t"))
    plan = plan_lock(model, attribute(model, evidence))
    (refused,) = plan.refused
    assert plan.blocked and refused.startswith("multi-tag: //a:t#c::x declares REQ-1, REQ-3; a test case verifies")
    assert refused.endswith("model.yaml:4))")  # the claim's origin
    assert entries(plan.lock) == [("//b:t", "c::lit", "REQ-2"), ("//w:t", "[target]", "REQ-2")]


def test_plan_lock_keeps_tag_owned_entries_of_absent_targets_in_hybrid_mode(tmp_path):
    previous = Lock((LockEntry("//t:tagged", "c::t", "REQ-3"),))
    for mode, kept in (("hybrid", True), ("model", False)):
        model = model_of(tmp_path, mode)
        plan = plan_lock(model, attribute(model, Evidence()), previous)
        assert (("//t:tagged", "c::t", "REQ-3") in entries(plan.lock)) is True  # kept either way unless allowed
        assert bool(plan.removed) is not kept, mode
    model = model_of(tmp_path, "hybrid")
    tagged = ev(TestCase("t", "passed", "c", declared=("REQ-3",), target="//t:tagged"))
    assert ("//t:tagged", "c::t", "REQ-3") in entries(plan_lock(model, attribute(model, tagged)).lock)


MOVED = """
config: {attribution: model}
user_needs: [{id: UN-1, title: n}]
requirements:
  - {id: REQ-1, title: a, satisfies: [UN-1], verified_by: [{target: //p:t, cases: ['c::a']}, {target: //h:e2e, cases: ['s::y']}]}
  - {id: REQ-2, title: b, satisfies: [UN-1], verified_by: [{target: //h:e2e, cases: SELECTOR}]}
"""


@pytest.mark.parametrize("selector", ["['s::x']", "['s::x*']"])
@pytest.mark.parametrize("allow", [False, True])
def test_plan_lock_an_owner_change_on_a_target_that_did_not_run_is_no_removal(tmp_path, selector, allow):
    """The model moves s::x from REQ-1 to REQ-2 while only the software lane
    ran (//h:e2e is absent): the entry follows the claim (allowed, listed in
    ``changed``), it is neither removed nor dropped by --allow-removals."""
    model, _ = read_model(write(tmp_path, "m.yaml", MOVED.replace("SELECTOR", selector)))
    assert not model.parse_errors
    previous = Lock(
        (
            LockEntry("//h:e2e", "s::x", "REQ-1"),
            LockEntry("//h:e2e", "s::y", "REQ-1"),
            LockEntry("//p:t", "c::a", "REQ-1"),
        )
    )
    att = attribute(model, ev(TestCase("a", "passed", "c", target="//p:t")), lock=previous)
    plan = plan_lock(model, att, previous, allow_removals=allow)
    assert not plan.blocked and plan.removed == ()
    assert [(old.case, old.owner, new.owner) for old, new in plan.changed] == [("//h:e2e#s::x", "REQ-1", "REQ-2")]
    assert entries(plan.lock) == [
        ("//h:e2e", "s::x", "REQ-2"),
        ("//h:e2e", "s::y", "REQ-1"),
        ("//p:t", "c::a", "REQ-1"),
    ]


def test_plan_lock_removed_and_lock_agree(tmp_path):
    """Without --allow-removals every removed entry stays in the lock unchanged;
    with it, none does; a case another entity's literal selector takes is an
    owner change, not a removal."""
    model = model_of(tmp_path)
    previous = Lock(
        (
            LockEntry("//b:t", "c::lit", "REQ-3"),  # REQ-2's literal selector takes it
            LockEntry("//b:t", "c::old", "REQ-2"),  # no claim selects it: stale
            LockEntry("//a:t", "c::gone", "REQ-1"),  # its target ran without it
        )
    )
    att = attribute(model, ev(TestCase("x", "passed", "c", target="//a:t")), lock=previous)
    for allow in (False, True):
        plan = plan_lock(model, att, previous, allow_removals=allow)
        assert [e.case for e in plan.removed] == ["//a:t#c::gone", "//b:t#c::old"]
        assert [(old.owner, new.owner) for old, new in plan.changed] == [("REQ-3", "REQ-2")]
        for gone in plan.removed:
            assert (plan.lock.entry(gone.target, gone.path) == gone) is not allow
