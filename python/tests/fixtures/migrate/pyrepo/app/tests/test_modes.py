# SPDX-License-Identifier: AGPL-3.0-or-later
"""Parametrizations that verify different requirements need a hand split."""

import pytest

pytestmark = pytest.mark.rr("REQ-1", "REQ-3")


@pytest.mark.parametrize("mode", ["fast", "slow"])
def test_modes(mode):
    assert mode
