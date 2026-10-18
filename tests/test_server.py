"""Tests for `timekeeper.server`, the MCP adapter.

Every test goes through a real MCP client session connected in memory, so
it observes exactly what a client sees on the wire: `isError`, the JSON
text content, `structuredContent`, tool schemas, and annotations. Core
time math is tested in `test_core.py`.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from timekeeper import core
from timekeeper.server import mcp

pytestmark = pytest.mark.anyio

# Fields of a complete success payload, per tool.
SUCCESS_FIELDS = {
    "get_current_time": {
        "utc_iso", "local_iso", "timezone", "weekday_number", "month_number",
        "unix_timestamp", "human_readable_24h", "human_readable_12h",
    },
    "convert_time": {
        "original", "converted_iso", "timezone", "weekday_number",
        "month_number", "human_readable_24h", "human_readable_12h",
    },
    "time_until": {
        "target_iso", "now_iso", "is_past", "total_seconds",
        "days", "hours", "minutes", "seconds",
    },
}
# Fields of an error payload: the message plus the echoed inputs.
ERROR_FIELDS = {
    "get_current_time": {"error", "timezone"},
    "convert_time": {"error", "iso_timestamp", "target_timezone"},
    "time_until": {"error", "iso_timestamp"},
}
TOOL_NAMES = set(SUCCESS_FIELDS)

_JSON_TYPES = {"string": str, "integer": int, "boolean": bool}


def assert_conforms(payload: dict, schema: dict) -> None:
    """Check `payload` against the closed, flat object schemas we publish."""
    unknown = set(payload) - set(schema["properties"])
    assert not unknown, f"fields not in outputSchema: {unknown}"
    for key, value in payload.items():
        expected = _JSON_TYPES[schema["properties"][key]["type"]]
        assert type(value) is expected, (key, value)


async def call(name: str, arguments: dict):
    """Call a tool over an in-memory MCP session and return the raw result.

    Any structured content is also checked against the tool's advertised
    outputSchema, so every test doubles as a schema-agreement test.
    """
    async with create_connected_server_and_client_session(mcp) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        result = await client.call_tool(name, arguments)
    if result.structuredContent is not None:
        assert_conforms(result.structuredContent, tools[name].outputSchema)
    return result


def payload_of(result) -> dict:
    """Parse the JSON text content that every timekeeper tool result carries."""
    assert len(result.content) == 1
    return json.loads(result.content[0].text)


class TestToolListing:
    async def test_lists_exactly_the_three_tools(self):
        async with create_connected_server_and_client_session(mcp) as client:
            tools = (await client.list_tools()).tools
        assert {tool.name for tool in tools} == TOOL_NAMES

    async def test_annotations_mark_tools_read_only_and_closed_world(self):
        async with create_connected_server_and_client_session(mcp) as client:
            tools = (await client.list_tools()).tools
        for tool in tools:
            assert tool.annotations is not None, tool.name
            assert tool.annotations.title, tool.name
            assert tool.annotations.readOnlyHint is True, tool.name
            assert tool.annotations.openWorldHint is False, tool.name

    async def test_output_schemas_are_closed_typed_objects(self):
        async with create_connected_server_and_client_session(mcp) as client:
            tools = (await client.list_tools()).tools
        for tool in tools:
            schema = tool.outputSchema
            assert schema["type"] == "object", tool.name
            assert schema["additionalProperties"] is False, tool.name
            # Nothing is required: one schema covers success and error payloads.
            assert "required" not in schema, tool.name
            assert set(schema["properties"]) == (
                SUCCESS_FIELDS[tool.name] | ERROR_FIELDS[tool.name]
            ), tool.name
            for field, spec in schema["properties"].items():
                assert spec["type"] in _JSON_TYPES, (tool.name, field)
                assert "default" not in spec, (tool.name, field)
            assert schema["properties"]["error"]["type"] == "string"


class TestSuccessResults:
    async def test_get_current_time(self):
        result = await call("get_current_time", {"timezone": "America/New_York"})
        payload = payload_of(result)
        assert result.isError is False
        assert payload == result.structuredContent
        assert set(payload) == SUCCESS_FIELDS["get_current_time"]
        assert payload["timezone"] == "America/New_York"
        local = datetime.fromisoformat(payload["local_iso"])
        assert payload["human_readable_24h"].endswith(
            "EDT" if local.utcoffset().total_seconds() == -4 * 3600 else "EST"
        )

    async def test_get_current_time_defaults_to_utc(self):
        result = await call("get_current_time", {})
        payload = payload_of(result)
        assert result.isError is False
        assert payload["timezone"] == "UTC"
        assert payload["human_readable_24h"].endswith(" UTC")

    async def test_convert_time(self):
        result = await call(
            "convert_time",
            {"iso_timestamp": "2026-05-08T03:59:00Z", "target_timezone": "America/Los_Angeles"},
        )
        payload = payload_of(result)
        assert result.isError is False
        assert payload == result.structuredContent
        assert payload == {
            "original": "2026-05-08T03:59:00Z",
            "converted_iso": "2026-05-07T20:59:00-07:00",
            "timezone": "America/Los_Angeles",
            "weekday_number": 3,
            "month_number": 5,
            "human_readable_24h": "Thursday, May 07, 2026 at 20:59 PDT",
            "human_readable_12h": "Thursday, May 07, 2026 at 08:59 PM PDT",
        }

    async def test_time_until(self):
        result = await call("time_until", {"iso_timestamp": "2000-01-01T00:00:00Z"})
        payload = payload_of(result)
        assert result.isError is False
        assert payload == result.structuredContent
        assert payload["is_past"] is True
        assert payload["total_seconds"] < 0
        assert set(payload) == SUCCESS_FIELDS["time_until"]


class TestZoneAbbreviations:
    @pytest.mark.parametrize("instant,expected_24h,expected_12h", [
        # The repeated 01:30 on the fall-back night: first EDT, then EST.
        ("2026-11-01T05:30:00Z",
         "Sunday, November 01, 2026 at 01:30 EDT",
         "Sunday, November 01, 2026 at 01:30 AM EDT"),
        ("2026-11-01T06:30:00Z",
         "Sunday, November 01, 2026 at 01:30 EST",
         "Sunday, November 01, 2026 at 01:30 AM EST"),
    ])
    async def test_fall_back_hour_shows_edt_then_est(self, instant, expected_24h, expected_12h):
        result = await call(
            "convert_time", {"iso_timestamp": instant, "target_timezone": "America/New_York"}
        )
        payload = payload_of(result)
        assert result.isError is False
        assert payload["human_readable_24h"] == expected_24h
        assert payload["human_readable_12h"] == expected_12h


class TestDomainErrors:
    async def test_unknown_timezone(self):
        result = await call("get_current_time", {"timezone": "Mars/Olympus_Mons"})
        payload = payload_of(result)
        assert result.isError is True
        assert payload == result.structuredContent
        assert set(payload) == ERROR_FIELDS["get_current_time"]
        assert "Unknown timezone" in payload["error"]
        assert payload["timezone"] == "Mars/Olympus_Mons"

    async def test_convert_malformed_timestamp(self):
        result = await call(
            "convert_time", {"iso_timestamp": "not-a-date", "target_timezone": "UTC"}
        )
        payload = payload_of(result)
        assert result.isError is True
        assert payload == result.structuredContent
        assert set(payload) == ERROR_FIELDS["convert_time"]
        assert "Could not parse" in payload["error"]
        assert payload["iso_timestamp"] == "not-a-date"
        assert payload["target_timezone"] == "UTC"

    async def test_convert_out_of_range(self):
        result = await call(
            "convert_time",
            {"iso_timestamp": "9999-12-31T23:59:59+00:00", "target_timezone": "Asia/Tokyo"},
        )
        payload = payload_of(result)
        assert result.isError is True
        assert "representable date range" in payload["error"]

    async def test_time_until_malformed_timestamp(self):
        result = await call("time_until", {"iso_timestamp": "yesterday"})
        payload = payload_of(result)
        assert result.isError is True
        assert payload == result.structuredContent
        assert set(payload) == ERROR_FIELDS["time_until"]
        assert payload["iso_timestamp"] == "yesterday"

    async def test_oversized_input_gives_bounded_response(self):
        result = await call("get_current_time", {"timezone": "x" * 100_000})
        payload = payload_of(result)
        assert result.isError is True
        assert len(result.content[0].text) < 1_000
        assert payload["timezone"] == "x" * 64 + "..."


class TestUnexpectedErrors:
    @pytest.mark.parametrize("name,core_function,arguments", [
        ("get_current_time", "get_time_in_zone", {"timezone": "UTC"}),
        ("convert_time", "convert_to_zone",
         {"iso_timestamp": "2026-05-08T03:59:00Z", "target_timezone": "UTC"}),
        ("time_until", "time_until", {"iso_timestamp": "2026-05-08T03:59:00Z"}),
    ])
    async def test_internal_details_are_not_disclosed(
        self, monkeypatch, caplog, name, core_function, arguments
    ):
        def boom(*args, **kwargs):
            raise RuntimeError("SYNTHETIC_SECRET at /fake/internal/path.py")

        monkeypatch.setattr(core, core_function, boom)
        result = await call(name, arguments)
        payload = payload_of(result)

        assert result.isError is True
        assert payload == {"error": "Internal error", **arguments}
        assert "SYNTHETIC_SECRET" not in result.content[0].text
        assert "/fake/internal" not in result.content[0].text
        # The detail is logged server-side (stderr) for the operator.
        assert "SYNTHETIC_SECRET" in caplog.text


class TestSchemaEnforcement:
    # Each drift carries synthetic secret/path markers that must never reach
    # the client, even though they would appear in raw validation details.
    @pytest.mark.parametrize("name,core_function,arguments,drift", [
        ("time_until", "time_until", {"iso_timestamp": "2026-05-08T03:59:00Z"},
         {"total_seconds": "SYNTHETIC_SECRET /fake/internal/path"}),     # wrong type
        ("time_until", "time_until", {"iso_timestamp": "2026-05-08T03:59:00Z"},
         {"SYNTHETIC_SECRET_FIELD": "/fake/internal/path"}),             # extra field
        ("time_until", "time_until", {"iso_timestamp": "2026-05-08T03:59:00Z"},
         {"is_past": None, "now_iso": "SYNTHETIC_SECRET /fake/internal/path"}),  # null
        ("get_current_time", "get_time_in_zone", {"timezone": "UTC"},
         {"SYNTHETIC_SECRET_FIELD": "/fake/internal/path"}),
        ("convert_time", "convert_to_zone",
         {"iso_timestamp": "2026-05-08T03:59:00Z", "target_timezone": "UTC"},
         {"weekday_number": "SYNTHETIC_SECRET /fake/internal/path"}),
    ])
    async def test_invalid_internal_output_becomes_safe_internal_error(
        self, monkeypatch, caplog, name, core_function, arguments, drift
    ):
        real = getattr(core, core_function)

        def drifted(*args, **kwargs):
            return {**real(*args, **kwargs), **drift}

        monkeypatch.setattr(core, core_function, drifted)
        result = await call(name, arguments)
        payload = payload_of(result)

        assert result.isError is True
        assert payload == {"error": "Internal error", **arguments}
        assert payload == result.structuredContent
        assert "SYNTHETIC_SECRET" not in result.content[0].text
        assert "/fake/internal" not in result.content[0].text
        assert "validation error" not in result.content[0].text
        # Diagnostics go to the server log instead.
        assert f"{name} failed" in caplog.text
        assert "validation error" in caplog.text


class TestDeterministicCurrentTime:
    async def test_repeated_fall_back_hour_with_counting_clock(self, monkeypatch):
        # Expected values below were computed independently with the system
        # `date` utility, not with Python's datetime or timekeeper.core.
        instants = iter([
            datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc),
            datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc),
        ])
        reads = []

        def counting_clock():
            reads.append(1)
            return next(instants)

        real = core.get_time_in_zone
        monkeypatch.setattr(
            core, "get_time_in_zone",
            lambda tz_name: real(tz_name, clock_fn=counting_clock),
        )

        first = payload_of(await call("get_current_time", {"timezone": "America/New_York"}))
        assert len(reads) == 1
        second = payload_of(await call("get_current_time", {"timezone": "America/New_York"}))
        assert len(reads) == 2

        assert first == {
            "utc_iso": "2026-11-01T05:30:00+00:00",
            "local_iso": "2026-11-01T01:30:00-04:00",
            "timezone": "America/New_York",
            "weekday_number": 6,
            "month_number": 11,
            "unix_timestamp": 1793511000,
            "human_readable_24h": "Sunday, November 01, 2026 at 01:30 EDT",
            "human_readable_12h": "Sunday, November 01, 2026 at 01:30 AM EDT",
        }
        assert second == {
            "utc_iso": "2026-11-01T06:30:00+00:00",
            "local_iso": "2026-11-01T01:30:00-05:00",
            "timezone": "America/New_York",
            "weekday_number": 6,
            "month_number": 11,
            "unix_timestamp": 1793514600,
            "human_readable_24h": "Sunday, November 01, 2026 at 01:30 EST",
            "human_readable_12h": "Sunday, November 01, 2026 at 01:30 AM EST",
        }


class TestSdkGeneratedErrors:
    # These are rejected by the MCP SDK before any timekeeper code runs, so
    # they carry plain text rather than the JSON error payload.
    @pytest.mark.parametrize("name,arguments", [
        ("convert_time", {"iso_timestamp": "2026-05-08T03:59:00Z"}),  # missing
        ("time_until", {}),                                            # missing
        ("get_current_time", {"timezone": 5}),                         # wrong type
        ("no_such_tool", {}),                                          # unknown tool
    ])
    async def test_plain_text_error_results(self, name, arguments):
        result = await call(name, arguments)
        assert result.isError is True
        assert result.structuredContent is None
        assert not result.content[0].text.lstrip().startswith("{")
