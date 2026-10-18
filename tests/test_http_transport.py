"""Tests for the HTTP transport configuration: security and sessions.

Unit tests check `configure_http()` and the stdio/HTTP dispatch in-process.
Behavioral tests start the real entrypoint (`python -m timekeeper`) as a
subprocess on an ephemeral loopback port (see `tests/harness.py`) and talk
to it over HTTP. Full tool-call flows are in `test_http_integration.py`.

The `mcp_remote` test is opt-in (TIMEKEEPER_RUN_MCP_REMOTE=1) because it
downloads a pinned `mcp-remote` from the npm registry.
"""
from __future__ import annotations

import json
import os
import shutil
import time

import anyio
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.version import SUPPORTED_PROTOCOL_VERSIONS

import timekeeper.__main__ as entry
from tests.harness import (
    TRANSPORT_ENV_VARS, delete_session, initialize, initialize_body, post,
)
from timekeeper.server import mcp

MCP_REMOTE_VERSION = "0.15.1"


# --- configure_http() and dispatch (in-process) ------------------------------

@pytest.fixture
def clean_http_env(monkeypatch):
    for name in TRANSPORT_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.fixture
def isolated_settings(monkeypatch):
    """Let tests mutate `mcp.settings` without leaking into other tests."""
    monkeypatch.setattr(mcp, "settings", mcp.settings.model_copy(deep=True))
    return mcp.settings


def test_configure_http_defaults(clean_http_env, isolated_settings):
    entry.configure_http(mcp)
    s = mcp.settings
    assert (s.host, s.port) == ("127.0.0.1", 8765)
    assert s.transport_security.enable_dns_rebinding_protection is True
    assert s.transport_security.allowed_hosts == ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    assert s.transport_security.allowed_origins == [
        "http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*",
    ]
    assert s.max_request_body_size == 64 * 1024
    assert (s.stateless_http, s.json_response) == (False, False)
    assert (s.session_idle_timeout, s.max_sessions) == (600, 200)


def test_configure_http_extends_allowlists_from_env(clean_http_env, isolated_settings):
    clean_http_env.setenv("MCP_ALLOWED_HOSTS", " time.example.org, alt.example:8443 ,,")
    clean_http_env.setenv("MCP_ALLOWED_ORIGINS", "https://time.example.org")
    entry.configure_http(mcp)
    security = mcp.settings.transport_security
    assert security.allowed_hosts[-2:] == ["time.example.org", "alt.example:8443"]
    assert security.allowed_hosts[:3] == ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    assert security.allowed_origins[-1] == "https://time.example.org"
    assert len(security.allowed_origins) == 4


def test_stdio_leaves_http_settings_untouched(clean_http_env, isolated_settings, monkeypatch):
    before = mcp.settings.model_dump()
    calls = []
    monkeypatch.setattr(mcp, "run", lambda *args, **kwargs: calls.append((args, kwargs)))
    clean_http_env.setenv("MCP_ALLOWED_HOSTS", "time.example.org")  # must be ignored
    entry.main()
    assert calls == [((), {})]
    assert mcp.settings.model_dump() == before


def test_http_configures_before_running(clean_http_env, isolated_settings, monkeypatch):
    seen = []
    monkeypatch.setattr(
        mcp, "run",
        lambda *args, **kwargs: seen.append((args, kwargs, mcp.settings.max_request_body_size)),
    )
    clean_http_env.setenv("MCP_TRANSPORT", "http")
    entry.main()
    assert seen == [((), {"transport": "streamable-http"}, 64 * 1024)]


# --- real entrypoint over HTTP ------------------------------------------------

@pytest.fixture(scope="module")
def server(http_server):
    return http_server.url


@pytest.fixture(scope="module")
def deployed_server(deployed_http_server):
    """Configured as if behind a proxy for time.example.org."""
    return deployed_http_server.url


