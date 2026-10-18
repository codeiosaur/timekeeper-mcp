"""MCP server: registers tools and translates between MCP and the domain.

This module is the only place that imports FastMCP. Time logic lives in
`timekeeper.core`; formatting lives in `timekeeper.formatting`.

Tool docstrings are written for the LLM that will decide whether to call
them. They are directive ("always call this") rather than descriptive
because directive phrasing materially affects tool selection.

Every tool returns an explicit `CallToolResult` whose text is a JSON
object (also sent as `structuredContent`). Errors set `isError: true` and
carry `{"error": "...", ...echoed inputs}`.

Output schemas: in MCP SDK 1.x, FastMCP validates every returned
`CallToolResult` against the tool's declared output model, error results
included. Each tool therefore declares a single object schema that types
both its success fields and its error/echoed-input fields, with none of
them required, so error payloads validate unchanged. Unknown properties
are rejected (`additionalProperties: false`), and field types are checked
strictly.
"""
from __future__ import annotations

import json
import logging
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, ConfigDict

from timekeeper import core, formatting

logger = logging.getLogger(__name__)

mcp = FastMCP("timekeeper")

# Echoed inputs in error payloads are truncated to this many characters.
_MAX_ECHO = 64


def _drop_null_defaults(schema: dict) -> None:
    # Fields default to None only so they may be absent; null is not a valid
    # value, so don't advertise it as a default in the published schema.
    for prop in schema.get("properties", {}).values():
        prop.pop("default", None)


class _ToolOutput(BaseModel):
    """Base for tool output schemas. Fields are optional so that success and
    error payloads share one schema; `None` defaults mean "may be absent",
    and an explicit null is rejected by strict validation."""

    model_config = ConfigDict(
        extra="forbid", strict=True, json_schema_extra=_drop_null_defaults
    )


class CurrentTimeOutput(_ToolOutput):
    """Success: every field except `error`. Error: `error` and `timezone`."""

    utc_iso: str = None
    local_iso: str = None
    timezone: str = None
    weekday_number: int = None
    month_number: int = None
    unix_timestamp: int = None
    human_readable_24h: str = None
    human_readable_12h: str = None
    error: str = None


class ConversionOutput(_ToolOutput):
    """Success: `original` through `human_readable_12h`. Error: `error`,
    `iso_timestamp`, and `target_timezone`."""

    original: str = None
    converted_iso: str = None
    timezone: str = None
    weekday_number: int = None
    month_number: int = None
    human_readable_24h: str = None
    human_readable_12h: str = None
    error: str = None
    iso_timestamp: str = None
    target_timezone: str = None


class DurationOutput(_ToolOutput):
    """Success: `target_iso` through `seconds`. Error: `error` and
    `iso_timestamp`."""

    target_iso: str = None
    now_iso: str = None
    is_past: bool = None
    total_seconds: int = None
    days: int = None
    hours: int = None
    minutes: int = None
    seconds: int = None
    error: str = None
    iso_timestamp: str = None


def _result(payload: dict, *, is_error: bool = False) -> CallToolResult:
    """Wrap `payload` as a tool result: JSON text plus structured content."""
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload, indent=2))],
        structuredContent=payload,
        isError=is_error,
    )


def _success(payload: dict, model: type[_ToolOutput]) -> CallToolResult:
    """Return a success result after checking `payload` against `model`.

    Called inside each tool's try block, so a malformed internal payload
    raises here and becomes the generic "Internal error" result (with the
    details logged server-side) instead of reaching the client. The SDK's
    own output validation remains as a backstop.
    """
    model.model_validate(payload)
    return _result(payload)


def _error(message: str, **inputs: str) -> CallToolResult:
    """Return an error result echoing the (truncated) inputs that caused it.

    The top-level `error` key is always present and truthy, so consumers
    can branch with `if "error" in response` as well as on `isError`.
    """
    echoed = {
        key: value if len(value) <= _MAX_ECHO else value[:_MAX_ECHO] + "..."
        for key, value in inputs.items()
    }
    return _result({"error": message, **echoed}, is_error=True)


def _annotations(title: str) -> ToolAnnotations:
    return ToolAnnotations(title=title, readOnlyHint=True, openWorldHint=False)


@mcp.tool(annotations=_annotations("Get current time"))
def get_current_time(
    timezone: str = "UTC",
) -> Annotated[CallToolResult, CurrentTimeOutput]:
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
        On success: a JSON object with `utc_iso`, `local_iso`, `timezone`,
        `weekday_number` (Python convention: 0=Monday, 6=Sunday),
        `month_number` (1=January, 12=December), `unix_timestamp`,
        `human_readable_24h`, and `human_readable_12h`.
        On failure: an error result (isError) whose JSON object has an
        `error` message and the requested `timezone`. Fix the input and
        retry; do not guess the time instead.
    """
    try:
        result = core.get_time_in_zone(timezone)
        result.update(
            formatting.both_formats(core.zoned(result["utc_iso"], timezone))
        )
        return _success(result, CurrentTimeOutput)
    except core.TimekeeperError as exc:
        return _error(str(exc), timezone=timezone)
    except Exception:
        logger.exception("get_current_time failed")
        return _error("Internal error", timezone=timezone)


@mcp.tool(annotations=_annotations("Convert time between timezones"))
def convert_time(
    iso_timestamp: str, target_timezone: str
) -> Annotated[CallToolResult, ConversionOutput]:
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
        On success: a JSON object with `original`, `converted_iso`,
        `timezone`, `weekday_number` (0=Monday, 6=Sunday), `month_number`,
        `human_readable_24h`, and `human_readable_12h`.
        On failure: an error result (isError) whose JSON object has an
        `error` message and the original inputs.
    """
    try:
        result = core.convert_to_zone(iso_timestamp, target_timezone)
        result.update(
            formatting.both_formats(core.zoned(iso_timestamp, target_timezone))
        )
        return _success(result, ConversionOutput)
    except core.TimekeeperError as exc:
        return _error(
            str(exc), iso_timestamp=iso_timestamp, target_timezone=target_timezone
        )
    except Exception:
        logger.exception("convert_time failed")
        return _error(
            "Internal error",
            iso_timestamp=iso_timestamp,
            target_timezone=target_timezone,
        )


@mcp.tool(annotations=_annotations("Time until or since a timestamp"))
def time_until(iso_timestamp: str) -> Annotated[CallToolResult, DurationOutput]:
    """Compute the duration until or since an ISO 8601 timestamp.

    Useful for deadline arithmetic and for reasoning about scheduled
    future events. Negative `total_seconds` indicates the target is in
    the past; check `is_past` for an explicit boolean.

    Args:
        iso_timestamp: An ISO 8601 datetime string. If the string has
            no timezone information, UTC is assumed.

    Returns:
        On success: a JSON object with `target_iso`, `now_iso`, `is_past`,
        `total_seconds` (signed), and a `days`/`hours`/`minutes`/`seconds`
        breakdown of the absolute duration.
        On failure: an error result (isError) whose JSON object has an
        `error` message and the original input.
    """
    try:
        return _success(core.time_until(iso_timestamp), DurationOutput)
    except core.TimekeeperError as exc:
        return _error(str(exc), iso_timestamp=iso_timestamp)
    except Exception:
        logger.exception("time_until failed")
        return _error("Internal error", iso_timestamp=iso_timestamp)
