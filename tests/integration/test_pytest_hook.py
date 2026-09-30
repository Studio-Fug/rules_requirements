# SPDX-License-Identifier: AGPL-3.0-or-later
import pytest


@pytest.mark.rr("REQ-1")
def test_marker_is_traced():
    assert 1 + 1 == 2


@pytest.mark.rr("REQ-6", level="simulation")
def test_hardware_requirement_in_simulation():
    assert True
