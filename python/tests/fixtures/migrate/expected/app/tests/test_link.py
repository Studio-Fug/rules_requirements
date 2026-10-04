# SPDX-License-Identifier: AGPL-3.0-or-later
"""Link recovery: a marker list whose rr marker names two requirements."""

import pytest

pytestmark = [
    pytest.mark.requirements("REQ-3", level="sil"),
    pytest.mark.filterwarnings("ignore::DeprecationWarning"),
]


def test_reconnects_after_drop():
    assert True


def test_backs_off_exponentially():
    assert True
