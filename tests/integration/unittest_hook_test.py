# SPDX-License-Identifier: AGPL-3.0-or-later
import unittest

from rules_requirements import rr


class UnittestHook(unittest.TestCase):
    @rr.verifies("REQ-2", level="simulation")
    def test_decorator_is_traced(self):
        self.assertEqual(2 * 2, 4)

    @rr.verifies("REQ-5")
    def test_failure_is_recorded(self):
        # Deliberately failing: the golden report shows REQ-5 as FAILED.
        self.assertEqual("actual", "expected")


if __name__ == "__main__":
    rr.unittest_main()
