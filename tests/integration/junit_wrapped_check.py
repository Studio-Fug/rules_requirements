# SPDX-License-Identifier: AGPL-3.0-or-later
"""Checks what the junit-format wrapper writes for ``:junit_runner`` under Bazel.

``junit_wrapped_test`` passes whenever the wrapper exits 0; this runs the same
wrapper over the same runner and asserts on its output: the runner's cases
copied with their requirement and level, and ``rr.file`` stripped of the
runfiles prefix (``tests/integration/junit_runner.py``).
"""

import os
import sys

from rules_requirements import ingest
from rules_requirements.hooks import wrap


def check(ok: bool, what: str) -> None:
    if not ok:
        raise SystemExit(f"junit_wrapped_check: {what}")


def main() -> int:
    out = os.path.join(os.environ["TEST_TMPDIR"], "wrapped.xml")
    # $(rootpaths :junit_runner): the launcher alone under Bazel 8; under
    # Bazel 7 the py_binary's .py source too, which is not the runner.
    runner = next(p for p in sys.argv[1:] if not p.endswith(".py"))
    rc = wrap.main(
        [
            "--format",
            "junit",
            "--junit-in",
            "${TEST_TMPDIR}/runner/junit.xml",
            "--junit-xml",
            out,
            "--target",
            "//tests/integration:junit_wrapped_test",
            "--",
            runner,
        ]
    )
    check(rc == 0, f"wrapper exited {rc}")
    cases = {f"{c.classname}::{c.name}": c for c in ingest.collect([out]).cases}
    check(set(cases) == {"bench.boot::banner", "bench.run::completed"}, f"cases {sorted(cases)}")
    banner, completed = cases["bench.boot::banner"], cases["bench.run::completed"]
    check(banner.status == completed.status == "passed", "a case did not pass")
    check(banner.requirements == ("REQ-1",) and completed.requirements == (), "requirements")
    check(banner.level == completed.level == "simulation", f"level {banner.level!r}")
    for case in (banner, completed):
        got = case.properties.get("rr.file")
        check(got == "tests/integration/junit_runner.py", f"rr.file {got!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
