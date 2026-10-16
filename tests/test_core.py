"""Tests for `timekeeper.core`.

Pure functions are tested with an injected fake clock for deterministic
results. The tests cover happy paths, error paths, and DST handling.
"""
from datetime import datetime, timedelta, timezone
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


class TestRealClock:
    def test_returns_aware_utc(self):
        now = core._real_clock()
        assert now.utcoffset() == timedelta(0)


# (timezone_name, expected_offset_string_in_local_iso)
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
    ("Asia/Kathmandu",      "+05:45"),  # NPT (quarter-hour offset)
    ("Australia/Sydney",    "+10:00"),  # AEST (DST not active in April)
    ("Pacific/Auckland",    "+12:00"),  # NZST (DST not active in April)
    ("Pacific/Kiritimati",  "+14:00"),  # LINT (largest offset)
]


@pytest.mark.parametrize("tz_name,expected_offset", WORLD_ZONES)
def test_local_iso_carries_correct_offset(tz_name, expected_offset):
    result = core.get_time_in_zone(tz_name, clock_fn=fake_clock)
    assert result["timezone"] == tz_name
    assert result["local_iso"].endswith(expected_offset)


# Source: 2026-05-08T03:59:00Z (the same instant in every row).
ROUND_TRIP_CONVERSIONS = [
    ("America/Los_Angeles", "2026-05-07T20:59:00-07:00"),
    ("America/New_York",    "2026-05-07T23:59:00-04:00"),
    ("Europe/London",       "2026-05-08T04:59:00+01:00"),
    ("Asia/Tokyo",          "2026-05-08T12:59:00+09:00"),
    ("Asia/Kolkata",        "2026-05-08T09:29:00+05:30"),
    ("Australia/Sydney",    "2026-05-08T13:59:00+10:00"),
]


@pytest.mark.parametrize("target_tz,expected_iso", ROUND_TRIP_CONVERSIONS)
def test_utc_instant_converts_correctly(target_tz, expected_iso):
    result = core.convert_to_zone("2026-05-08T03:59:00+00:00", target_tz)
    assert result["converted_iso"] == expected_iso


# (timezone_name, utc_winter_clock, expected_winter_offset,
#                 utc_summer_clock, expected_summer_offset)
DST_CASES = [
    ("America/Los_Angeles",
     datetime(2026, 1, 15, 20, 0, tzinfo=timezone.utc), "-08:00",
     datetime(2026, 7, 15, 19, 0, tzinfo=timezone.utc), "-07:00"),
    ("Europe/Berlin",
     datetime(2026, 1, 15, 11, 0, tzinfo=timezone.utc), "+01:00",
     datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc), "+02:00"),
    # Southern hemisphere: DST in January, standard in July.
    ("Australia/Sydney",
     datetime(2026, 1, 15, 1, 0, tzinfo=timezone.utc), "+11:00",
     datetime(2026, 7, 15, 2, 0, tzinfo=timezone.utc), "+10:00"),
    ("Asia/Tokyo",
     datetime(2026, 1, 15, 3, 0, tzinfo=timezone.utc), "+09:00",
     datetime(2026, 7, 15, 3, 0, tzinfo=timezone.utc), "+09:00"),
]


@pytest.mark.parametrize(
    "tz_name,winter_now,winter_offset,summer_now,summer_offset", DST_CASES
)
def test_offset_changes_match_dst_rules(
    tz_name, winter_now, winter_offset, summer_now, summer_offset
):
    winter = core.get_time_in_zone(tz_name, clock_fn=lambda: winter_now)
    summer = core.get_time_in_zone(tz_name, clock_fn=lambda: summer_now)
    assert winter["local_iso"].endswith(winter_offset)
    assert summer["local_iso"].endswith(summer_offset)


