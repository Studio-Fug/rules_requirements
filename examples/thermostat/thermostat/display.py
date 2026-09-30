# SPDX-License-Identifier: AGPL-3.0-or-later
"""Wall-panel text."""


# @rr(REQ-7): The unit is always shown next to the number.
def format_setpoint(setpoint_c: float) -> str:
    return f"{setpoint_c:.1f} °C"
