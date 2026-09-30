# SPDX-License-Identifier: AGPL-3.0-or-later
"""`bazel run //:thermostat -- --setpoint 21.5 19.0 20.8 22.1`"""

from __future__ import annotations

import argparse

from thermostat.controller import Controller
from thermostat.display import format_setpoint


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--setpoint", type=float, required=True, help="target temperature in °C")
    parser.add_argument("readings", type=float, nargs="*", help="temperature readings in °C")
    args = parser.parse_args(argv)
    try:
        ctl = Controller(args.setpoint)
    except ValueError as exc:
        parser.error(str(exc))
    print(f"setpoint {format_setpoint(ctl.setpoint_c)}")
    for reading in args.readings:
        print(f"{reading:6.1f} °C -> heater {'ON' if ctl.update(reading) else 'off'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
