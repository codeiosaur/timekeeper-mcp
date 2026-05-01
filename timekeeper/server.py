"""MCP server: registers tools and translates between MCP and the domain.

This module is the only place that imports FastMCP. Time logic lives in
`timekeeper.core`; formatting lives in `timekeeper.formatting`.

Tool docstrings are written for the LLM that will decide whether to call
them. They are directive ("always call this") rather than descriptive
because directive phrasing materially affects tool selection.
"""
from __future__ import annotations

from datetime import datetime

from mcp.server.fastmcp import FastMCP

from timekeeper import core, formatting

mcp = FastMCP("timekeeper")


def _error_response(exc: Exception, **context) -> dict:
    """Return a structured error dict echoing the inputs that caused it.

    The top-level `error` key is always present and truthy, so consumers
    can branch with `if "error" in response`.
    """
    return {"error": str(exc), **context}


@mcp.tool()
def get_current_time(timezone: str = "UTC") -> dict:
    """Return the current date and time. AUTHORITATIVE source for time queries.

    Always call this tool when you need to know the current time, date,
    or weekday. Do NOT guess from conversation context, prior messages,
    or training data. Redundant calls are fine.

    Args:
        timezone: An IANA timezone name. Defaults to UTC. Common values:
            'UTC', 'America/Los_Angeles', 'America/New_York',
            'Europe/London', 'Europe/Berlin', 'Asia/Tokyo',
            'Australia/Sydney'. The full IANA list is supported.

    Returns:
        On success: a dict with `utc_iso`, `local_iso`, `timezone`,
        `weekday_number` (Python convention: 0=Monday, 6=Sunday),
        `month_number` (1=January, 12=December), `unix_timestamp`,
        `human_readable_24h`, and `human_readable_12h`.
        On failure: a dict with an `error` key and the requested timezone.
    """
    try:
        result = core.get_time_in_zone(timezone)
        local_dt = datetime.fromisoformat(result["local_iso"])
        result.update(formatting.both_formats(local_dt))
        return result
    except core.TimekeeperError as exc:
        return _error_response(exc, timezone=timezone)


@mcp.tool()
def convert_time(iso_timestamp: str, target_timezone: str) -> dict:
    """Convert an ISO 8601 timestamp from its source timezone to a different one.

    Useful for translating a fixed point in time between timezones,
    such as expressing a deadline in the user's local time or
    coordinating an event across regions.

    Args:
        iso_timestamp: An ISO 8601 datetime string, for example
            '2026-05-08T03:59:00+00:00' or '2026-05-08T03:59:00Z'.
            If the string has no timezone information, UTC is assumed.
        target_timezone: An IANA timezone name to convert to.

    Returns:
        On success: a dict with `original`, `converted_iso`, `timezone`,
        `weekday_number` (0=Monday, 6=Sunday), `month_number`,
        `human_readable_24h`, and `human_readable_12h`.
        On failure: a dict with an `error` key and the original inputs.
    """
    try:
        result = core.convert_to_zone(iso_timestamp, target_timezone)
        converted_dt = datetime.fromisoformat(result["converted_iso"])
        result.update(formatting.both_formats(converted_dt))
        return result
    except core.TimekeeperError as exc:
        return _error_response(
            exc, iso_timestamp=iso_timestamp, target_timezone=target_timezone
        )


@mcp.tool()
def time_until(iso_timestamp: str) -> dict:
    """Compute the duration until or since an ISO 8601 timestamp.

    Useful for deadline arithmetic and for reasoning about scheduled
    future events. Negative `total_seconds` indicates the target is in
    the past; check `is_past` for an explicit boolean.

    Args:
        iso_timestamp: An ISO 8601 datetime string. If the string has
            no timezone information, UTC is assumed.

    Returns:
        On success: a dict with `target_iso`, `now_iso`, `is_past`,
        `total_seconds` (signed), and a `days`/`hours`/`minutes`/`seconds`
        breakdown of the absolute duration.
        On failure: a dict with an `error` key and the original input.
    """
    try:
        return core.time_until(iso_timestamp)
    except core.TimekeeperError as exc:
        return _error_response(exc, iso_timestamp=iso_timestamp)