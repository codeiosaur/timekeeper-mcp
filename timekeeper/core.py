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

from datetime import datetime, timedelta, timezone
from functools import cache
from typing import Callable
from zoneinfo import ZoneInfo, available_timezones

# Inputs echoed back in error messages are truncated to this many characters.
_MAX_ECHO = 64

# Files that some systems keep in their zoneinfo directory but that are not
# IANA zone names: "localtime" (a link to the host's own zone, present on
# Linux, where available_timezones() lists it) and "posixrules".
_NON_IANA_KEYS = frozenset({"localtime", "posixrules"})


class TimekeeperError(ValueError):
    """Raised for invalid input: unknown timezones, malformed timestamps."""


def _real_clock() -> datetime:
    """Return the current UTC time as a tz-aware datetime."""
    return datetime.now(timezone.utc)


@cache
def _known_timezones() -> frozenset[str]:
    """Return the set of valid IANA keys, scanned once per process.

    Only the list of zone names is cached, never a current time.
    `available_timezones()` walks the tz database on disk, which is far too
    slow to repeat per request. Checking membership before calling
    `ZoneInfo()` also rejects keys that `ZoneInfo()` alone would accept
    inconsistently across platforms (e.g. 'utc' on case-insensitive
    filesystems) and keeps path-like input away from the filesystem.
    Host-specific entries such as 'localtime' are excluded, so the accepted
    keys don't depend on how the host lays out its tz database.
    Timezone-data updates on disk take effect only after a restart.
    """
    return frozenset(available_timezones()) - _NON_IANA_KEYS


def _echo(value: str) -> str:
    """Return `value` as a repr, truncated for safe inclusion in errors."""
    if len(value) > _MAX_ECHO:
        return f"{value[:_MAX_ECHO]!r}... ({len(value)} chars)"
    return repr(value)


def _validate_timezone(tz_name: str) -> ZoneInfo:
    """Return a ZoneInfo for `tz_name`, or raise `TimekeeperError`."""
    if tz_name not in _known_timezones():
        raise TimekeeperError(
            f"Unknown timezone: {_echo(tz_name)}. "
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


def _parse_iso(iso_timestamp: str) -> datetime:
    """Parse an ISO 8601 string into an aware datetime (naive means UTC)."""
    try:
        dt = datetime.fromisoformat(iso_timestamp)
    except ValueError as exc:
        raise TimekeeperError(
            f"Could not parse {_echo(iso_timestamp)} as ISO 8601. "
            f"Examples: '2026-05-08T03:59:00+00:00' or '2026-05-08T03:59:00Z'."
        ) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def zoned(iso_timestamp: str, tz_name: str) -> datetime:
    """Return the instant `iso_timestamp` as an aware datetime in `tz_name`.

    For callers that need a real `ZoneInfo`-backed datetime (e.g. to show
    the zone abbreviation 'EDT') rather than the fixed offset that
    reparsing an ISO string yields.

    Raises:
        TimekeeperError: If the timestamp, timezone, or result is invalid.
    """
    tz = _validate_timezone(tz_name)
    dt = _parse_iso(iso_timestamp)
    try:
        return dt.astimezone(tz)
    except OverflowError as exc:
        raise TimekeeperError(
            f"{_echo(iso_timestamp)} in {tz_name} falls outside the "
            f"representable date range (years 1-9999)."
        ) from exc


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
        TimekeeperError: If the timestamp is malformed, the timezone is
            unknown, or the result falls outside years 1-9999.
    """
    converted = zoned(iso_timestamp, target_tz_name)
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

        `is_past` compares the exact (microsecond) difference, so a target
        even 0.5 seconds ago is past; a target equal to now is not.
        `total_seconds` and the breakdown are whole seconds truncated
        toward zero, so a sub-second past target reports
        `total_seconds == 0` with `is_past == True`.

    Raises:
        TimekeeperError: If the timestamp is malformed or invalid.
    """
    target = _parse_iso(iso_timestamp)
    now = clock_fn()
    delta: timedelta = target - now
    is_past = delta < timedelta(0)
    # Exact integer arithmetic; float total_seconds() loses precision on
    # multi-millennium spans and could round across a second boundary.
    abs_seconds = abs(delta) // timedelta(seconds=1)
    total_seconds = -abs_seconds if is_past else abs_seconds

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
