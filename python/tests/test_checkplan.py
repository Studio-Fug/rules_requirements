# SPDX-License-Identifier: AGPL-3.0-or-later
import pytest
from conftest import write

from rules_requirements import ingest
from rules_requirements.hooks.checkplan import CheckPlan, HarnessError
from rules_requirements.hooks.junit_writer import JUnitWriter
from rules_requirements.model import read_model
from rules_requirements.trace import FAILED, INCOMPLETE, VERIFIED, build_matrix

STEPS = {
    "flash_boot": ("ble_advertising",),
    "improv_provision": ("provisioned",),
    "websocket_checks": ("ws_connect", "build_info", "rename"),
    "run": ("completed",),
}
TAGS = {
    "flash_boot.ble_advertising": "REQ-13",
    "improv_provision.provisioned": "REQ-13",
    "websocket_checks.ws_connect": "REQ-13",
    "websocket_checks.build_info": "REQ-35",
    "websocket_checks.rename": "REQ-13",
    "run.completed": "REQ-23",
}


class RigError(RuntimeError):
    pass


def plan_for(**kw):
    report = JUnitWriter("hitl_e2e", default_level="hitl", file="pi/hitl/harness/hitl_e2e.py")
    return report, CheckPlan(report, STEPS, tags=TAGS, is_infrastructure=lambda e: isinstance(e, RigError), **kw)


def full_run(plan, fail_in=None, exc=None):
    """The e2e flow; raises ``exc`` at the point named ``fail_in``."""

    def boom(where):
        if fail_in == where:
            raise exc

    with plan.run():
        boom("setup")
        plan.setup_done()
        with plan.step("flash_boot"):
            boom("flash")
            with plan.check("ble_advertising"):
                boom("ble_advertising")
        with plan.step("improv_provision"):
            boom("provision")
            plan.passed("provisioned")
        with plan.step("websocket_checks"):
            with plan.check("ws_connect"):
                boom("ws_connect")
            with plan.check("build_info"):
                pass
            boom("between_checks")
            with plan.check("rename"):
                pass
        with plan.step("run"):
            plan.passed("completed")
        boom("cleanup")


def cases_of(report):
    return {(c.classname, c.name): (c.status, list(c.requirements), c.message) for c in report.cases}


def test_full_pass_records_one_single_owner_case_per_check():
    report, plan = plan_for()
    full_run(plan)
    cases = cases_of(report)
    assert set(cases) == {(f"hitl_e2e.{s}", c) for s, cs in STEPS.items() for c in cs}
    assert all(status == "passed" and len(ids) == 1 for status, ids, _ in cases.values())
    assert cases[("hitl_e2e.websocket_checks", "build_info")][1] == ["REQ-35"]
    assert all(c.level == "hitl" and c.file == "pi/hitl/harness/hitl_e2e.py" for c in report.cases)
    plan.finish()  # idempotent
    assert len(report.cases) == 6


def test_device_stop_in_a_step_fails_every_unreached_check():
    report, plan = plan_for()
    with pytest.raises(OSError, match="no boot"):
        full_run(plan, "flash", OSError("no boot"))
    cases = cases_of(report)
    assert len(cases) == 6  # no extra case: each check fails through its own owner
    for (_, name), (status, ids, message) in cases.items():
        assert status == "failed" and len(ids) == 1, name
        assert message == "not reached: flash_boot failed: OSError: no boot"


def test_device_failure_in_a_check():
    report, plan = plan_for()
    with pytest.raises(AssertionError):
        full_run(plan, "ws_connect", AssertionError("no welcome"))
    cases = cases_of(report)
    assert cases[("hitl_e2e.flash_boot", "ble_advertising")][0] == "passed"
    assert cases[("hitl_e2e.improv_provision", "provisioned")][0] == "passed"
    assert cases[("hitl_e2e.websocket_checks", "ws_connect")][::2] == ("failed", "AssertionError: no welcome")
    for key in (("hitl_e2e.websocket_checks", "build_info"), ("hitl_e2e.run", "completed")):
        assert cases[key][0] == "failed"
        assert cases[key][2] == "not reached: websocket_checks failed: AssertionError: no welcome"


