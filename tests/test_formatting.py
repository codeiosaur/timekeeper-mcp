"""Tests for `timekeeper.formatting`."""
from datetime import datetime
from zoneinfo import ZoneInfo

from timekeeper import formatting


def _sample_dt():
    # Thursday April 30, 2026 at 14:46 PDT.
    return datetime(2026, 4, 30, 14, 46, 0, tzinfo=ZoneInfo("America/Los_Angeles"))


def test_format_24h_uses_24_hour_clock():
    result = formatting.format_24h(_sample_dt())
    assert "14:46" in result
    assert "Thursday" in result
    assert "April" in result


def test_format_12h_uses_am_pm():
    result = formatting.format_12h(_sample_dt())
    assert "02:46" in result
    assert "PM" in result


def test_both_formats_returns_dict_with_both_keys():
    result = formatting.both_formats(_sample_dt())
    assert "human_readable_24h" in result
    assert "human_readable_12h" in result
    assert "14:46" in result["human_readable_24h"]
    assert "PM" in result["human_readable_12h"]
