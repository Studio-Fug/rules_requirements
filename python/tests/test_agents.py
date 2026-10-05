# SPDX-License-Identifier: AGPL-3.0-or-later
import json
import os
import pathlib
import subprocess
import sys
from types import SimpleNamespace

import pytest
from conftest import MODEL, junit, write

from rules_requirements.agents import Finding, JobManager
from rules_requirements.agents import llm as rr_llm
from rules_requirements.agents.workflows import WORKFLOWS, Context, linked_tests
from rules_requirements.case_keys import CaseKey
from rules_requirements.server.app import Api, HttpError
from rules_requirements.server.workspace import Workspace


class FakeLLM:
    name = "fake"

    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts = []

    def json(self, system, prompt, schema):
        self.prompts.append((system, prompt, schema))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def proposal(kind="none", **kw):
    base = {
        "kind": kind,
        "title": "",
        "description": "",
        "satisfies": [],
        "refines": [],
        "mitigates": [],
        "implemented_by": [],
        "severity": "",
        "likelihood": "",
        "method": "",
        "type": "",
    }
    base.update(kw)
    return base


@pytest.fixture
def ws(tmp_path):
    write(tmp_path, "req/model.yaml", MODEL)
    write(
        tmp_path,
        "src/ctl.py",
        "# @rr(REQ-1): heats\ndef heat(t):\n    if t < 20:\n        return True\n    return False\n\n\nOTHER = 1\n",
    )
    write(
        tmp_path,
        "tests/test_ctl.py",
        "import pytest\n\n\n@pytest.mark.rr('REQ-1')\ndef test_heat():\n    assert heat(10)\n\n\ndef test_unrelated():\n    pass\n",
    )
    write(
        tmp_path,
        "cc/lock_test.cc",
        'TEST(Interlock, Trips) {\n  RR_VERIFIES("REQ-3");\n  EXPECT_FALSE(allowed(40));\n}\n',
    )
    junit(tmp_path, "logs/t/test.xml", [("test_heat", "passed", ["REQ-1"], "")])
    xml = (tmp_path / "logs/t/test.xml").read_text().replace('classname="suite"', 'classname="tests.test_ctl"')
    (tmp_path / "logs/t/test.xml").write_text(xml)
    return Workspace(root=str(tmp_path), model_paths=["req"], evidence_paths=["logs"])


def ctx_of(ws):
    snap = ws.snapshot()
    return Context(model=snap.model, matrix=snap.matrix, root=ws.root, references=snap.references, issues=snap.issues)


def run(ws, workflow, llm, **params):
    mgr = JobManager(WORKFLOWS, lambda job: ctx_of(ws), llm)
    job = mgr.start(workflow, params, wait=True)
    assert job.status == "done", job.log
    return job


def test_context_snippets_and_linked_tests(ws):
    ctx = ctx_of(ws)
    snip = ctx.snippet("src/ctl.py", 1)
    assert "def heat(t):" in snip and "return False" in snip and "OTHER" not in snip
    cc = ctx.snippet("cc/lock_test.cc", 1)
    assert cc.splitlines()[-1].endswith("}")
    assert ctx.snippet("missing.py", 1) == "" and ctx.lines("../../etc/passwd") == []
    tests = linked_tests(ctx, "REQ-1")
    assert tests and "def test_heat" in tests[0][1]
    assert "REQ-3" in ctx.digest() and "[UNVERIFIED]" in ctx.digest()
    case = SimpleNamespace(name="Trips", classname="Interlock")
    assert ctx.locate_case(case) == ("cc/lock_test.cc", 1)
    assert ctx.locate_case(SimpleNamespace(name="nothing_here", classname="")) is None