def test_rig_trouble_before_setup_fails_nothing():
    report, plan = plan_for()
    with pytest.raises(ValueError):  # any exception before setup_done() is setup trouble
        full_run(plan, "setup", ValueError("no WiFi credentials"))
    cases = cases_of(report)
    assert cases.pop(("hitl_e2e", "rig")) == ("error", [], "ValueError: no WiFi credentials")
    assert len(cases) == 6
    for status, ids, message in cases.values():
        assert status == "skipped" and len(ids) == 1
        assert message == "not run: rig trouble: ValueError: no WiFi credentials"


def test_rig_trouble_mid_run_keeps_the_passed_checks_tags():
    report, plan = plan_for()
    with pytest.raises(RigError):
        full_run(plan, "between_checks", RigError("tunnel dropped"))
    cases = cases_of(report)
    assert cases[("hitl_e2e", "rig")][0] == "error"
    assert not any(status == "failed" for status, _, _ in cases.values())
    # REQ-13 also tags `rename`, which never ran. 0.2 withdrew the passed
    # checks' tags; from 0.3 recorded cases are final, and REQ-13's set reads
    # INCOMPLETE through the skipped `rename` instead.
    for name in ("ble_advertising", "provisioned", "ws_connect"):
        status, ids, _ = next(v for (_, n), v in cases.items() if n == name)
        assert status == "passed" and ids == ["REQ-13"], name
    # REQ-35's only check passed.
    assert cases[("hitl_e2e.websocket_checks", "build_info")] == ("passed", ["REQ-35"], "")
    assert cases[("hitl_e2e.websocket_checks", "rename")][:2] == ("skipped", ["REQ-13"])
    assert cases[("hitl_e2e.run", "completed")][:2] == ("skipped", ["REQ-23"])


def test_rig_trouble_inside_a_check_records_nothing_for_it():
    report, plan = plan_for()
    with pytest.raises(RigError):
        full_run(plan, "ble_advertising", RigError("ssh 255"))
    cases = cases_of(report)
    assert cases[("hitl_e2e.flash_boot", "ble_advertising")][:2] == ("skipped", ["REQ-13"])
    assert sum(status == "error" for status, _, _ in cases.values()) == 1  # just `rig`


def test_device_failure_after_every_check_passed():
    report, plan = plan_for()
    with pytest.raises(OSError):
        full_run(plan, "cleanup", OSError("teardown"))
    cases = cases_of(report)
    assert cases.pop(("hitl_e2e", "after_checks")) == ("error", [], "OSError: teardown")
    assert all(status == "passed" for status, _, _ in cases.values())


def test_harness_bugs():
    report, plan = plan_for()
    with plan.run(), plan.step("flash_boot"):
        plan.passed("ble_advertising")
        # improv_provision, websocket_checks and run never execute their checks
    cases = cases_of(report)
    assert cases[("hitl_e2e.flash_boot", "ble_advertising")][0] == "passed"
    missing = [v for k, v in cases.items() if k[1] != "ble_advertising"]
    assert len(missing) == 5
    assert all(v[0] == "error" and v[2] == "planned check never executed (harness bug)" for v in missing)

    report, plan = plan_for()
    with pytest.raises(HarnessError, match="unknown step 'flsah_boot'"), plan.run():
        plan.setup_done()
        with plan.step("flsah_boot"):
            pass
    cases = cases_of(report)
    assert cases.pop(("hitl_e2e", "harness"))[0] == "error"
    assert all(v[0] == "error" and "harness bug" in v[2] for v in cases.values())

    report, plan = plan_for()
    with (
        pytest.raises(HarnessError, match="unknown check 'ble'"),
        plan.run(),
        plan.step("flash_boot"),
        plan.check("ble"),
    ):
        pass
    assert cases_of(report)[("hitl_e2e", "harness")][0] == "error"

    _, plan = plan_for()
    with pytest.raises(HarnessError, match="outside a step"):
        plan.passed("completed")
    with pytest.raises(HarnessError, match="recorded twice"):
        plan.passed("run.completed")
        plan.passed("run.completed")
    with pytest.raises(HarnessError, match="inside step"), plan.step("run"), plan.step("flash_boot"):
        pass