class TestTimezoneValidation:
    @pytest.mark.parametrize("tz_name", [
        "../../etc/passwd",
        "/etc/localtime",
        "UTC/../UTC",
        "",
        "utc",              # ZoneInfo accepts this on case-insensitive filesystems
        "posixrules",
        "zone.tab",
        "localtime",
        "America/New_York\x00",
        "America/New_York ",
    ])
    def test_rejects_non_iana_keys(self, tz_name):
        with pytest.raises(core.TimekeeperError, match="Unknown timezone"):
            core.get_time_in_zone(tz_name, clock_fn=fake_clock)
        with pytest.raises(core.TimekeeperError, match="Unknown timezone"):
            core.convert_to_zone("2026-05-08T03:59:00+00:00", tz_name)

    @pytest.mark.parametrize("tz_name", ["US/Pacific", "Etc/GMT+5", "UTC"])
    def test_accepts_iana_aliases(self, tz_name):
        assert core.get_time_in_zone(tz_name, clock_fn=fake_clock)["timezone"] == tz_name

    def test_oversized_key_gives_bounded_message(self):
        with pytest.raises(core.TimekeeperError) as excinfo:
            core.get_time_in_zone("x" * 100_000, clock_fn=fake_clock)
        message = str(excinfo.value)
        assert len(message) < 300
        assert "100000 chars" in message

    def test_zone_list_is_scanned_once(self, monkeypatch):
        calls = []

        def counting_available_timezones():
            calls.append(1)
            return {"UTC"}

        monkeypatch.setattr(core, "available_timezones", counting_available_timezones)
        core._known_timezones.cache_clear()
        try:
            for _ in range(3):
                core.get_time_in_zone("UTC", clock_fn=fake_clock)
            assert len(calls) == 1
        finally:
            core._known_timezones.cache_clear()


class TestTimestampParsing:
    @pytest.mark.parametrize("iso,expected_utc", [
        ("2026-05-08T03:59:00Z", "2026-05-08T03:59:00+00:00"),
        ("2026-05-08T03:59:00.250000Z", "2026-05-08T03:59:00.250000+00:00"),
        ("2026-05-08T17:59:00+14:00", "2026-05-08T03:59:00+00:00"),
        ("2026-05-07T15:59:00-12:00", "2026-05-08T03:59:00+00:00"),
        ("2026-05-08T09:44:00+05:45", "2026-05-08T03:59:00+00:00"),
        # Date-only input is midnight UTC (documented behavior).
        ("2026-05-08", "2026-05-08T00:00:00+00:00"),
    ])
    def test_accepted_forms(self, iso, expected_utc):
        assert core.convert_to_zone(iso, "UTC")["converted_iso"] == expected_utc

    @pytest.mark.parametrize("iso", [
        "",
        " 2026-05-08T03:59:00Z",
        "yesterday",
        "2026-13-01T00:00:00",
        "2026-05-08T03:59:00+24:00",
    ])
    def test_malformed_raises(self, iso):
        with pytest.raises(core.TimekeeperError, match="Could not parse"):
            core.convert_to_zone(iso, "UTC")
        with pytest.raises(core.TimekeeperError, match="Could not parse"):
            core.time_until(iso, clock_fn=fake_clock)

    def test_oversized_timestamp_gives_bounded_message(self):
        with pytest.raises(core.TimekeeperError) as excinfo:
            core.time_until("9" * 100_000, clock_fn=fake_clock)
        assert len(str(excinfo.value)) < 300


class TestDateBoundaries:
    def test_crossing_year_end_forward(self):
        result = core.convert_to_zone("2026-12-31T23:30:00+00:00", "Asia/Tokyo")
        assert result["converted_iso"] == "2027-01-01T08:30:00+09:00"
        assert result["month_number"] == 1
        assert result["weekday_number"] == 4  # Friday

    def test_crossing_year_end_backward(self):
        result = core.convert_to_zone("2026-01-01T03:00:00+00:00", "America/Los_Angeles")
        assert result["converted_iso"] == "2025-12-31T19:00:00-08:00"
        assert result["month_number"] == 12

    def test_leap_day_rolls_into_march(self):
        result = core.convert_to_zone("2028-02-29T12:00:00+00:00", "Pacific/Kiritimati")
        assert result["converted_iso"] == "2028-03-01T02:00:00+14:00"


