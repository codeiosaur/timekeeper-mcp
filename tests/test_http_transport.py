"""Phase 3 acceptance tests for the HTTP transport configuration.

Unit tests check `configure_http()` and the stdio/HTTP dispatch in-process.
Behavioral tests start the real entrypoint (`python -m timekeeper`) as a
subprocess on an ephemeral loopback port and talk to it over HTTP.

The `mcp_remote` test is opt-in (TIMEKEEPER_RUN_MCP_REMOTE=1) because it
downloads a pinned `mcp-remote` from the npm registry.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import anyio
import httpx
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

import timekeeper.__main__ as entry
from timekeeper.server import mcp

PROJECT_ROOT = Path(__file__).parent.parent
PROTOCOL_VERSION = "2025-11-25"
MCP_REMOTE_VERSION = "0.15.1"
HTTP_ENV_VARS = (
    "MCP_TRANSPORT", "MCP_HOST", "MCP_PORT",
    "MCP_ALLOWED_HOSTS", "MCP_ALLOWED_ORIGINS",
)


# --- helpers -----------------------------------------------------------------

def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def running_server(tmp_path: Path, python_args: list[str] | None = None, **env_overrides):
    """Run the HTTP entrypoint on an ephemeral port; yield its base URL."""
    port = _free_port()
    env = {k: v for k, v in os.environ.items() if k not in HTTP_ENV_VARS}
    env.update(MCP_TRANSPORT="http", MCP_PORT=str(port), **env_overrides)
    log = tmp_path / f"server-{port}.log"
    with open(log, "w") as log_file:
        proc = subprocess.Popen(
            [sys.executable, *(python_args or ["-m", "timekeeper"])],
            cwd=PROJECT_ROOT, env=env, stdout=log_file, stderr=subprocess.STDOUT,
        )
    try:
        deadline = time.monotonic() + 15
        while True:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                if proc.poll() is not None or time.monotonic() > deadline:
                    pytest.fail(f"server did not start:\n{log.read_text()[-2000:]}")
                time.sleep(0.05)
        yield f"http://127.0.0.1:{port}"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def _initialize_body(client_name: str = "phase3-test") -> dict:
    return {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": client_name, "version": "0"},
        },
    }


def post(base_url: str, body, *, host: str | None = None, headers: dict | None = None,
         session_id: str | None = None) -> httpx.Response:
    """POST to /mcp. `host` overrides the Host header.

    A dict `body` is JSON-encoded; bytes or a byte iterator are sent as-is
    (an iterator is sent chunked, without Content-Length).
    """
    all_headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": PROTOCOL_VERSION,
    }
    if host is not None:
        all_headers["Host"] = host
    if session_id is not None:
        all_headers["Mcp-Session-Id"] = session_id
    all_headers.update(headers or {})
    content = json.dumps(body) if isinstance(body, dict) else body
    return httpx.post(f"{base_url}/mcp", content=content, headers=all_headers, timeout=10)


def _initialize(base_url: str, **kwargs) -> httpx.Response:
    return post(base_url, _initialize_body(), **kwargs)


# --- configure_http() and dispatch (in-process) ------------------------------

@pytest.fixture
def clean_http_env(monkeypatch):
    for name in HTTP_ENV_VARS:
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
def server(tmp_path_factory):
    with running_server(tmp_path_factory.mktemp("default")) as url:
        yield url


@pytest.fixture(scope="module")
def deployed_server(tmp_path_factory):
    """Configured as if behind a proxy for time.example.org."""
    with running_server(
        tmp_path_factory.mktemp("deployed"),
        MCP_ALLOWED_HOSTS="time.example.org",
        MCP_ALLOWED_ORIGINS="https://time.example.org",
    ) as url:
        yield url


def test_complete_initialized_flow(server):
    async def flow():
        async with streamable_http_client(f"{server}/mcp") as (read, write, get_session_id):
            async with ClientSession(read, write) as client:
                init = await client.initialize()
                assert init.protocolVersion == PROTOCOL_VERSION
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
        assert _initialize(server, host=f"127.0.0.1:{port}").status_code == 200
        assert _initialize(server, host=f"localhost:{port}").status_code == 200

    def test_public_host_rejected_by_default(self, server):
        assert _initialize(server, host="time.example.org").status_code == 421

    def test_configured_host_allowed(self, deployed_server):
        assert _initialize(deployed_server, host="time.example.org").status_code == 200

    def test_unlisted_host_still_rejected_when_configured(self, deployed_server):
        assert _initialize(deployed_server, host="other.example.org").status_code == 421

    def test_forwarded_host_cannot_bypass_validation(self, server):
        port = server.rsplit(":", 1)[1]
        spoofed = _initialize(server, host="evil.example",
                              headers={"X-Forwarded-Host": f"127.0.0.1:{port}"})
        assert spoofed.status_code == 421
        ignored = _initialize(server, headers={"X-Forwarded-Host": "evil.example"})
        assert ignored.status_code == 200


class TestOriginValidation:
    def test_absent_origin_allowed(self, server):
        assert _initialize(server).status_code == 200

    def test_loopback_origin_allowed(self, server):
        port = server.rsplit(":", 1)[1]
        response = _initialize(server, headers={"Origin": f"http://localhost:{port}"})
        assert response.status_code == 200

    def test_unlisted_origin_rejected(self, server):
        response = _initialize(server, headers={"Origin": "https://evil.example"})
        assert response.status_code == 403

    def test_configured_origin_allowed_only_where_configured(self, server, deployed_server):
        origin = {"Origin": "https://time.example.org"}
        assert _initialize(deployed_server, host="time.example.org", headers=origin).status_code == 200
        assert _initialize(server, headers=origin).status_code == 403


class TestRequestBodyLimit:
    LIMIT = 64 * 1024

    def _padded_initialize(self, size: int) -> bytes:
        body = _initialize_body("x")
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
        first = _initialize(server).headers["mcp-session-id"]
        second = _initialize(server).headers["mcp-session-id"]
        assert first and second and first != second

    def test_unknown_session_id_is_404(self, server):
        ping = {"jsonrpc": "2.0", "id": 2, "method": "ping"}
        assert post(server, ping, session_id="0" * 32).status_code == 404

    def test_deleted_session_cannot_be_reused(self, server):
        session_id = _initialize(server).headers["mcp-session-id"]
        headers = {"Mcp-Session-Id": session_id, "MCP-Protocol-Version": PROTOCOL_VERSION}
        assert httpx.delete(f"{server}/mcp", headers=headers, timeout=10).status_code == 200
        ping = {"jsonrpc": "2.0", "id": 2, "method": "ping"}
        assert post(server, ping, session_id=session_id).status_code == 404

    def test_session_cap_returns_503(self, tmp_path):
        # A fresh server, so other tests' sessions don't count toward the cap.
        with running_server(tmp_path) as url:
            for _ in range(200):
                assert _initialize(url).status_code == 200
            assert _initialize(url).status_code == 503

    def test_idle_session_expires(self, tmp_path):
        # Same entrypoint with the idle timeout shortened to 1 second.
        launcher = (
            "import timekeeper.__main__ as entry; "
            "entry.SESSION_IDLE_TIMEOUT_SECONDS = 1; entry.main()"
        )
        with running_server(tmp_path, ["-c", launcher]) as url:
            session_id = _initialize(url).headers["mcp-session-id"]
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