def test_a_completed_step_with_a_missing_check_is_a_harness_bug_even_on_device_failure():
    report, plan = plan_for()
    with pytest.raises(OSError), plan.run():
        with plan.step("flash_boot"):
            pass  # forgot its check
        with plan.step("improv_provision"):
            raise OSError("provisioning timed out")
    cases = cases_of(report)
    assert cases[("hitl_e2e.flash_boot", "ble_advertising")][::2] == (
        "error",
        "planned check never executed (harness bug)",
    )
    assert cases[("hitl_e2e.improv_provision", "provisioned")][2].startswith("not reached: improv_provision failed")


def test_explicit_results_by_qualified_name():
    report, plan = plan_for()
    with plan.run():
        with plan.step("websocket_checks"):
            plan.failed("ws_connect", "no welcome frame")
            plan.skipped("build_info", "no descriptor in runfiles")
        plan.passed("websocket_checks.rename")
        plan.passed("flash_boot.ble_advertising")
        plan.passed("improv_provision.provisioned")
        plan.passed("run.completed", "end to end")
    cases = cases_of(report)
    assert cases[("hitl_e2e.websocket_checks", "ws_connect")] == ("failed", ["REQ-13"], "no welcome frame")
    assert cases[("hitl_e2e.websocket_checks", "build_info")][0] == "skipped"
    assert cases[("hitl_e2e.run", "completed")] == ("passed", ["REQ-23"], "end to end")
    assert plan.pending() == []


def test_construction_validates_single_ids_and_names():
    report = JUnitWriter("s", file="")
    with pytest.raises(ValueError, match="RR-E101"):
        CheckPlan(report, {"a": ["x"]}, tags={"a.x": ["REQ-1", "REQ-2"]})
    with pytest.raises(ValueError, match="RR-E104"):
        CheckPlan(report, {"a": ["x"]}, tags={"a.x": "REQ-1, REQ-2"})
    with pytest.raises(ValueError, match="RR-E104"):
        CheckPlan(report, {"a": ["x"]}, tags={"a.x": "REQ-1 REQ-2"})
    with pytest.raises(ValueError, match="RR-E104"):
        CheckPlan(report, {"a": ["x"]}, tags={"a.x": ""})
    with pytest.raises(ValueError, match="not a planned"):
        CheckPlan(report, {"a": ["x"]}, tags={"a.y": "REQ-1"})
    with pytest.raises(ValueError, match="more than once"):
        CheckPlan(report, {"a": ["x", "x"]})
    with pytest.raises(TypeError, match="not a string"):
        CheckPlan(report, {"a": "x"})
    assert report.cases == ()


def test_reexported_from_junit_writer():
    from rules_requirements.hooks import junit_writer

    assert junit_writer.CheckPlan is CheckPlan
    with pytest.raises(AttributeError):
        junit_writer.NoSuchThing  # noqa: B018


VERDICT_MODEL = """
requirements:
  - {id: REQ-13, title: Provision and connect, method: hitl}
  - {id: REQ-23, title: On-hardware evidence, method: hitl}
  - {id: REQ-35, title: Report the firmware build, method: hitl}
"""


def verdicts(tmp_path, report):
    model, _ = read_model(write(tmp_path, "model.yaml", VERDICT_MODEL))
    path = tmp_path / "bazel-testlogs" / "pi" / "hitl" / "e2e" / "test.xml"
    path.parent.mkdir(parents=True, exist_ok=True)
    report.write(str(path))
    matrix = build_matrix(model, ingest.collect([str(path)]))
    return {rid: matrix.status(rid) for rid in ("REQ-13", "REQ-23", "REQ-35")}


def test_verdicts_through_the_report(tmp_path):
    report, plan = plan_for()
    full_run(plan)
    assert verdicts(tmp_path / "pass", report) == dict.fromkeys(("REQ-13", "REQ-23", "REQ-35"), VERIFIED)

    report, plan = plan_for()
    with pytest.raises(OSError):
        full_run(plan, "flash", OSError("no boot"))
    assert verdicts(tmp_path / "device", report) == dict.fromkeys(("REQ-13", "REQ-23", "REQ-35"), FAILED)


