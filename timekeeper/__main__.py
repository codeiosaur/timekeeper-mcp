"""Entry point. Selects MCP transport from environment variables.

Usage:
    # Default: stdio
    python -m timekeeper

    # HTTP on loopback (127.0.0.1:8765 by default)
    MCP_TRANSPORT=http python -m timekeeper

    # HTTP behind a reverse proxy that forwards the public Host header
    MCP_TRANSPORT=http MCP_ALLOWED_HOSTS=time.example.org python -m timekeeper

HTTP environment variables (all optional; stdio ignores them):
    MCP_HOST             Bind address. Default 127.0.0.1 (loopback only).
    MCP_PORT             Bind port. Default 8765.
    MCP_ALLOWED_HOSTS    Comma-separated extra Host header values to accept,
                         e.g. "time.example.org" or "time.example.org:8443".
                         A "host:*" entry accepts any port on that host.
    MCP_ALLOWED_ORIGINS  Comma-separated extra Origin values to accept for
                         browser clients, e.g. "https://time.example.org".

Loopback hosts and origins are always allowed, and DNS-rebinding protection
is always on: a request whose Host is not allowed gets 421, and a request
that sends an Origin that is not allowed gets 403. Requests without an
Origin header (non-browser MCP clients) are accepted. There is no
authentication; the time tools are intentionally anonymous.

For production HTTP deployment, keep the default loopback bind and put nginx
or a similar service in front for TLS termination. Do not expose this
process directly to the public internet.
"""
from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING

from mcp.server.transport_security import TransportSecuritySettings

from timekeeper.server import mcp

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

# Always-allowed loopback values (the same ones the SDK uses by default).
_LOOPBACK_HOSTS = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
_LOOPBACK_ORIGINS = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]

# Requests are a few hundred bytes; the SDK default is 4 MiB.
MAX_REQUEST_BODY_BYTES = 64 * 1024
# Stateful Streamable HTTP session bounds (see README "HTTP transport").
SESSION_IDLE_TIMEOUT_SECONDS = 600
MAX_SESSIONS = 200


def _env_list(name: str) -> list[str]:
    """Return the non-empty, stripped entries of a comma-separated variable."""
    return [item.strip() for item in os.environ.get(name, "").split(",") if item.strip()]


def configure_http(server: FastMCP) -> None:
    """Apply HTTP-only settings from the environment to `server`.

    Must run before the HTTP app is built: FastMCP reads these settings once,
    when `run(transport="streamable-http")` creates its session manager.
    """
    settings = server.settings
    settings.host = os.environ.get("MCP_HOST", "127.0.0.1")
    settings.port = int(os.environ.get("MCP_PORT", "8765"))
    settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=_LOOPBACK_HOSTS + _env_list("MCP_ALLOWED_HOSTS"),
        allowed_origins=_LOOPBACK_ORIGINS + _env_list("MCP_ALLOWED_ORIGINS"),
    )
    settings.max_request_body_size = MAX_REQUEST_BODY_BYTES
    settings.session_idle_timeout = SESSION_IDLE_TIMEOUT_SECONDS
    settings.max_sessions = MAX_SESSIONS


def main() -> None:
    transport = os.environ.get("MCP_TRANSPORT", "stdio").lower()

    if transport == "stdio":
        mcp.run()
        return

    if transport == "http":
        configure_http(mcp)
        mcp.run(transport="streamable-http")
        return

    sys.exit(f"Unknown MCP_TRANSPORT={transport!r}. Use 'stdio' or 'http'.")


if __name__ == "__main__":
    main()
