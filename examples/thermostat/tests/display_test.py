# SPDX-License-Identifier: AGPL-3.0-or-later
import unittest

from rules_requirements import rr
from thermostat.display import format_setpoint


class DisplayTest(unittest.TestCase):
    # Automated check of the text; the rendered panel is signed off by
    # inspection (evidence/panel_inspection.rr.yaml) as TM-2 demands.
    @rr.verifies("REQ-7")
    def test_setpoint_shows_unit(self):
        self.assertEqual(format_setpoint(21.5), "21.5 °C")
        self.assertEqual(format_setpoint(5), "5.0 °C")


if __name__ == "__main__":
    rr.unittest_main()