class TestOverflow:
    @pytest.mark.parametrize("iso,tz_name", [
        ("9999-12-31T23:59:59+00:00", "Asia/Tokyo"),
        ("0001-01-01T00:00:00+00:00", "America/New_York"),
        ("0001-01-01T00:00:00+01:00", "UTC"),
    ])
    def test_out_of_range_conversion_raises_domain_error(self, iso, tz_name):
        with pytest.raises(core.TimekeeperError, match="representable date range"):
            core.convert_to_zone(iso, tz_name)

    def test_extreme_future_duration_is_exact(self):
        # Float total_seconds() would round this up by one second.
        result = core.time_until("9999-12-31T23:59:59.999999+00:00", clock_fn=fake_clock)
        assert result["total_seconds"] == 251_624_715_239
        assert result["is_past"] is False

    def test_extreme_past_duration(self):
        result = core.time_until("0001-01-01T00:00:00+00:00", clock_fn=fake_clock)
        assert result["is_past"] is True
        assert result["total_seconds"] < 0


class TestTimeUntilPrecision:
    # (offset of target from now, is_past, total_seconds, seconds field)
    @pytest.mark.parametrize("offset,is_past,total,seconds", [
        (timedelta(0), False, 0, 0),
        (timedelta(microseconds=1), False, 0, 0),
        (timedelta(microseconds=-1), True, 0, 0),
        (timedelta(seconds=0.5), False, 0, 0),
        (timedelta(seconds=-0.5), True, 0, 0),
        (timedelta(seconds=1.5), False, 1, 1),
        (timedelta(seconds=-1.5), True, -1, 1),
    ])
    def test_sub_second_semantics(self, offset, is_past, total, seconds):
        target = (FIXED_UTC + offset).isoformat()
        result = core.time_until(target, clock_fn=fake_clock)
        assert result["is_past"] is is_past
        assert result["total_seconds"] == total
        assert result["seconds"] == seconds


class TestDSTTransitions:
    def test_spring_forward_skips_two_am(self):
        before = core.convert_to_zone("2026-03-08T06:59:59+00:00", "America/New_York")
        after = core.convert_to_zone("2026-03-08T07:00:00+00:00", "America/New_York")
        assert before["converted_iso"] == "2026-03-08T01:59:59-05:00"
        assert after["converted_iso"] == "2026-03-08T03:00:00-04:00"

    def test_fall_back_repeated_hour_is_distinguished(self):
        first = core.zoned("2026-11-01T05:30:00+00:00", "America/New_York")
        second = core.zoned("2026-11-01T06:30:00+00:00", "America/New_York")
        assert first.isoformat() == "2026-11-01T01:30:00-04:00"
        assert second.isoformat() == "2026-11-01T01:30:00-05:00"
        assert (first.tzname(), second.tzname()) == ("EDT", "EST")

    def test_time_until_counts_elapsed_seconds_across_spring_forward(self):
        # Local midnight to local midnight on the 23-hour day.
        midnight = lambda: datetime(2026, 3, 8, 5, 0, tzinfo=timezone.utc)
        result = core.time_until("2026-03-09T00:00:00-04:00", clock_fn=midnight)
        assert result["total_seconds"] == 23 * 3600
        assert (result["days"], result["hours"]) == (0, 23)

    def test_time_until_counts_elapsed_seconds_across_fall_back(self):
        # Local midnight to local midnight on the 25-hour day.
        midnight = lambda: datetime(2026, 11, 1, 4, 0, tzinfo=timezone.utc)
        result = core.time_until("2026-11-02T00:00:00-05:00", clock_fn=midnight)
        assert result["total_seconds"] == 25 * 3600
        assert (result["days"], result["hours"]) == (1, 1)


class TestZoned:
    def test_returns_named_zone_not_fixed_offset(self):
        dt = core.zoned("2026-04-30T21:46:00+00:00", "America/Los_Angeles")
        assert dt.tzinfo == ZoneInfo("America/Los_Angeles")
        assert dt.tzname() == "PDT"
        assert (dt.hour, dt.minute) == (14, 46)

    def test_naive_input_is_utc(self):
        assert core.zoned("2026-04-30T21:46:00", "UTC").hour == 21

    def test_unknown_timezone_raises(self):
        with pytest.raises(core.TimekeeperError):
            core.zoned("2026-04-30T21:46:00+00:00", "Mars/Olympus_Mons")