def test_completeness_rule_only_and_with_llm(ws):
    job = run(ws, "completeness", None)
    kinds = {f.category for f in job.findings}
    assert "trace:unverified" in kinds and all(f.source == "rule" for f in job.findings)
    llm = FakeLLM(
        [
            {
                "findings": [
                    {
                        "severity": "warning",
                        "category": "missing-requirement",
                        "entity": "UN-1",
                        "refs": ["REQ-1"],
                        "title": "No requirement for cooling",
                        "detail": "d",
                        "proposal": proposal(
                            "requirement",
                            title="Report when heating cannot keep up",
                            satisfies=["UN-1", "UN-404"],
                            method="warp",
                        ),
                    },
                    {
                        "severity": "bogus",
                        "category": "x",
                        "entity": "NOPE-1",
                        "refs": [],
                        "title": "",
                        "detail": "",
                        "proposal": proposal(),
                    },
                ]
            }
        ]
    )
    job = run(ws, "completeness", llm)
    llm_findings = [f for f in job.findings if f.source == "llm"]
    assert llm_findings[0].proposal == {
        "kind": "requirement",
        "data": {"title": "Report when heating cannot keep up", "satisfies": ["UN-1"]},
    }
    assert (
        llm_findings[1].severity == "info"
        and llm_findings[1].entity == ""
        and llm_findings[1].title == "(untitled finding)"
    )
    assert "REQ-1" in llm.prompts[0][1]
    job = run(ws, "completeness", FakeLLM([rr_llm.LLMError("boom")]))
    assert any("coverage review failed" in line for line in job.log)
    job = run(ws, "completeness", FakeLLM([]), use_llm=False)
    assert all(f.source == "rule" for f in job.findings)


def test_test_adequacy(ws):
    llm = FakeLLM(
        [
            {
                "tests": [{"test": "test_heat", "verdict": "partially", "rationale": "only checks one temperature"}],
                "missing_checks": ["hysteresis band"],
                "summary": "weak",
            },
        ]
    )
    job = run(ws, "test_adequacy", llm, entities=["REQ-1"])
    cats = [f.category for f in job.findings]
    assert cats == ["test-adequacy:partially", "test-adequacy:missing-checks"]
    assert "def test_heat" in llm.prompts[0][1] and "Heat below setpoint" in llm.prompts[0][1]
    job = run(
        ws,
        "test_adequacy",
        FakeLLM(
            [{"tests": [{"test": "t", "verdict": "proves", "rationale": ""}], "missing_checks": [], "summary": "ok"}]
        ),
        entities=["REQ-1"],
    )
    assert [f.category for f in job.findings] == ["test-adequacy:ok"]
    job = run(ws, "test_adequacy", FakeLLM([rr_llm.LLMError("x")]), entities=["REQ-1", "REQ-2"])
    assert job.findings == [] and any("no linked test" in line for line in job.log)


def test_implementation_review(ws):
    job = run(
        ws,
        "implementation_review",
        FakeLLM([{"verdict": "partially", "rationale": "no hysteresis", "missing_behavior": ["off above band"]}]),
    )
    assert [(f.entity, f.category) for f in job.findings] == [("REQ-1", "implementation:partially")]
    assert "off above band" in job.findings[0].detail
    job = run(
        ws, "implementation_review", FakeLLM([{"verdict": "implements", "rationale": "", "missing_behavior": []}])
    )
    assert job.findings == []


def test_mitigation_adequacy_and_risk_discovery(ws):
    llm = FakeLLM(
        [
            {
                "mitigations": [
                    {"mitigation": "MIT-1", "verdict": "partially-effective", "rationale": "no sensor check"}
                ],
                "residual_assessment": "optimistic",
                "findings": [
                    {
                        "severity": "warning",
                        "category": "missing-control",
                        "entity": "",
                        "refs": [],
                        "title": "Add sensor plausibility",
                        "detail": "",
                        "proposal": proposal("requirement", title="Turn heater off on implausible readings"),
                    }
                ],
            },
        ]
    )
    job = run(ws, "mitigation_adequacy", llm)
    cats = [(f.category, f.entity) for f in job.findings]
    assert cats == [
        ("mitigation:partially-effective", "MIT-1"),
        ("mitigation:residual", "RISK-1"),
        ("missing-control", "RISK-1"),
    ]
    assert "Interlock bench test" not in llm.prompts[0][1] and "MIT-1" in llm.prompts[0][1]
    llm = FakeLLM(
        [
            {
                "findings": [
                    {
                        "severity": "warning",
                        "category": "hazard",
                        "entity": "",
                        "refs": [],
                        "title": "Fire",
                        "detail": "",
                        "proposal": proposal(
                            "risk", title="Heater ignites nearby material", severity="catastrophic", likelihood="rare"
                        ),
                    }
                ]
            }
        ]
    )
    job = run(ws, "risk_discovery", llm)
    assert job.findings[0].proposal == {
        "kind": "risk",
        "data": {"title": "Heater ignites nearby material", "likelihood": "rare"},
    }


