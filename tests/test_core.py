"""Tests for `timekeeper.core`.

Pure functions are tested with an injected fake clock for deterministic
results. The tests cover happy paths, error paths, and DST handling.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from timekeeper import core


# Thursday April 30, 2026 at 21:46 UTC == Pacific (PDT) 14:46.
FIXED_UTC = datetime(2026, 4, 30, 21, 46, 0, tzinfo=ZoneInfo("UTC"))


def fake_clock() -> datetime:
    return FIXED_UTC


class TestGetTimeInZone:
    def test_returns_utc_by_default(self):
        result = core.get_time_in_zone(clock_fn=fake_clock)
        assert result["timezone"] == "UTC"
        assert result["utc_iso"] == result["local_iso"]

    def test_converts_to_pacific_correctly(self):
        result = core.get_time_in_zone("America/Los_Angeles", clock_fn=fake_clock)
        # April is in PDT (UTC-7): 21:46 UTC -> 14:46 PDT.
        assert "14:46" in result["local_iso"]
        assert result["timezone"] == "America/Los_Angeles"

    def test_weekday_number_uses_python_convention(self):
        # April 30, 2026 is a Thursday. Python: Mon=0, Thu=3.
        result = core.get_time_in_zone("UTC", clock_fn=fake_clock)
        assert result["weekday_number"] == 3

    def test_month_number_is_one_indexed(self):
        result = core.get_time_in_zone("UTC", clock_fn=fake_clock)
        assert result["month_number"] == 4

    def test_unknown_timezone_raises(self):
        with pytest.raises(core.TimekeeperError) as excinfo:
            core.get_time_in_zone("Mars/Olympus_Mons", clock_fn=fake_clock)
        assert "Mars/Olympus_Mons" in str(excinfo.value)


class TestConvertToZone:
    def test_utc_to_pacific(self):
        result = core.convert_to_zone(
            "2026-05-08T03:59:00+00:00", "America/Los_Angeles"
        )
        # 03:59 UTC on May 8 -> 20:59 PDT on May 7.
        assert "2026-05-07T20:59:00" in result["converted_iso"]

    def test_eastern_to_utc(self):
        # 23:59 EDT on May 7 -> 03:59 UTC on May 8.
        result = core.convert_to_zone("2026-05-07T23:59:00-04:00", "UTC")
        assert "2026-05-08T03:59:00" in result["converted_iso"]

    def test_naive_timestamp_assumed_utc(self):
        result = core.convert_to_zone(
            "2026-05-08T03:59:00", "America/Los_Angeles"
        )
        assert "2026-05-07T20:59:00" in result["converted_iso"]

    def test_malformed_timestamp_raises(self):
        with pytest.raises(core.TimekeeperError):
            core.convert_to_zone("not-a-date", "UTC")

    def test_unknown_timezone_raises(self):
        with pytest.raises(core.TimekeeperError):
            core.convert_to_zone("2026-05-08T03:59:00Z", "Atlantis/City")


class TestTimeUntil:
    def test_future_target_is_positive(self):
        # Target is 7 days, 6 hours, 13 minutes after FIXED_UTC.
        result = core.time_until("2026-05-08T03:59:00+00:00", clock_fn=fake_clock)
        assert result["is_past"] is False
        assert result["total_seconds"] > 0
        assert result["days"] == 7
        assert result["hours"] == 6
        assert result["minutes"] == 13

    def test_past_target_is_negative(self):
        # Target is 1 hour before FIXED_UTC.
        result = core.time_until("2026-04-30T20:46:00+00:00", clock_fn=fake_clock)
        assert result["is_past"] is True
        assert result["total_seconds"] < 0
        assert result["hours"] == 1
        assert result["days"] == 0

    def test_naive_timestamp_assumed_utc(self):
        result = core.time_until("2026-05-08T03:59:00", clock_fn=fake_clock)
        assert result["days"] == 7

    def test_malformed_timestamp_raises(self):
        with pytest.raises(core.TimekeeperError):
            core.time_until("yesterday", clock_fn=fake_clock)


class TestDSTHandling:
    def test_pacific_in_winter_is_pst(self):
        # January is PST (UTC-8): 20:00 UTC -> 12:00 PST.
        winter_clock = lambda: datetime(2026, 1, 15, 20, 0, 0, tzinfo=ZoneInfo("UTC"))
        result = core.get_time_in_zone("America/Los_Angeles", clock_fn=winter_clock)
        assert "12:00:00-08:00" in result["local_iso"]

    def test_pacific_in_summer_is_pdt(self):
        # July is PDT (UTC-7): 19:00 UTC -> 12:00 PDT.
        summer_clock = lambda: datetime(2026, 7, 15, 19, 0, 0, tzinfo=ZoneInfo("UTC"))
        result = core.get_time_in_zone("America/Los_Angeles", clock_fn=summer_clock)
        assert "12:00:00-07:00" in result["local_iso"]
