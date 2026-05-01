"""Human-readable formatting for datetime objects.

Produces 12-hour and 24-hour strings in US English. Internationalization
is out of scope; consumers needing other locales should use the
language-neutral fields returned by `timekeeper.core` and format
manually.
"""
from __future__ import annotations

from datetime import datetime


_DATE_FORMAT = "%A, %B %d, %Y"
_TIME_24H = "%H:%M %Z"
_TIME_12H = "%I:%M %p %Z"


def format_24h(dt: datetime) -> str:
    """Return `dt` formatted as 24-hour US English.

    Example: 'Thursday, April 30, 2026 at 14:46 PDT'.
    """
    return f"{dt.strftime(_DATE_FORMAT)} at {dt.strftime(_TIME_24H)}"


def format_12h(dt: datetime) -> str:
    """Return `dt` formatted as 12-hour US English.

    Example: 'Thursday, April 30, 2026 at 02:46 PM PDT'.
    """
    return f"{dt.strftime(_DATE_FORMAT)} at {dt.strftime(_TIME_12H)}"


def both_formats(dt: datetime) -> dict:
    """Return both 12-hour and 24-hour formatted strings keyed by format."""
    return {
        "human_readable_24h": format_24h(dt),
        "human_readable_12h": format_12h(dt),
    }