def test_assistant_operations(ws):
    llm = FakeLLM(
        [
            {
                "reply": "Added a logging requirement.",
                "operations": [
                    {
                        "op": "create",
                        "id": "",
                        "rationale": "audit",
                        "note": "",
                        "entity": proposal("requirement", title="Log setpoint changes", satisfies=["UN-2"]),
                    },
                    {
                        "op": "update",
                        "id": "REQ-2",
                        "rationale": "clarify",
                        "note": "",
                        "entity": proposal(
                            "requirement", title="Accept 5-30 C setpoints inclusive", satisfies=["UN-2"]
                        ),
                    },
                    {
                        "op": "update",
                        "id": "REQ-2",
                        "rationale": "wrong kind",
                        "note": "",
                        "entity": proposal("risk", title="x"),
                    },
                    {
                        "op": "note",
                        "id": "REQ-1",
                        "rationale": "",
                        "note": "Is 0.5 C band right?",
                        "entity": proposal(),
                    },
                    {"op": "create", "id": "", "rationale": "", "note": "", "entity": proposal()},
                ],
            }
        ]
    )
    job = run(ws, "assistant", llm, instruction="add logging", focus=["REQ-2", "NOPE"])
    assert job.result == {"reply": "Added a logging requirement."}
    assert [f.category for f in job.findings] == ["assistant:create", "assistant:update", "assistant:note"]
    api = Api(ws, llm=FakeLLM([]))
    api.jobs.jobs[job.id] = job
    for f in job.findings:
        api.dispatch(
            "POST",
            f"/api/findings/{f.id}/apply",
            {},
            {"action": "note"} if f.category == "assistant:note" else {"action": "create"},
        )
    m = ws.model
    assert m.requirements["REQ-4"].title == "Log setpoint changes"
    assert m.requirements["REQ-2"].title == "Accept 5-30 C setpoints inclusive"
    assert m.requirements["REQ-1"].notes[0].author == "rr-agent/assistant"


def test_job_failures_and_ids(ws):
    mgr = JobManager(WORKFLOWS, lambda job: ctx_of(ws), FakeLLM([]))
    job = mgr.start("assistant", {"instruction": ""}, wait=True)
    assert job.status == "failed" and "instruction is required" in job.error
    with pytest.raises(KeyError):
        mgr.start("nope")
    ok = mgr.start("completeness", {"use_llm": False}, wait=True)
    assert ok.findings[0].id == f"{ok.id}-1"
    assert mgr.finding(ok.findings[0].id)[1] is ok.findings[0]
    with pytest.raises(rr_llm.LLMError):
        JobManager(WORKFLOWS, lambda job: None, None).start("risk_discovery")
    assert Finding("w", "info", "c", "t").to_dict()["status"] == "open"


class _Stream:
    def __init__(self, message):
        self.message = message

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return self.message


class _FakeClient:
    def __init__(self, message=None, error=None):
        self.calls = []
        outer = self

        class Messages:
            def stream(self, **kwargs):
                outer.calls.append(kwargs)
                if error:
                    raise error
                return _Stream(message)

        self.beta = SimpleNamespace(messages=Messages())


def _msg(stop="end_turn", text='{"ok": true}', details=None):
    return SimpleNamespace(
        stop_reason=stop,
        stop_details=details,
        content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
    )


