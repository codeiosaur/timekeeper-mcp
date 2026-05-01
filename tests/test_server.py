"""Tests for `timekeeper.core`.

Pure functions are tested with an injected fake clock for deterministic
results. Timezone-sensitive cases are parameterized across continents
to surface region-specific tz database issues.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from timekeeper import core


# Thursday April 30, 2026 at 21:46 UTC. Chosen because it falls during
# Northern-hemisphere summer DST and Southern-hemisphere standard time,
# giving non-trivial offsets to verify in both directions.
FIXED_UTC = datetime(2026, 4, 30, 21, 46, 0, tzinfo=ZoneInfo("UTC"))


def fake_clock() -> datetime:
    return FIXED_UTC


# (timezone_name, expected_offset_string_in_local_iso)
# Spans North America, Europe, Africa, Asia, Oceania, and South America.
# Offsets reflect each zone's rules on FIXED_UTC's date.
WORLD_ZONES = [
    ("America/Los_Angeles", "-07:00"),  # PDT (DST active)
    ("America/New_York",    "-04:00"),  # EDT (DST active)
    ("America/Sao_Paulo",   "-03:00"),  # BRT (no DST since 2019)
    ("Europe/London",       "+01:00"),  # BST (DST active)
    ("Europe/Berlin",       "+02:00"),  # CEST (DST active)
    ("Africa/Johannesburg", "+02:00"),  # SAST (no DST)
    ("Asia/Tokyo",          "+09:00"),  # JST (no DST)
    ("Asia/Kolkata",        "+05:30"),  # IST (half-hour offset)
    ("Australia/Sydney",    "+10:00"),  # AEST (DST not active in April)
    ("Pacific/Auckland",    "+12:00"),  # NZST (DST not active in April)
]


class TestGetTimeInZone:
    def test_returns_utc_by_default(self):
        result = core.get_time_in_zone(clock_fn=fake_clock)
        assert result["timezone"] == "UTC"
        assert result["utc_iso"] == result["local_iso"]

    @pytest.mark.parametrize("tz_name,expected_offset", WORLD_ZONES)
    def test_local_iso_carries_correct_offset(self, tz_name, expected_offset):
        result = core.get_time_in_zone(tz_name, clock_fn=fake_clock)
        assert result["timezone"] == tz_name
        assert expected_offset in result["local_iso"]

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


# (source_iso, target_zone, expected_substring_in_converted_iso)
# Verifies a single UTC instant lands at the right local time worldwide.
# Source: 2026-05-08T03:59:00Z (the same instant in every row).
ROUND_TRIP_CONVERSIONS = [
    ("America/Los_Angeles", "2026-05-07T20:59:00-07:00"),
    ("America/New_York",    "2026-05-07T23:59:00-04:00"),
    ("Europe/London",       "2026-05-08T04:59:00+01:00"),
    ("Asia/Tokyo",          "2026-05-08T12:59:00+09:00"),
    ("Asia/Kolkata",        "2026-05-08T09:29:00+05:30"),
    ("Australia/Sydney",    "2026-05-08T13:59:00+10:00"),
]


class TestConvertToZone:
    @pytest.mark.parametrize("target_tz,expected_iso", ROUND_TRIP_CONVERSIONS)
    def test_utc_instant_converts_correctly(self, target_tz, expected_iso):
        result = core.convert_to_zone("2026-05-08T03:59:00+00:00", target_tz)
        assert expected_iso in result["converted_iso"]

    def test_offset_to_offset_conversion(self):
        # Same instant, different starting offset: 23:59 EDT == 03:59 UTC next day.
        result = core.convert_to_zone("2026-05-07T23:59:00-04:00", "UTC")
        assert "2026-05-08T03:59:00" in result["converted_iso"]

    def test_naive_timestamp_assumed_utc(self):
        result = core.convert_to_zone("2026-05-08T03:59:00", "Europe/Berlin")
        # 03:59 UTC -> 05:59 CEST.
        assert "2026-05-08T05:59:00" in result["converted_iso"]

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

    def test_target_in_distant_zone_uses_absolute_instant(self):
        # An NZ-local time corresponds to an earlier UTC instant; verify
        # the duration math doesn't get confused by the offset.
        # 2026-05-08T15:59:00+12:00 == 2026-05-08T03:59:00Z, same as above.
        result = core.time_until("2026-05-08T15:59:00+12:00", clock_fn=fake_clock)
        assert result["days"] == 7
        assert result["hours"] == 6
        assert result["minutes"] == 13

    def test_malformed_timestamp_raises(self):
        with pytest.raises(core.TimekeeperError):
            core.time_until("yesterday", clock_fn=fake_clock)


# (timezone_name, utc_winter_clock, expected_winter_offset,
#                 utc_summer_clock, expected_summer_offset)
# Verifies DST behavior across hemispheres. Northern zones are in DST
# during summer (June/July); Southern zones are in DST during their
# summer (December/January).
DST_CASES = [
    # Northern hemisphere: standard time in January, DST in July.
    ("America/Los_Angeles",
     datetime(2026, 1, 15, 20, 0, tzinfo=ZoneInfo("UTC")), "-08:00",
     datetime(2026, 7, 15, 19, 0, tzinfo=ZoneInfo("UTC")), "-07:00"),
    ("Europe/Berlin",
     datetime(2026, 1, 15, 11, 0, tzinfo=ZoneInfo("UTC")), "+01:00",
     datetime(2026, 7, 15, 10, 0, tzinfo=ZoneInfo("UTC")), "+02:00"),
    # Southern hemisphere: DST in January, standard in July.
    ("Australia/Sydney",
     datetime(2026, 1, 15, 1, 0, tzinfo=ZoneInfo("UTC")), "+11:00",
     datetime(2026, 7, 15, 2, 0, tzinfo=ZoneInfo("UTC")), "+10:00"),
    # No DST observed at all: offset is the same year-round.
    ("Asia/Tokyo",
     datetime(2026, 1, 15, 3, 0, tzinfo=ZoneInfo("UTC")), "+09:00",
     datetime(2026, 7, 15, 3, 0, tzinfo=ZoneInfo("UTC")), "+09:00"),
]


class TestDSTHandling:
    @pytest.mark.parametrize(
        "tz_name,winter_now,winter_offset,summer_now,summer_offset",
        DST_CASES,
    )
    def test_offset_changes_match_dst_rules(
        self, tz_name, winter_now, winter_offset, summer_now, summer_offset
    ):
        winter = core.get_time_in_zone(tz_name, clock_fn=lambda: winter_now)
        summer = core.get_time_in_zone(tz_name, clock_fn=lambda: summer_now)
        assert winter_offset in winter["local_iso"]
        assert summer_offset in summer["local_iso"]