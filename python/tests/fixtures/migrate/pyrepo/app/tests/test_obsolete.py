# SPDX-License-Identifier: AGPL-3.0-or-later
"""Helpers whose tests verify no requirement at all."""

import pytest

pytestmark = pytest.mark.requirements("REQ-4", "REQ-5")


def test_helper_shape():
    assert True
