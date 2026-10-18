"""End-to-end tests over the Streamable HTTP transport.

The real entrypoint runs as a subprocess on an ephemeral loopback port (see
`tests/harness.py`). Tool calls go through the official MCP SDK client after
a complete initialization; raw protocol edge cases use httpx. Transport
security and session limits are covered in `test_http_transport.py`.
"""
from __future__ import annotations

import json
import socket
import sys

import anyio
import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.version import SUPPORTED_PROTOCOL_VERSIONS

from tests import harness
from tests.harness import (
    PROJECT_ROOT, STOP_WAIT_SECONDS, TOOL_CALLS, HarnessError, initialize, post,
    running_http_server,
)


async def _run_tool_calls(url: str):
    """Initialize an SDK client session and call every entry of TOOL_CALLS."""
    results = []
    with anyio.fail_after(30):
        async with streamable_http_client(f"{url}/mcp") as (read, write, _):
            async with ClientSession(read, write) as client:
                init = await client.initialize()
                tools = {tool.name for tool in (await client.list_tools()).tools}
                for name, arguments, _expect_error, _fields in TOOL_CALLS:
                    results.append(await client.call_tool(name, arguments))
    return init, tools, results


def test_all_tools_over_initialized_sdk_session(http_server):
    init, tools, results = anyio.run(_run_tool_calls, http_server.url)

    assert init.protocolVersion in SUPPORTED_PROTOCOL_VERSIONS
    assert init.serverInfo.name == "timekeeper"
    assert tools == {"get_current_time", "convert_time", "time_until"}
    for (name, arguments, expect_error, fields), result in zip(TOOL_CALLS, results):
        assert result.isError is expect_error, (name, arguments)
        payload = json.loads(result.content[0].text)
        assert set(payload) == fields, (name, arguments)
        assert payload == result.structuredContent
        if expect_error:
            assert isinstance(payload["error"], str) and payload["error"]
            for key, value in arguments.items():
                assert payload[key] == value

    conversion = results[2].structuredContent
    assert conversion["converted_iso"] == "2026-11-01T01:30:00-05:00"
    assert conversion["human_readable_24h"] == "Sunday, November 01, 2026 at 01:30 EST"


def test_malformed_json_is_rejected_without_internal_details(http_server):
    response = post(http_server.url, b'{"jsonrpc": "2.0", "id": 1, "method": ')
    assert response.status_code == 400
    body = response.text
    assert "Traceback" not in body
    assert str(PROJECT_ROOT) not in body
    assert ".py" not in body
    assert response.json()["error"]["code"] == -32700  # JSON-RPC parse error


def test_only_the_mcp_path_is_served(http_server):
    assert httpx.get(f"{http_server.url}/", timeout=10).status_code == 404
    assert httpx.post(f"{http_server.url}/not-mcp", content=b"{}", timeout=10).status_code == 404


@pytest.mark.skipif(sys.platform == "win32", reason="SIGTERM semantics are POSIX")
def test_sigterm_stops_server_and_releases_port(start_http_server):
    server = start_http_server()
    server.proc.terminate()
    server.proc.wait(timeout=STOP_WAIT_SECONDS)
    assert server.proc.returncode is not None
    # No listener remains: the port can be bound again (checked without
    # contacting anything). SO_REUSEADDR, as servers use, ignores leftover
    # TIME_WAIT connections but still fails if a listener holds the port.
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", server.port))


def test_harness_reports_a_child_that_exits_early(start_http_server):
    with pytest.raises(HarnessError, match="exited with code"):
        start_http_server(MCP_TRANSPORT="not-a-transport")


def test_harness_stops_child_when_startup_is_interrupted(tmp_path, monkeypatch):
    class Interrupted(BaseException):  # like KeyboardInterrupt or a timeout signal
        pass

    started = []
    real_popen = harness.subprocess.Popen

    def recording_popen(*args, **kwargs):
        started.append(real_popen(*args, **kwargs))
        return started[-1]

    def interrupted(*args, **kwargs):
        raise Interrupted

    monkeypatch.setattr(harness.subprocess, "Popen", recording_popen)
    monkeypatch.setattr(harness, "initialize", interrupted)
    with pytest.raises(Interrupted):
        with running_http_server(tmp_path):
            pass
    assert len(started) == 1
    assert started[0].poll() is not None  # stopped, not leaked


def test_harness_retries_when_the_chosen_port_is_taken(tmp_path, monkeypatch):
    # A controlled, test-owned socket stands in for another process that
    # grabbed the port between free_port() and the child's bind.
    with socket.socket() as squatter:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # Windows: forbid takeover
            squatter.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        squatter.bind(("127.0.0.1", 0))
        squatter.listen()
        taken = squatter.getsockname()[1]
        ports = iter([taken])
        real_free_port = harness.free_port
        monkeypatch.setattr(harness, "free_port", lambda: next(ports, None) or real_free_port())

        with running_http_server(tmp_path) as server:
            assert server.port != taken
            assert initialize(server.url).status_code == 200
        assert (tmp_path / f"http-server-{taken}.log").exists()
