# SPDX-License-Identifier: AGPL-3.0-or-later
"""Configuration parsing: one module-level marker naming two requirements."""

import pytest


@pytest.mark.requirements("REQ-1")
def test_parses_minimal_file():
    assert True


@pytest.mark.requirements("REQ-2")
def test_rejects_missing_key():
    assert True


@pytest.mark.requirements("REQ-2")
@pytest.mark.parametrize("text", ["", "{", "]"])
def test_rejects_garbage(text):
    assert text is not None


class TestRoundTrip:
    @pytest.mark.requirements("REQ-1")
    def test_dump_then_load(self):
        assert True
