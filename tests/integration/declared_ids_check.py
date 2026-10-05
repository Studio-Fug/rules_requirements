# SPDX-License-Identifier: AGPL-3.0-or-later
"""Quarantine inputs: what the hooks declare for the integration evidence.

A test case verifies at most one requirement. The two deprecated multi-id
cases (``rust_hook_test::multiple_ids`` and
``GtestHook::IdsAccumulateAcrossCalls``) must reach the evidence with EVERY id
they name, so attribution quarantines them (they verify none of those
requirements); no other case may declare more than one id. A target-scope
``exit-status`` case (``leaky_wrapped_test`` passes its only test, then exits
1) declares no id at all, while the passing case keeps its own one. Whether
the quarantine itself happens is attribution's job, asserted where it runs.
"""

import sys

from rules_requirements import ingest

MULTI_ID = {
    "rust_hook_test::multiple_ids": {"REQ-4", "REQ-1"},
    "GtestHook::IdsAccumulateAcrossCalls": {"REQ-3", "REQ-1"},
}


def check(ok: bool, what: str) -> None:
    if not ok:
        raise SystemExit(f"declared_ids_check: {what}")


def main(argv: list) -> int:
    cases = {f"{c.classname}::{c.name}" if c.classname else c.name: c for c in ingest.collect(argv).cases}
    for name, ids in MULTI_ID.items():
        check(name in cases, f"{name} is missing from the evidence ({sorted(cases)})")
        got = set(cases[name].declared)
        check(got == ids, f"{name} declares {sorted(got)}, expected every id it names: {sorted(ids)}")
    for name, case in cases.items():
        if name not in MULTI_ID:
            check(len(case.declared) <= 1, f"{name} declares {list(case.declared)}: more than one id")
        if case.name == "exit-status":
            check(not case.declared, f"{name}: a target-scope exit-status case declares no requirement")
            check(case.properties.get("rr.scope") == "target", f"{name} is not target-scope: {case.properties}")
    exits = [c for c in cases.values() if c.name == "exit-status"]
    check(len(exits) == 1, f"expected one exit-status case (leaky_wrapped_test), got {len(exits)}")
    leaky = [c for c in cases.values() if c.name == "passes" and c.classname == "leaky"]
    check(len(leaky) == 1 and leaky[0].declared == ("REQ-1",), f"leaky::passes: {leaky}")
    check(leaky[0].status == "passed", f"leaky::passes is {leaky[0].status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
