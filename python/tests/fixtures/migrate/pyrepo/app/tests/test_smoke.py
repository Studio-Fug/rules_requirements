# SPDX-License-Identifier: AGPL-3.0-or-later
"""A single-id tag inside a target another requirement claims as a whole."""

import pytest


@pytest.mark.rr("REQ-3")
def test_boots():
    assert True


def test_prints_version():
    assert True