def test_claude_llm_request_shape_and_errors(monkeypatch):
    monkeypatch.delenv("RR_AGENT_MODEL", raising=False)
    monkeypatch.delenv("RR_AGENT_EFFORT", raising=False)
    client = _FakeClient(_msg())
    llm = rr_llm.ClaudeLLM(client=client)
    assert llm.json("sys", "prompt", {"type": "object"}) == {"ok": True}
    call = client.calls[0]
    assert call["model"] == "claude-opus-5-5" and call["thinking"] == {"type": "adaptive"}
    assert call["output_config"] == {"effort": "high", "format": {"type": "json_schema", "schema": {"type": "object"}}}
    assert call["betas"] == ["server-side-fallback-2026-07-01"] and call["fallbacks"] == "default"
    assert call["system"] == "sys" and call["messages"] == [{"role": "user", "content": "prompt"}]
    with pytest.raises(rr_llm.LLMError, match="declined"):
        rr_llm.ClaudeLLM(client=_FakeClient(_msg("refusal", "", SimpleNamespace(category="cyber")))).json("s", "p", {})
    with pytest.raises(rr_llm.LLMError, match="truncated"):
        rr_llm.ClaudeLLM(client=_FakeClient(_msg("max_tokens"))).json("s", "p", {})
    with pytest.raises(rr_llm.LLMError, match="invalid JSON"):
        rr_llm.ClaudeLLM(client=_FakeClient(_msg(text="nope"))).json("s", "p", {})
    with pytest.raises(rr_llm.LLMError, match="RuntimeError"):
        rr_llm.ClaudeLLM(client=_FakeClient(error=RuntimeError("down"))).json("s", "p", {})
    # What anthropic 1.11 raises when it finds no credentials at request time.
    no_auth = TypeError(
        '"Could not resolve authentication method. Expected one of api_key, auth_token, or credentials to be set. '
        'Or for one of the `X-Api-Key` or `Authorization` headers to be explicitly omitted"'
    )
    with pytest.raises(rr_llm.LLMError, match="no usable Claude credentials: set ANTHROPIC_API_KEY"):
        rr_llm.ClaudeLLM(client=_FakeClient(error=no_auth)).json("s", "p", {})
    monkeypatch.setenv("RR_AGENT_MODEL", "claude-sonnet-5-5")
    assert rr_llm.ClaudeLLM(client=client, effort="low").name == "claude-sonnet-5-5 (effort low)"


def test_a_broken_credentials_profile_leaves_the_llm_unavailable(monkeypatch):
    # e.g. ANTHROPIC_PROFILE naming a profile that does not exist: the SDK fails in the constructor.
    class CredentialsError(Exception):
        pass

    def broken():
        raise CredentialsError("Config file not found at ~/.config/anthropic/configs/missing.json (profile 'missing')")

    monkeypatch.setitem(sys.modules, "anthropic", SimpleNamespace(Anthropic=broken))
    assert rr_llm.default_llm() is None  # rr serve still starts
    status = rr_llm.llm_status(None)
    assert status["available"] is False and "CredentialsError: Config file not found" in status["reason"]


def test_llm_availability(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def no_anthropic(name, *a, **kw):
        if name == "anthropic":
            raise ImportError(name)
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_anthropic)
    assert rr_llm.default_llm() is None
    assert "pip install" in rr_llm.llm_status(None)["reason"]
    assert rr_llm.default_llm(enabled=False) is None
    assert "disabled" in rr_llm.llm_status(None, enabled=False)["reason"]
    assert rr_llm.llm_status(FakeLLM([]))["available"] is True
    with pytest.raises(rr_llm.LLMUnavailable):
        rr_llm.ClaudeLLM()


def test_evidence_mapping_used_by_workflows(ws):
    assert json.dumps(ctx_of(ws).matrix.verdicts["REQ-1"].status) == '"VERIFIED"'
    assert subprocess.run(["true"]).returncode == 0


# --------------------------------------------------------------------------- #
# Agents read the attribution, and only propose case owners (P26, P31)        #
# --------------------------------------------------------------------------- #

OWNED_MODEL = MODEL.replace(
    "  - id: REQ-3\n    title: Cut the heater at 35 C\n",
    "  - id: REQ-3\n    title: Cut the heater at 35 C\n"
    "    verified_by: [{target: //t:agent_test, cases: ['tests.test_ctl::test_unrelated']}]\n",
)


@pytest.fixture
def owned_ws(tmp_path):
    write(tmp_path, "req/model.yaml", OWNED_MODEL)
    write(
        tmp_path,
        "tests/test_ctl.py",
        "def test_heat():\n    assert heat(10)\n\n\ndef test_unrelated():\n    assert cut(40)\n\n\n"
        "def test_both():\n    assert heat(10) and accept(20)\n\n\ndef test_orphan():\n    assert accept(5)\n",
    )

    def case(name, *ids):
        props = "".join(f'<property name="requirement" value="{i}"/>' for i in ids)
        return (
            f'<testcase classname="tests.test_ctl" name="{name}" file="tests/test_ctl.py">'
            f"<properties>{props}</properties></testcase>"
        )

    xml = (
        case("test_heat", "REQ-1")  # hybrid: its tag owns it
        + case("test_unrelated", "REQ-2")  # REQ-3 claims it: the model wins (tag-mismatch)
        + case("test_both", "REQ-1", "REQ-2")  # multi-tag: quarantined
        + case("test_orphan")  # nobody's
    )
    write(
        tmp_path,
        "bazel-testlogs/t/agent_test/test.xml",
        f'<?xml version="1.0"?><testsuites><testsuite name="s">{xml}</testsuite></testsuites>',
    )
    return Workspace(root=str(tmp_path), model_paths=["req"], evidence_paths=["bazel-testlogs"], scan=False)


