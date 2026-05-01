"""Entry point. Selects MCP transport from environment variables.

Usage:
    # Default: stdio
    python -m timekeeper

    # HTTP (binds to 127.0.0.1:8765 by default)
    MCP_TRANSPORT=http python -m timekeeper
    MCP_TRANSPORT=http MCP_HOST=0.0.0.0 MCP_PORT=8765 python -m timekeeper

For production HTTP deployment, place nginx or a similar service in front for TLS
termination. Do not expose this process directly to the public internet.
"""
from __future__ import annotations

import os
import sys

from timekeeper.server import mcp


def main() -> None:
    transport = os.environ.get("MCP_TRANSPORT", "stdio").lower()

    if transport == "stdio":
        mcp.run()
        return

    if transport == "http":
        host = os.environ.get("MCP_HOST", "127.0.0.1")
        port = int(os.environ.get("MCP_PORT", "8765"))
        mcp.settings.host = host
        mcp.settings.port = port
        mcp.run(transport="streamable-http")
        return

    sys.exit(f"Unknown MCP_TRANSPORT={transport!r}. Use 'stdio' or 'http'.")


if __name__ == "__main__":
    main()
