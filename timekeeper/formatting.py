"""Human-readable formatting for datetime objects.

Produces 12-hour and 24-hour strings in US English. Internationalization
is out of scope; consumers needing other locales should use the
language-neutral fields returned by `timekeeper.core` and format
manually.

Names come from fixed tables rather than locale-dependent strftime codes
(`%A`, `%B`, `%p`), so the output is English regardless of the process
locale. Pass a `ZoneInfo`-backed datetime (see `core.zoned`) to get zone
abbreviations such as 'PDT'; a fixed-offset datetime yields 'UTC-07:00'.
"""
from __future__ import annotations

from datetime import datetime


_DAYS = (
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday",
    "Sunday",
)
_MONTHS = (
    "January", "February", "March", "April", "May", "June", "July",
    "August", "September", "October", "November", "December",
)


def _date(dt: datetime) -> str:
    return (
        f"{_DAYS[dt.weekday()]}, {_MONTHS[dt.month - 1]} "
        f"{dt.day:02d}, {dt.year:04d}"
    )


def format_24h(dt: datetime) -> str:
    """Return `dt` formatted as 24-hour US English.

    Example: 'Thursday, April 30, 2026 at 14:46 PDT'.
    """
    return f"{_date(dt)} at {dt.hour:02d}:{dt.minute:02d} {dt.tzname() or ''}"


def format_12h(dt: datetime) -> str:
    """Return `dt` formatted as 12-hour US English.

    Example: 'Thursday, April 30, 2026 at 02:46 PM PDT'.
    """
    hour = dt.hour % 12 or 12
    meridiem = "AM" if dt.hour < 12 else "PM"
    return (
        f"{_date(dt)} at {hour:02d}:{dt.minute:02d} {meridiem} "
        f"{dt.tzname() or ''}"
    )


def both_formats(dt: datetime) -> dict:
    """Return both 12-hour and 24-hour formatted strings keyed by format."""
    return {
        "human_readable_24h": format_24h(dt),
        "human_readable_12h": format_12h(dt),
    }