def _no_raw_tags(monkeypatch):
    from rules_requirements import ingest

    def boom(self, entity_id):
        raise AssertionError(f"an agent read raw tags: Evidence.for_id({entity_id!r})")

    monkeypatch.setattr(ingest.Evidence, "for_id", boom)


def test_agent_context_is_the_verification_set_never_the_tags(owned_ws, monkeypatch):
    _no_raw_tags(monkeypatch)
    ctx = ctx_of(owned_ws)
    labels = lambda rid: [label for label, _ in linked_tests(ctx, rid)]  # noqa: E731
    assert [x.split(" (")[0] for x in labels("REQ-1")] == ["//t:agent_test#tests.test_ctl::test_heat"]
    # REQ-2 is tagged on test_unrelated (owned by REQ-3) and on the quarantined test_both: no test of it.
    assert labels("REQ-2") == []
    assert [x.split(" (")[0] for x in labels("REQ-3")] == ["//t:agent_test#tests.test_ctl::test_unrelated"]
    assert "set 0/1 passed · 1 quarantined" in ctx.set_line("REQ-2")
    assert "REQ-2 [INVALID; set 0/1 passed · 1 quarantined]" in ctx.digest()
    llm = FakeLLM([{"tests": [], "missing_checks": [], "summary": ""}] * 3)
    run(owned_ws, "test_adequacy", llm)
    prompts = "\n".join(p for _, p, _ in llm.prompts)
    assert "test_unrelated" in prompts and "verification set:" in prompts
    reviewed = [p for _, p, _ in llm.prompts if p.split("\n")[1].startswith("REQ-2")]
    assert reviewed == []  # REQ-2 has no test to review: skipped, not fed its tags


def test_agent_proposals_never_carry_ownership(owned_ws):
    from rules_requirements.agents.workflows import to_proposal

    raw = {
        "kind": "requirement",
        "title": "t",
        "verified_by": [{"target": "//t:agent_test", "cases": ["*"]}],
        "validated_by": ["//x:y"],
    }
    prop = to_proposal(raw, owned_ws.model)
    assert prop == {"kind": "requirement", "data": {"title": "t"}}


def test_assign_cases_proposes_one_owner_into_the_worksheet(owned_ws, monkeypatch):
    _no_raw_tags(monkeypatch)
    model_text = pathlib.Path(os.path.join(owned_ws.root, "req/model.yaml")).read_text(encoding="utf-8")
    both = "//t:agent_test#tests.test_ctl::test_both"
    orphan = "//t:agent_test#tests.test_ctl::test_orphan"
    llm = FakeLLM(
        [
            {
                "assignments": [
                    {"case": both, "owner": "REQ-1", "rationale": "asserts heating"},
                    {"case": orphan, "owner": "REQ-1, REQ-2", "rationale": "greedy"},  # never two
                    {"case": both, "owner": "REQ-2", "rationale": "second answer ignored"},
                    {"case": "//t:x#y", "owner": "REQ-2", "rationale": "not asked"},
                ]
            }
        ]
    )
    job = run(owned_ws, "assign_cases", llm, worksheet="plans/attribution.rrplan")
    (f,) = job.findings
    assert f.proposal["kind"] == "case_owner" and f.proposal["case"] == both and f.proposal["owner"] == "REQ-1"
    assert f.refs == ["REQ-2"] and f.status == "applied"
    _, prompt, schema = llm.prompts[0]
    assert both in prompt and orphan in prompt and "quarantined (multi-tag)" in prompt and "def test_both" in prompt
    enum = schema["properties"]["assignments"]["items"]["properties"]["owner"]["enum"]
    assert "none" in enum and "RISK-1" not in enum and "TM-1" not in enum
    assert any("not one entity id" in line for line in job.log)
    # The worksheet holds a proposal; the decision stays open, the model untouched.
    from rules_requirements import migrate

    doc = migrate.load_worksheet(os.path.join(owned_ws.root, "plans/attribution.rrplan"))
    assert migrate.check_worksheet(doc, owned_ws.model) == []
    (group,) = doc["groups"]
    (entry,) = group["cases"]
    assert entry["path"] == "tests.test_ctl::test_both" and entry["proposed"] == "REQ-1"
    assert entry["proposed_by"] == "rr-agent/assign_cases" and "owner" not in entry and group["owner"] == "?"
    assert migrate.decisions(doc) == {CaseKey("//t:agent_test", "tests.test_ctl::test_both"): "?"}
    assert pathlib.Path(os.path.join(owned_ws.root, "req/model.yaml")).read_text(encoding="utf-8") == model_text
    # Its open rows are what a second run assigns.
    from rules_requirements.agents.workflows import cases_to_assign

    keys = [str(k) for k, _ in cases_to_assign(ctx_of(owned_ws), worksheet="plans/attribution.rrplan")]
    assert keys == [both]
    assert {str(k) for k, _ in cases_to_assign(ctx_of(owned_ws))} == {both, orphan}