def test_rig_trouble_verdicts_are_neither_verified_nor_failed(tmp_path):
    report, plan = plan_for()
    with pytest.raises(RigError):
        full_run(plan, "between_checks", RigError("tunnel dropped"))
    rig = verdicts(tmp_path / "rig", report)
    # neither VERIFIED nor FAILED where a check never ran; REQ-35's one check passed
    assert rig["REQ-35"] == VERIFIED
    assert rig["REQ-13"] not in (VERIFIED, FAILED) and rig["REQ-23"] not in (VERIFIED, FAILED)
    # 0.3 verification sets: a passed check next to a skipped one is INCOMPLETE
    # (CheckPlan no longer withdraws tags), and the rig case, owned by nobody,
    # fails nothing.
    assert rig["REQ-13"] == INCOMPLETE and rig["REQ-23"] == INCOMPLETE


def test_a_result_recorded_inside_its_own_check_block_is_the_only_case():
    # The natural code for a check that can be skipped (board_caps, cert_page):
    # the explicit result stands and the block's own pass is not added to it.
    report = JUnitWriter("s", file="")
    plan = CheckPlan(report, {"a": ["skip", "pass", "fail"]}, tags={"a.skip": "REQ-1", "a.pass": "REQ-2"})
    with pytest.raises(AssertionError), plan.run(), plan.step("a"):
        with plan.check("skip"):
            plan.skipped("skip", "no descriptor in runfiles")
        with plan.check("pass"):
            plan.passed("pass", "explicit")
        with plan.check("fail"):
            plan.failed("fail", "explicit")
            raise AssertionError("then raised")
    assert [(c.name, c.status, c.message) for c in report.cases] == [
        ("skip", "skipped", "no descriptor in runfiles"),
        ("pass", "passed", "explicit"),
        ("fail", "failed", "explicit"),
    ]


def test_step_names_with_a_dot_are_rejected():
    # {"a.b": ["c"], "a": ["b.c"]} would both be "a.b.c"
    with pytest.raises(ValueError, match=r"step name 'a\.b' contains '\.'"):
        CheckPlan(JUnitWriter("s", file=""), {"a.b": ["c"], "a": ["b.c"]})


def test_an_interrupted_or_cleanly_exited_run_is_not_the_device():
    report, plan = plan_for()  # the default classifier would claim nothing
    plan._infrastructure = lambda e: False
    with pytest.raises(KeyboardInterrupt), plan.run():
        plan.setup_done()
        with plan.step("flash_boot"), plan.check("ble_advertising"):
            raise KeyboardInterrupt
    cases = cases_of(report)
    assert cases.pop(("hitl_e2e", "rig"))[0] == "error"
    assert {v[0] for v in cases.values()} == {"skipped"}  # nothing failed against the device

    report, plan = plan_for()
    with pytest.raises(SystemExit), plan.run():
        plan.setup_done()
        with plan.step("flash_boot"):
            raise SystemExit(0)
    assert "failed" not in {v[0] for v in cases_of(report).values()}

    for code in (0, None):  # every check passed, then a clean exit inside the run: nothing to add
        report, plan = plan_for()
        with pytest.raises(SystemExit):
            full_run(plan, "cleanup", SystemExit(code))
        assert ("hitl_e2e", "rig") not in cases_of(report)
        assert {v[0] for v in cases_of(report).values()} == {"passed"} and len(report.cases) == 6

    for code in (1, "fatal"):  # every check passed, then a failing exit: listed, but no rig trouble
        report, plan = plan_for()
        with pytest.raises(SystemExit):
            full_run(plan, "cleanup", SystemExit(code))
        cases = cases_of(report)
        assert ("hitl_e2e", "rig") not in cases
        assert cases.pop(("hitl_e2e", "after_checks")) == ("error", [], f"SystemExit: {code}")
        assert {v[0] for v in cases.values()} == {"passed"} and len(cases) == 6

    report, plan = plan_for()
    with pytest.raises(SystemExit), plan.run():
        plan.setup_done()
        with plan.step("flash_boot"):
            raise SystemExit(3)  # a failing exit still counts against the device
    assert cases_of(report)[("hitl_e2e.flash_boot", "ble_advertising")][0] == "failed"
