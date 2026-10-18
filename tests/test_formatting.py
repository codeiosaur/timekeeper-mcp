"""Tests for `timekeeper.formatting`."""
import locale
from contextlib import contextmanager
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

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


# Instants covering midnight, noon, single-digit days, and every month.
_SAMPLES = [
    datetime(2026, month, day, hour, minute, tzinfo=ZoneInfo(zone))
    for month, day, hour, minute, zone in [
        (1, 1, 0, 0, "UTC"),
        (2, 9, 12, 0, "Europe/Berlin"),
        (3, 8, 3, 0, "America/New_York"),
        (4, 30, 14, 46, "America/Los_Angeles"),
        (5, 5, 23, 59, "Asia/Kolkata"),
        (6, 15, 11, 59, "Australia/Sydney"),
        (7, 4, 12, 1, "America/Chicago"),
        (8, 31, 0, 30, "Asia/Tokyo"),
        (9, 1, 9, 5, "Europe/London"),
        (10, 25, 1, 0, "Europe/Berlin"),
        (11, 1, 1, 30, "America/New_York"),
        (12, 31, 23, 59, "Pacific/Kiritimati"),
    ]
]


@contextmanager
def lc_time(*candidates):
    """Set LC_TIME to the first available candidate; always restore it.

    Skips the test if none of the candidates is installed.
    """
    original = locale.setlocale(locale.LC_TIME)
    try:
        for name in candidates:
            try:
                locale.setlocale(locale.LC_TIME, name)
                break
            except locale.Error:
                continue
        else:
            pytest.skip(f"none of the locales {candidates} is installed")
        yield
    finally:
        locale.setlocale(locale.LC_TIME, original)


@pytest.mark.parametrize("dt", _SAMPLES, ids=lambda dt: dt.isoformat())
def test_output_matches_previous_strftime_format_in_c_locale(dt):
    # The fixed name tables must reproduce the old strftime output exactly.
    # strftime's names are English only in the C locale, so establish it.
    with lc_time("C"):
        date = dt.strftime("%A, %B %d, %Y")
        old_24h = f"{date} at {dt.strftime('%H:%M %Z')}"
        old_12h = f"{date} at {dt.strftime('%I:%M %p %Z')}"
    assert formatting.format_24h(dt) == old_24h
    assert formatting.format_12h(dt) == old_12h


def test_output_stays_english_under_a_non_english_locale():
    with lc_time("de_DE.UTF-8", "de_DE", "German_Germany.1252"):
        # Sanity check: strftime itself is localized under this locale.
        assert _sample_dt().strftime("%A") == "Donnerstag"
        assert formatting.format_24h(_sample_dt()) == (
            "Thursday, April 30, 2026 at 14:46 PDT"
        )
        assert formatting.format_12h(_sample_dt()) == (
            "Thursday, April 30, 2026 at 02:46 PM PDT"
        )


def test_locale_helper_restores_the_original_locale():
    original = locale.setlocale(locale.LC_TIME)
    with lc_time("C"):
        pass
    assert locale.setlocale(locale.LC_TIME) == original


def test_zone_aware_datetime_keeps_abbreviation():
    dt = datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc).astimezone(
        ZoneInfo("America/New_York")
    )
    assert formatting.format_24h(dt).endswith("01:30 EST")


def test_fixed_offset_datetime_shows_numeric_offset():
    dt = datetime.fromisoformat("2026-04-30T14:46:00-07:00")
    assert formatting.format_24h(dt).endswith("14:46 UTC-07:00")


@pytest.mark.parametrize("hour,expected", [
    (0, "12:00 AM"), (11, "11:00 AM"), (12, "12:00 PM"), (23, "11:00 PM"),
])
def test_12h_clock_edges(hour, expected):
    dt = datetime(2026, 1, 1, hour, 0, tzinfo=timezone.utc)
    assert f"at {expected} UTC" in formatting.format_12h(dt)
