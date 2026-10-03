# SPDX-License-Identifier: AGPL-3.0-or-later
"""A harness that writes its own JUnit to a fixed path (rr_wrapped_test format = "junit").

It runs a two-step CheckPlan and writes the report to
``$TEST_TMPDIR/runner/junit.xml``, where the wrapper picks it up.
"""

import os

from rules_requirements.hooks.checkplan import CheckPlan
from rules_requirements.hooks.junit_writer import JUnitWriter


def main() -> int:
    report = JUnitWriter("bench", default_level="simulation")
    plan = CheckPlan(report, {"boot": ["banner"], "run": ["completed"]}, tags={"boot.banner": "REQ-1"})
    with plan.run():
        plan.setup_done()
        with plan.step("boot"), plan.check("banner"):
            assert "ready" in "bench ready"
        with plan.step("run"):
            plan.passed("completed")
    out = os.path.join(os.environ["TEST_TMPDIR"], "runner", "junit.xml")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    report.write(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