def test_complete_initialized_flow(server):
    async def flow():
        async with streamable_http_client(f"{server}/mcp") as (read, write, get_session_id):
            async with ClientSession(read, write) as client:
                init = await client.initialize()
                assert init.protocolVersion in SUPPORTED_PROTOCOL_VERSIONS
                assert get_session_id()  # stateful mode issues a session ID
                tools = {tool.name for tool in (await client.list_tools()).tools}
                assert tools == {"get_current_time", "convert_time", "time_until"}
                for name, args, expect_error in [
                    ("get_current_time", {"timezone": "America/New_York"}, False),
                    ("get_current_time", {"timezone": "Mars/X"}, True),
                    ("convert_time", {"iso_timestamp": "2026-11-01T06:30:00Z",
                                      "target_timezone": "America/New_York"}, False),
                    ("time_until", {"iso_timestamp": "nope"}, True),
                ]:
                    result = await client.call_tool(name, args)
                    payload = json.loads(result.content[0].text)
                    assert result.isError is expect_error, name
                    assert payload == result.structuredContent
                    assert ("error" in payload) is expect_error

    anyio.run(flow)


class TestHostValidation:
    def test_loopback_hosts_allowed(self, server):
        port = server.rsplit(":", 1)[1]
        assert initialize(server, host=f"127.0.0.1:{port}").status_code == 200
        assert initialize(server, host=f"localhost:{port}").status_code == 200

    def test_public_host_rejected_by_default(self, server):
        assert initialize(server, host="time.example.org").status_code == 421

    def test_configured_host_allowed(self, deployed_server):
        assert initialize(deployed_server, host="time.example.org").status_code == 200

    def test_unlisted_host_still_rejected_when_configured(self, deployed_server):
        assert initialize(deployed_server, host="other.example.org").status_code == 421

    def test_forwarded_host_cannot_bypass_validation(self, server):
        port = server.rsplit(":", 1)[1]
        spoofed = initialize(server, host="evil.example",
                             headers={"X-Forwarded-Host": f"127.0.0.1:{port}"})
        assert spoofed.status_code == 421
        ignored = initialize(server, headers={"X-Forwarded-Host": "evil.example"})
        assert ignored.status_code == 200


class TestOriginValidation:
    def test_absent_origin_allowed(self, server):
        assert initialize(server).status_code == 200

    def test_loopback_origin_allowed(self, server):
        port = server.rsplit(":", 1)[1]
        response = initialize(server, headers={"Origin": f"http://localhost:{port}"})
        assert response.status_code == 200

    def test_unlisted_origin_rejected(self, server):
        response = initialize(server, headers={"Origin": "https://evil.example"})
        assert response.status_code == 403

    def test_configured_origin_allowed_only_where_configured(self, server, deployed_server):
        origin = {"Origin": "https://time.example.org"}
        assert initialize(deployed_server, host="time.example.org", headers=origin).status_code == 200
        assert initialize(server, headers=origin).status_code == 403


class TestRequestBodyLimit:
    LIMIT = 64 * 1024

    def _padded_initialize(self, size: int) -> bytes:
        body = initialize_body("x")
        base = len(json.dumps(body).encode())
        body["params"]["clientInfo"]["name"] = "x" * (size - base + 1)
        encoded = json.dumps(body).encode()
        assert len(encoded) == size
        return encoded

    def test_body_at_limit_accepted(self, server):
        assert post(server, self._padded_initialize(self.LIMIT)).status_code == 200

    def test_body_over_limit_rejected(self, server):
        response = post(server, self._padded_initialize(self.LIMIT + 1))
        assert response.status_code == 413

    def test_streamed_body_over_limit_rejected(self, server):
        # No Content-Length: the limit must hold for chunked bodies too.
        chunks = iter([b"x" * 1024] * 65)
        assert post(server, chunks).status_code == 413


