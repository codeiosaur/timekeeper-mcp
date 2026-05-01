"""Time and timezone computation.

All public functions are pure: they take inputs and return outputs.
The current time is supplied via an injectable `clock_fn` parameter
(default: real system clock) so callers can substitute a fake for
deterministic testing.

Errors are raised as `TimekeeperError`. The MCP layer in
`timekeeper.server` is responsible for converting them to structured
error responses.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable
from zoneinfo import ZoneInfo, available_timezones


class TimekeeperError(ValueError):
    """Raised for invalid input: unknown timezones, malformed timestamps."""


def _real_clock() -> datetime:
    """Return the current UTC time as a tz-aware datetime."""
    return datetime.now(ZoneInfo("UTC"))


def _validate_timezone(tz_name: str) -> ZoneInfo:
    """Return a ZoneInfo for `tz_name`, or raise `TimekeeperError`."""
    if tz_name not in available_timezones():
        raise TimekeeperError(
            f"Unknown timezone: {tz_name!r}. "
            f"Use IANA names like 'UTC', 'America/Los_Angeles', "
            f"'America/New_York', or 'Europe/Berlin'."
        )
    return ZoneInfo(tz_name)


def get_time_in_zone(
    tz_name: str = "UTC",
    clock_fn: Callable[[], datetime] = _real_clock,
) -> dict:
    """Return the current time, expressed in the given timezone.

    Args:
        tz_name: An IANA timezone name. Defaults to UTC.
        clock_fn: A no-argument callable returning a tz-aware UTC datetime.

    Returns:
        A dict containing both UTC and local time in ISO 8601 form,
        a Unix timestamp, and language-neutral weekday and month numbers.
        Human-readable formatting is added by the MCP layer.

    Raises:
        TimekeeperError: If `tz_name` is not a valid IANA timezone.
    """
    tz = _validate_timezone(tz_name)
    now_utc = clock_fn()
    now_local = now_utc.astimezone(tz)

    return {
        "utc_iso": now_utc.isoformat(),
        "local_iso": now_local.isoformat(),
        "timezone": tz_name,
        "weekday_number": now_local.weekday(),  # 0=Monday, 6=Sunday
        "month_number": now_local.month,        # 1=January, 12=December
        "unix_timestamp": int(now_utc.timestamp()),
    }


def convert_to_zone(iso_timestamp: str, target_tz_name: str) -> dict:
    """Convert an ISO 8601 timestamp to a different timezone.

    Args:
        iso_timestamp: An ISO 8601 datetime string. If naive (no tzinfo),
            UTC is assumed.
        target_tz_name: An IANA timezone name to convert to.

    Returns:
        A dict containing the original and converted timestamps in ISO
        form, plus language-neutral fields.

    Raises:
        TimekeeperError: If the timestamp is malformed or the timezone is
            unknown.
    """
    target_tz = _validate_timezone(target_tz_name)

    try:
        dt = datetime.fromisoformat(iso_timestamp)
    except ValueError as exc:
        raise TimekeeperError(
            f"Could not parse {iso_timestamp!r} as ISO 8601. "
            f"Examples: '2026-05-08T03:59:00+00:00' or '2026-05-08T03:59:00Z'."
        ) from exc

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))

    converted = dt.astimezone(target_tz)
    return {
        "original": iso_timestamp,
        "converted_iso": converted.isoformat(),
        "timezone": target_tz_name,
        "weekday_number": converted.weekday(),
        "month_number": converted.month,
    }


def time_until(
    iso_timestamp: str,
    clock_fn: Callable[[], datetime] = _real_clock,
) -> dict:
    """Compute the duration until (or since) a target ISO 8601 timestamp.

    Args:
        iso_timestamp: An ISO 8601 datetime string. If naive, UTC is assumed.
        clock_fn: A no-argument callable returning a tz-aware UTC datetime.

    Returns:
        A dict with the duration broken into days, hours, minutes, and
        seconds; a signed `total_seconds` (negative if the target is in
        the past); and an explicit `is_past` boolean.

    Raises:
        TimekeeperError: If the timestamp is malformed or invalid.
    """
    try:
        target = datetime.fromisoformat(iso_timestamp)
    except ValueError as exc:
        raise TimekeeperError(
            f"Could not parse {iso_timestamp!r} as ISO 8601."
        ) from exc

    if target.tzinfo is None:
        target = target.replace(tzinfo=ZoneInfo("UTC"))

    now = clock_fn()
    delta: timedelta = target - now
    total_seconds = int(delta.total_seconds())
    is_past = total_seconds < 0

    abs_seconds = abs(total_seconds)
    days, rem = divmod(abs_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)

    return {
        "target_iso": target.isoformat(),
        "now_iso": now.isoformat(),
        "is_past": is_past,
        "total_seconds": total_seconds,
        "days": days,
        "hours": hours,
        "minutes": minutes,
        "seconds": seconds,
    }