def test_a_case_owner_finding_only_goes_to_the_worksheet(owned_ws):
    api = Api(owned_ws, llm=FakeLLM([]), author="Ada <ada@x>")
    orphan = "//t:agent_test#tests.test_ctl::test_orphan"
    api.llm.answers.append({"assignments": [{"case": orphan, "owner": "REQ-2", "rationale": "accepts 5 C"}]})
    job = api.dispatch("POST", "/api/agents/run", {}, {"workflow": "assign_cases", "params": {}, "wait": True})
    (f,) = job["findings"]
    assert f["status"] == "open" and f["proposal"]["owner"] == "REQ-2"
    for action in ("update", "create"):
        with pytest.raises(HttpError) as exc:
            api.dispatch("POST", f"/api/findings/{f['id']}/apply", {}, {"action": action})
        assert exc.value.status == 400 and "worksheet" in str(exc.value)
    with pytest.raises(HttpError):
        api.dispatch("POST", f"/api/findings/{f['id']}/apply", {}, {"action": "worksheet", "worksheet": "../x.rrplan"})
    r = api.dispatch("POST", f"/api/findings/{f['id']}/apply", {}, {"action": "worksheet"})
    assert r["worksheet"] == "attribution.rrplan" and r["proposed"] == "REQ-2"
    text = pathlib.Path(os.path.join(owned_ws.root, "attribution.rrplan")).read_text(encoding="utf-8")
    assert "proposed: REQ-2" in text and "rr-agent/assign_cases" in text
    snap = owned_ws.snapshot()
    assert snap.attribution.owner_of(CaseKey.parse(orphan)) is None  # still nobody's: a person decides


def test_applying_an_agent_update_cannot_change_claims(owned_ws):
    api = Api(owned_ws, author="Ada <ada@x>")
    job = api.dispatch(
        "POST", "/api/agents/run", {}, {"workflow": "completeness", "params": {"use_llm": False}, "wait": True}
    )
    finding = Finding(
        "assistant",
        "info",
        "assistant:update",
        "t",
        entity="REQ-3",
        proposal={"kind": "requirement", "op": "update", "data": {"title": "Cut the heater at 35 C, always"}},
        source="llm",
    )
    finding.id = "x-1"
    api.jobs.jobs[job["id"]].findings.append(finding)
    steal = {"title": "x", "verified_by": [{"target": "//t:agent_test", "cases": ["*"]}]}
    with pytest.raises(HttpError) as exc:
        api.dispatch("POST", "/api/findings/x-1/apply", {}, {"action": "update", "data": steal})
    assert exc.value.status == 409 and "verified_by" in str(exc.value)
    r = api.dispatch("POST", "/api/findings/x-1/apply", {}, {"action": "update"})
    assert r["entity"]["data"]["verified_by"] == [
        {"target": "//t:agent_test", "cases": ["tests.test_ctl::test_unrelated"]}
    ]
