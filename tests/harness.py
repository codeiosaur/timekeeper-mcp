"""Subprocess and raw-HTTP helpers shared by the integration tests.

Servers run the real entrypoint (`python -m timekeeper`) on an ephemeral
loopback port. Child output goes to log files rather than pipes, so it can
never fill a pipe buffer and block the child; logs are read on failure.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
from mcp.types import LATEST_PROTOCOL_VERSION

PROJECT_ROOT = Path(__file__).parent.parent
TRANSPORT_ENV_VARS = (
    "MCP_TRANSPORT", "MCP_HOST", "MCP_PORT",
    "MCP_ALLOWED_HOSTS", "MCP_ALLOWED_ORIGINS",
)
READY_DEADLINE_SECONDS = 10
STOP_WAIT_SECONDS = 5

# Calls exercised over every transport, success and error path per tool:
# (tool, arguments, expect_error, fields expected in the JSON payload)
TOOL_CALLS = [
    ("get_current_time", {"timezone": "America/New_York"}, False,
     {"utc_iso", "local_iso", "timezone", "weekday_number", "month_number",
      "unix_timestamp", "human_readable_24h", "human_readable_12h"}),
    ("get_current_time", {"timezone": "Mars/Olympus_Mons"}, True,
     {"error", "timezone"}),
    ("convert_time", {"iso_timestamp": "2026-11-01T06:30:00Z",
                      "target_timezone": "America/New_York"}, False,
     {"original", "converted_iso", "timezone", "weekday_number", "month_number",
      "human_readable_24h", "human_readable_12h"}),
    ("convert_time", {"iso_timestamp": "not-a-date", "target_timezone": "UTC"}, True,
     {"error", "iso_timestamp", "target_timezone"}),
    ("time_until", {"iso_timestamp": "2000-01-01T00:00:00Z"}, False,
     {"target_iso", "now_iso", "is_past", "total_seconds",
      "days", "hours", "minutes", "seconds"}),
    ("time_until", {"iso_timestamp": "yesterday"}, True,
     {"error", "iso_timestamp"}),
]



class HarnessError(RuntimeError):
    """The server subprocess failed to start or exited unexpectedly."""


def free_port() -> int:
    """Return a loopback port that was free a moment ago (the OS picks it)."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def port_in_use(port: int) -> bool:
    """True if another socket holds `port` on loopback.

    Checked by trying to bind it (without SO_REUSEADDR), never by connecting,
    so it works the same on every OS and cannot disturb a listener.
    """
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return True
    return False


def child_env(**overrides: str) -> dict[str, str]:
    """The test process's environment minus our transport variables."""
    env = {k: v for k, v in os.environ.items() if k not in TRANSPORT_ENV_VARS}
    env.update(overrides)
    return env


def stop(proc: subprocess.Popen) -> None:
    """Terminate, wait up to STOP_WAIT_SECONDS, then kill if still running."""
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=STOP_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=STOP_WAIT_SECONDS)


@dataclass
class HttpServer:
    url: str          # base URL, e.g. http://127.0.0.1:54321
    port: int
    proc: subprocess.Popen
    log: Path

    def log_text(self) -> str:
        return self.log.read_text(errors="replace")


def _wait_until_ready(server: HttpServer) -> None:
    """Poll with an initialize POST until it succeeds, the child exits, or
    READY_DEADLINE_SECONDS pass. The probe's session is deleted afterwards
    so it does not count toward the server's session cap."""
    deadline = time.monotonic() + READY_DEADLINE_SECONDS
    while True:
        if server.proc.poll() is not None:
            raise HarnessError(
                f"server exited with code {server.proc.returncode} before "
                f"becoming ready:\n{server.log_text()[-2000:]}"
            )
        try:
            response = initialize(server.url, timeout=1)
        except httpx.TransportError:
            response = None
        if response is not None and response.status_code == 200:
            session_id = response.headers.get("mcp-session-id")
            if session_id:
                delete_session(server.url, session_id)
            return
        if time.monotonic() > deadline:
            raise HarnessError(
                f"server not ready within {READY_DEADLINE_SECONDS}s:\n"
                f"{server.log_text()[-2000:]}"
            )
        time.sleep(0.05)


@contextmanager
def running_http_server(log_dir: Path, python_args: list[str] | None = None,
                        **env_overrides: str):
    """Start the HTTP entrypoint on an ephemeral port; yield an HttpServer.

    If the child exits before becoming ready because another process took
    the chosen port in the meantime, retry once on a new port. The child is
    always stopped on exit from this context, however that exit happens.
    """
    for attempt in range(2):
        port = free_port()
        log = log_dir / f"http-server-{port}.log"
        env = child_env(**{"MCP_TRANSPORT": "http", "MCP_PORT": str(port), **env_overrides})
        with open(log, "w") as log_file:
            proc = subprocess.Popen(
                [sys.executable, *(python_args or ["-m", "timekeeper"])],
                cwd=PROJECT_ROOT, env=env,
                stdin=subprocess.DEVNULL, stdout=log_file, stderr=subprocess.STDOUT,
            )
        server = HttpServer(f"http://127.0.0.1:{port}", port, proc, log)
        try:
            try:
                _wait_until_ready(server)
            except HarnessError:
                if attempt == 0 and proc.poll() is not None and port_in_use(port):
                    continue  # the finally below stops this child first
                raise
            yield server
            return
        finally:
            stop(proc)


# --- raw MCP-over-HTTP requests ---------------------------------------------

def initialize_body(client_name: str = "timekeeper-tests") -> dict:
    return {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": LATEST_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": client_name, "version": "0"},
        },
    }


def post(base_url: str, body, *, host: str | None = None, headers: dict | None = None,
         session_id: str | None = None, timeout: float = 10) -> httpx.Response:
    """POST to /mcp. `host` overrides the Host header.

    A dict `body` is JSON-encoded; bytes or a byte iterator are sent as-is
    (an iterator is sent chunked, without Content-Length).
    """
    all_headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": LATEST_PROTOCOL_VERSION,
    }
    if host is not None:
        all_headers["Host"] = host
    if session_id is not None:
        all_headers["Mcp-Session-Id"] = session_id
    all_headers.update(headers or {})
    content = json.dumps(body) if isinstance(body, dict) else body
    return httpx.post(f"{base_url}/mcp", content=content, headers=all_headers,
                      timeout=timeout)


def initialize(base_url: str, **kwargs) -> httpx.Response:
    return post(base_url, initialize_body(), **kwargs)


def delete_session(base_url: str, session_id: str) -> httpx.Response:
    headers = {"Mcp-Session-Id": session_id, "MCP-Protocol-Version": LATEST_PROTOCOL_VERSION}
    return httpx.delete(f"{base_url}/mcp", headers=headers, timeout=10)
