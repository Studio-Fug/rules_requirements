# SPDX-License-Identifier: AGPL-3.0-or-later
"""A unittest suite: ids from the class and the method accumulate."""

import unittest

from rules_requirements import rr


@rr.verifies("REQ-1", "REQ-3", level="simulation")
class LegacyTest(unittest.TestCase):
    def test_reads_old_format(self):
        self.assertTrue(True)

    @rr.verifies("REQ-2")
    def test_rejects_old_garbage(self):
        self.assertTrue(True)


if __name__ == "__main__":
    rr.unittest_main()
