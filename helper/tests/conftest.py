"""Shared test setup."""
import pytest

from balloonlens.backends import page


@pytest.fixture(autouse=True)
def no_call_gap(monkeypatch):
    """The one-second gap between CLI calls guards real use. Fake CLIs answer at once, so tests skip the wait."""
    monkeypatch.setattr(page, "MIN_CALL_GAP_S", 0.0)
