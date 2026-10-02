# SPDX-License-Identifier: AGPL-3.0-or-later
"""A hardware run as ordered steps and single-owner checks.

On-hardware (HITL) harnesses are usually a sequence of *steps* — flash, boot,
provision, connect — each followed by assertions. A step is an action: it
verifies nothing by itself and is owned by no requirement. A *check* is one
assertion and becomes one JUnit case, ``<suite>.<step>::<check>``, which
verifies at most one requirement::

    from rules_requirements.hooks.checkplan import CheckPlan
    from rules_requirements.hooks.junit_writer import JUnitWriter

    report = JUnitWriter("hitl_e2e", default_level="hitl")
    plan = CheckPlan(
        report,
        {"flash_boot": ["ble_advertising"], "websocket_checks": ["ws_connect", "rename"]},
        tags={"flash_boot.ble_advertising": "REQ-13", "websocket_checks.rename": "REQ-35"},
        is_infrastructure=lambda exc: isinstance(exc, RigError),
    )
    try:
        with plan.run():
            reserve_rig()                  # rig/setup trouble until setup_done()
            plan.setup_done()
            with plan.step("flash_boot"):
                flash(dut)                 # a failure here is the device's
                with plan.check("ble_advertising"):
                    assert BLE_MARKER in serial_log()
            with plan.step("websocket_checks"):
                ...
    finally:
        report.write(os.environ["XML_OUTPUT_FILE"])

How a run that stops early is recorded:

* **Device failure** (any exception after :meth:`CheckPlan.setup_done` that
  ``is_infrastructure`` does not claim): a check that raised is failed with the
  exception, and every planned check that has not run — the rest of the
  failing step and every later step — is failed as ``not reached``, each
  through its own case and requirement.
* **Rig or setup trouble** (an exception before ``setup_done()``, or one
  ``is_infrastructure`` claims): one untagged ``<suite>::rig`` error case, and
  every planned check that has not run is skipped (``not run: rig trouble``).
  Nothing the device did is failed, and the checks that never ran keep their
  requirements from reading verified.
* **Harness bug** (an unknown step or check name, or a planned check never
  executed by a run that otherwise ended normally): the check is recorded as
  ``error``, and unknown names as an untagged ``<suite>::harness`` error.

*v0.2 compatibility.* Until per-requirement verification sets land (0.3),
skipped evidence does not stop a requirement reading VERIFIED. So on rig
trouble a CheckPlan also withdraws the tag of every passed check whose
requirement is also the tag of a check that did not run: the requirement reads
neither VERIFIED nor FAILED from a partial run, as before.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Callable, Iterator, Mapping, NoReturn, Sequence

from rules_requirements.hooks.ids import check_id
from rules_requirements.hooks.junit_writer import JUnitWriter, _Case


class HarnessError(ValueError):
    """The harness used its CheckPlan wrongly (an unknown step or check name,
    a check outside a step, a check recorded twice): a bug in the harness,
    not in the device or the rig."""


def _never(exc: BaseException) -> bool:
    return False


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


class CheckPlan:
    """A hardware run as ordered steps (actions, owned by nobody) and checks
    (assertions, one JUnit case each, at most one requirement each).

    Args:
      writer: the :class:`~rules_requirements.hooks.junit_writer.JUnitWriter`
        the cases go to; its ``suite`` prefixes every case's classname.
      steps: step name -> its check names, in run order. Check names are
        unique within a step.
      tags: ``"<step>.<check>"`` -> the ONE requirement id that check
        verifies (a declared tag; the model may also claim the case).
      is_infrastructure: whether an exception is rig or setup trouble rather
        than the device's failure. Anything it does not claim after
        :meth:`setup_done` counts against the device.
    """

    def __init__(
        self,
        writer: JUnitWriter,
        steps: Mapping[str, Sequence[str]],
        *,
        tags: Mapping[str, str] | None = None,
        is_infrastructure: Callable[[BaseException], bool] = _never,
    ) -> None:
        self.writer = writer
        self.suite = writer.suite
        self.steps: dict[str, tuple[str, ...]] = {}
        for step, checks in steps.items():
            if isinstance(checks, str):
                raise TypeError(f"CheckPlan: step {step!r}: checks must be a sequence of names, not a string")
            names = tuple(checks)
            dupes = sorted({c for c in names if names.count(c) > 1})
            if dupes:
                raise ValueError(f"CheckPlan: step {step!r} plans check(s) {', '.join(dupes)} more than once")
            self.steps[step] = names
        planned = {f"{s}.{c}" for s, cs in self.steps.items() for c in cs}
        self.tags: dict[str, str] = {}
        for key, rid in (tags or {}).items():
            if key not in planned:
                raise ValueError(f"CheckPlan: tag for {key!r}, which is not a planned '<step>.<check>'")
            self.tags[key] = check_id(rid, f"CheckPlan tag {key!r}")
        self._infrastructure = is_infrastructure
        self._recorded: dict[tuple[str, str], _Case] = {}
        self._setup_done = False
        self._step: str | None = None
        self._stopped_in: str | None = None  # the step an exception left
        self._completed: set[str] = set()  # steps that ended normally
        self._finished = False

    # -- the run -----------------------------------------------------------

    def setup_done(self) -> None:
        """Mark the end of setup (rig reserved, credentials and bundle checked).

        From here on, a stop is the device's unless ``is_infrastructure``
        claims it. Entering a step implies it.
        """
        self._setup_done = True

    @contextmanager
    def run(self) -> Iterator[CheckPlan]:
        """Wrap the whole run: a normal end calls :meth:`finish`; an exception
        is recorded as a device failure, rig trouble or harness bug (see the
        module docs) and re-raised."""
        try:
            yield self
        except BaseException as exc:
            self._stop(exc)
            raise
        self.finish()

    @contextmanager
    def step(self, name: str) -> Iterator[None]:
        """Run one step. The step itself records nothing; its checks do."""
        if self._step is not None:
            self._harness_bug(f"step {name!r} entered inside step {self._step!r}")
        if name not in self.steps:
            self._harness_bug(f"unknown step {name!r}")
        self.setup_done()
        self._step = name
        try:
            yield
        except BaseException:
            if self._stopped_in is None:
                self._stopped_in = name
            raise
        finally:
            self._step = None
        self._completed.add(name)

    @contextmanager
    def check(self, name: str) -> Iterator[None]:
        """Run one check of the current step: passed if the block completes,
        failed (then re-raised) if it raises; rig trouble records nothing."""
        step, check = self._resolve(name, current_only=True)
        start = time.monotonic()
        try:
            yield
        except BaseException as exc:
            if not isinstance(exc, HarnessError) and not self._is_infrastructure(exc):
                self._record(step, check, "failed", _describe(exc), time.monotonic() - start)
            raise
        self._record(step, check, "passed", "", time.monotonic() - start)

    def passed(self, name: str, message: str = "") -> None:
        """Record check ``name`` (of the current step, or ``"<step>.<check>"``) as passed."""
        step, check = self._resolve(name)
        self._record(step, check, "passed", message)

    def failed(self, name: str, message: str) -> None:
        """Record check ``name`` as failed (the device's failure); does not raise."""
        step, check = self._resolve(name)
        self._record(step, check, "failed", message)

    def skipped(self, name: str, reason: str) -> None:
        """Record check ``name`` as deliberately skipped (it verifies nothing this run)."""
        step, check = self._resolve(name)
        self._record(step, check, "skipped", reason)

    def finish(self) -> None:
        """End a run that completed: every planned check never recorded is a
        harness bug, recorded as ``error``. Idempotent."""
        if self._finished:
            return
        self._finished = True
        for step, check in self.pending():
            self._record(step, check, "error", "planned check never executed (harness bug)")

    def pending(self) -> list[tuple[str, str]]:
        """Planned ``(step, check)`` pairs not recorded yet, in run order."""
        return [(s, c) for s, cs in self.steps.items() for c in cs if (s, c) not in self._recorded]

    # -- internals ---------------------------------------------------------

    def _is_infrastructure(self, exc: BaseException) -> bool:
        return not self._setup_done or bool(self._infrastructure(exc))

    def _stop(self, exc: BaseException) -> None:
        if self._finished:
            return
        self._finished = True
        failure = _describe(exc)
        pending = self.pending()
        if isinstance(exc, HarnessError):
            for step, check in pending:
                self._record(step, check, "error", f"planned check never executed (harness bug: {exc})")
        elif self._is_infrastructure(exc):
            self.writer._append("rig", (), "error", failure, classname=self.suite)
            for step, check in pending:
                self._record(step, check, "skipped", f"not run: rig trouble: {failure}")
            self._withdraw_partial(pending)
        else:
            where = self._stopped_in or "the run"
            for step, check in pending:
                if step in self._completed:  # its step ended without running it
                    self._record(step, check, "error", "planned check never executed (harness bug)")
                else:
                    self._record(step, check, "failed", f"not reached: {where} failed: {failure}")
            if not pending and not any(c.status in ("failed", "error") for c in self._recorded.values()):
                # Every check had passed (e.g. a cleanup step failed): no
                # requirement is affected, but the stop is listed.
                self.writer._append("after_checks", (), "error", failure, classname=self.suite)

    def _withdraw_partial(self, pending: list[tuple[str, str]]) -> None:
        # v0.2 only: a passed check must not verify a requirement that a check
        # which never ran also verifies (0.3's verification sets make this
        # INCOMPLETE by themselves).
        unrun = {self.tags[f"{s}.{c}"] for s, c in pending if f"{s}.{c}" in self.tags}
        for case in self._recorded.values():
            if case.status == "passed" and case.requirement in unrun:
                case.requirements = []

    def _resolve(self, name: str, current_only: bool = False) -> tuple[str, str]:
        """``(step, check)`` for a check of the current step, or (unless
        ``current_only``) a ``"<step>.<check>"`` name; a harness bug otherwise."""
        found: tuple[str, str] | None = None
        if self._step is not None and name in self.steps[self._step]:
            found = (self._step, name)
        elif not current_only:
            found = next(
                (
                    (s, name[len(s) + 1 :])
                    for s, cs in self.steps.items()
                    if name[len(s) + 1 :] in cs and name.startswith(f"{s}.")
                ),
                None,
            )
        if found is None:
            if self._step is None and "." not in name:
                self._harness_bug(f"check {name!r} outside a step")
            self._harness_bug(f"unknown check {name!r}" + (f" in step {self._step!r}" if self._step else ""))
        if found in self._recorded:
            self._harness_bug(f"check {found[0]}.{found[1]} recorded twice")
        return found

    def _record(self, step: str, check: str, status: str, message: str, duration: float = 0.0) -> None:
        tag = self.tags.get(f"{step}.{check}")
        self._recorded[(step, check)] = self.writer._append(
            check, [tag] if tag else [], status, message, duration, classname=f"{self.suite}.{step}"
        )

    def _harness_bug(self, message: str) -> NoReturn:
        self.writer._append("harness", (), "error", f"harness bug: {message}", classname=self.suite)
        raise HarnessError(f"CheckPlan: {message}")
