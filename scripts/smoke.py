"""Smoke-test a running timekeeper-mcp HTTP endpoint with the official MCP SDK.

Usage:
    python scripts/smoke.py https://time.example.org/mcp
    python scripts/smoke.py http://127.0.0.1:8765/mcp

Opens one initialized MCP session (about five requests) and checks that:
  - initialize succeeds (the SDK then sends the initialized notification);
  - tools/list returns exactly get_current_time, convert_time, time_until;
  - get_current_time(timezone="UTC") succeeds, and reports the clock skew
    between the server's UTC time and this machine's;
  - get_current_time(timezone="Mars/X") fails with isError and a JSON
    object that has an "error" key.

Plain http:// is accepted only for loopback addresses; use https:// for
anything else. The whole run is bounded by --timeout. Exit status: 0 if
every check passed, 1 if a check failed or the endpoint was unreachable,
2 for a bad command line. Run it once after a deploy, not in a loop.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import sys
from datetime import datetime, timezone
from urllib.parse import urlsplit

import anyio
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

EXPECTED_TOOLS = {"get_current_time", "convert_time", "time_until"}
# Skew larger than this is reported as a warning (it does not fail the run).
SKEW_WARNING_SECONDS = 5.0


class SmokeFailure(Exception):
    """A smoke check did not hold."""


def check_url(url: str) -> None:
    """Raise ValueError unless `url` is https://, or http:// to loopback."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"expected an http(s) URL such as https://host/mcp, got {url!r}")
    if parts.scheme == "http":
        host = parts.hostname
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = host == "localhost"
        if not loopback:
            raise ValueError("plain http:// is only accepted for loopback; use https://")


def _payload(result) -> dict:
    return json.loads(result.content[0].text)


async def run_smoke(url: str, timeout: float = 30, out=print) -> None:
    """Run the smoke checks against `url`; raise SmokeFailure on any failure."""
    check_url(url)
    with anyio.fail_after(timeout):
        async with httpx.AsyncClient(timeout=httpx.Timeout(10, read=timeout)) as http_client:
            async with streamable_http_client(url, http_client=http_client) as (read, write, _):
                async with ClientSession(read, write) as session:
                    init = await session.initialize()
                    out(f"OK   initialize: {init.serverInfo.name} {init.serverInfo.version}, "
                        f"protocol {init.protocolVersion}")

                    tools = {tool.name for tool in (await session.list_tools()).tools}
                    if tools != EXPECTED_TOOLS:
                        raise SmokeFailure(f"tools/list returned {sorted(tools)}, "
                                           f"expected {sorted(EXPECTED_TOOLS)}")
                    out(f"OK   tools/list: {', '.join(sorted(tools))}")

                    before = datetime.now(timezone.utc)
                    result = await session.call_tool("get_current_time", {"timezone": "UTC"})
                    after = datetime.now(timezone.utc)
                    if result.isError:
                        raise SmokeFailure(f"get_current_time(UTC) failed: {result.content}")
                    server_utc = datetime.fromisoformat(_payload(result)["utc_iso"])
                    local_mid = before + (after - before) / 2
                    skew = (server_utc - local_mid).total_seconds()
                    round_trip = (after - before).total_seconds()
                    out(f"OK   get_current_time(UTC): server {server_utc.isoformat()}")
                    out(f"INFO clock skew (server - this machine): {skew:+.3f} s "
                        f"(round trip {round_trip:.3f} s)")
                    if abs(skew) > SKEW_WARNING_SECONDS + round_trip:
                        out(f"WARN clock skew exceeds {SKEW_WARNING_SECONDS:.0f} s: check NTP "
                            "on the server and on this machine (timedatectl)")

                    result = await session.call_tool("get_current_time", {"timezone": "Mars/X"})
                    if not result.isError:
                        raise SmokeFailure("get_current_time(Mars/X) did not return an error")
                    try:
                        error = _payload(result)["error"]
                    except (ValueError, KeyError, IndexError) as exc:
                        raise SmokeFailure(f"error result is not the JSON error object: "
                                           f"{result.content}") from exc
                    out(f"OK   get_current_time(Mars/X): isError with error {error[:60]!r}...")


def _leaf_exceptions(exc: BaseException) -> list[BaseException]:
    """The underlying exceptions inside (possibly nested) exception groups.

    The MCP client runs in task groups, so a failed check or a connection
    error usually arrives wrapped in an ExceptionGroup; report what's inside.
    """
    if isinstance(exc, BaseExceptionGroup):
        return [leaf for inner in exc.exceptions for leaf in _leaf_exceptions(inner)]
    return [exc]


def _describe(exc: BaseException) -> str:
    if isinstance(exc, SmokeFailure):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Smoke-test a timekeeper-mcp HTTP endpoint.")
    parser.add_argument("url", help="the MCP endpoint, e.g. https://time.example.org/mcp")
    parser.add_argument("--timeout", type=float, default=30,
                        help="deadline for the whole run in seconds (default 30)")
    args = parser.parse_args(argv)
    try:
        check_url(args.url)
    except ValueError as exc:
        print(f"usage error: {exc}", file=sys.stderr)
        return 2
    try:
        anyio.run(run_smoke, args.url, args.timeout)
    except TimeoutError:
        print(f"FAIL no complete answer within {args.timeout:.0f} s", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - failed check, unreachable host, TLS or protocol error
        for leaf in _leaf_exceptions(exc):
            if isinstance(leaf, TimeoutError):
                print(f"FAIL no complete answer within {args.timeout:.0f} s", file=sys.stderr)
            else:
                print(f"FAIL {_describe(leaf)}", file=sys.stderr)
        return 1
    print("PASS all smoke checks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