class TestStatefulSessions:
    def test_each_initialize_gets_a_distinct_session(self, server):
        first = initialize(server).headers["mcp-session-id"]
        second = initialize(server).headers["mcp-session-id"]
        assert first and second and first != second

    def test_unknown_session_id_is_404(self, server):
        ping = {"jsonrpc": "2.0", "id": 2, "method": "ping"}
        assert post(server, ping, session_id="0" * 32).status_code == 404

    def test_deleted_session_cannot_be_reused(self, server):
        session_id = initialize(server).headers["mcp-session-id"]
        assert delete_session(server, session_id).status_code == 200
        ping = {"jsonrpc": "2.0", "id": 2, "method": "ping"}
        assert post(server, ping, session_id=session_id).status_code == 404

    def test_session_cap_returns_503(self, start_http_server):
        # A fresh server, so other tests' sessions don't count toward the cap.
        url = start_http_server().url
        for _ in range(200):
            assert initialize(url).status_code == 200
        assert initialize(url).status_code == 503

    def test_idle_session_expires(self, start_http_server):
        # Same entrypoint with the idle timeout shortened to 1 second.
        launcher = (
            "import timekeeper.__main__ as entry; "
            "entry.SESSION_IDLE_TIMEOUT_SECONDS = 1; entry.main()"
        )
        url = start_http_server(["-c", launcher]).url
        session_id = initialize(url).headers["mcp-session-id"]
        # Any request resets the idle timer, so leave the session untouched
        # for well over the timeout before probing it once.
        time.sleep(3)
        ping = {"jsonrpc": "2.0", "id": 2, "method": "ping"}
        assert post(url, ping, session_id=session_id).status_code == 404


# --- compatibility gate, automated part (opt-in) -----------------------------

@pytest.mark.mcp_remote
@pytest.mark.skipif(
    os.environ.get("TIMEKEEPER_RUN_MCP_REMOTE") != "1" or shutil.which("npx") is None,
    reason="set TIMEKEEPER_RUN_MCP_REMOTE=1 and install npx to run",
)
def test_mcp_remote_proxy_flow(server, tmp_path):
    """Drive the server through the pinned mcp-remote stdio proxy."""
    params = StdioServerParameters(
        command="npx",
        args=["-y", f"mcp-remote@{MCP_REMOTE_VERSION}", f"{server}/mcp",
              "--transport", "http-only"],
        env={
            "PATH": os.environ["PATH"],
            "HOME": str(tmp_path),  # keep mcp-remote state out of the real home
            "MCP_REMOTE_CONFIG_DIR": str(tmp_path / "mcp-auth"),
            "npm_config_cache": os.environ.get("npm_config_cache", str(tmp_path / "npm")),
            "npm_config_update_notifier": "false",
        },
    )

    async def flow():
        with open(tmp_path / "mcp-remote.log", "w") as errlog:
            async with stdio_client(params, errlog=errlog) as (read, write):
                async with ClientSession(read, write) as client:
                    with anyio.fail_after(120):
                        await client.initialize()
                        tools = {tool.name for tool in (await client.list_tools()).tools}
                        assert tools == {"get_current_time", "convert_time", "time_until"}
                        for name, args, expect_error in [
                            ("get_current_time", {"timezone": "UTC"}, False),
                            ("get_current_time", {"timezone": "Mars/X"}, True),
                            ("convert_time", {"iso_timestamp": "2026-05-08T03:59:00Z",
                                              "target_timezone": "Asia/Tokyo"}, False),
                            ("convert_time", {"iso_timestamp": "nope",
                                              "target_timezone": "UTC"}, True),
                            ("time_until", {"iso_timestamp": "2000-01-01T00:00:00Z"}, False),
                            ("time_until", {"iso_timestamp": "nope"}, True),
                        ]:
                            result = await client.call_tool(name, args)
                            payload = json.loads(result.content[0].text)
                            assert result.isError is expect_error, name
                            assert payload == result.structuredContent

    anyio.run(flow)
