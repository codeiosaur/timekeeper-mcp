"""Descriptive latency measurements for timekeeper-mcp on this machine.

Usage (from a checkout, with the package and its dev extras installed):
    python scripts/bench.py [--core-calls 2000] [--mcp-calls 200]

Reports the environment and the median, 95th percentile and sample count for:
  1. the core time functions, called directly;
  2. MCP tool calls over an in-memory client session (no transport);
  3. MCP tool calls over HTTP to a server this script starts on an ephemeral
     loopback port and stops again when done.

These are measurements, not pass/fail thresholds: they vary with hardware,
load and Python version, and are only comparable with runs on the same
machine. Nothing here contacts anything other than 127.0.0.1. Not run in CI.
"""
from __future__ import annotations

import argparse
import importlib.metadata
import logging
import os
import platform
import statistics
import sys
import tempfile
import time
from pathlib import Path

import anyio
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.memory import create_connected_server_and_client_session

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))  # for timekeeper and tests.harness from a checkout

from tests.harness import running_http_server  # noqa: E402
from timekeeper import core  # noqa: E402
from timekeeper.server import mcp as server  # noqa: E402

CALL = ("get_current_time", {"timezone": "America/New_York"})
SESSION_DEADLINE_SECONDS = 300


def summarize(name: str, samples_ns: list[int]) -> str:
    micros = [s / 1000 for s in samples_ns]
    # quantiles() needs two samples; a single sample is its own 95th percentile.
    p95 = statistics.quantiles(micros, n=100)[94] if len(micros) > 1 else micros[0]
    return (f"{name:<44} n={len(micros):<6} median={statistics.median(micros):>10.1f} us"
            f"   p95={p95:>10.1f} us")


def time_calls(function, count: int) -> list[int]:
    samples = []
    for _ in range(count):
        start = time.perf_counter_ns()
        function()
        samples.append(time.perf_counter_ns() - start)
    return samples


async def time_session_calls(session: ClientSession, count: int) -> list[int]:
    samples = []
    for _ in range(count):
        start = time.perf_counter_ns()
        result = await session.call_tool(*CALL)
        samples.append(time.perf_counter_ns() - start)
        if result.isError:
            raise RuntimeError(f"tool call failed: {result.content}")
    return samples


async def in_memory(count: int) -> list[int]:
    with anyio.fail_after(SESSION_DEADLINE_SECONDS):
        async with create_connected_server_and_client_session(server) as session:
            await session.call_tool(*CALL)  # warm-up, not measured
            return await time_session_calls(session, count)


async def over_http(url: str, count: int) -> list[int]:
    with anyio.fail_after(SESSION_DEADLINE_SECONDS):
        async with httpx.AsyncClient(timeout=httpx.Timeout(10, read=30)) as http_client:
            async with streamable_http_client(url, http_client=http_client) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    await session.call_tool(*CALL)  # warm-up, not measured
                    return await time_session_calls(session, count)


def positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, got {value}")
    return value


def environment() -> list[str]:
    return [
        f"os:       {platform.platform()}",
        f"machine:  {platform.machine()} ({platform.processor() or 'processor n/a'}), "
        f"{os.cpu_count()} logical CPUs",
        f"python:   {platform.python_implementation()} {platform.python_version()}",
        f"mcp:      {importlib.metadata.version('mcp')}",
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Descriptive timekeeper-mcp latencies.")
    parser.add_argument("--core-calls", type=positive_int, default=2000)
    parser.add_argument("--mcp-calls", type=positive_int, default=200)
    args = parser.parse_args(argv)
    # The in-process server and HTTP client log every request at INFO; keep
    # the report readable.
    for name in ("mcp", "httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)

    print("\n".join(environment()))
    print()
    core.get_time_in_zone("America/New_York")  # warm the timezone cache, not measured
    print(summarize("core.get_time_in_zone (direct call)",
                    time_calls(lambda: core.get_time_in_zone("America/New_York"),
                               args.core_calls)))
    print(summarize("core.convert_to_zone (direct call)",
                    time_calls(lambda: core.convert_to_zone("2026-11-01T06:30:00Z",
                                                            "America/New_York"),
                               args.core_calls)))
    print(summarize("core.time_until (direct call)",
                    time_calls(lambda: core.time_until("2030-01-01T00:00:00Z"),
                               args.core_calls)))
    print(summarize("get_current_time via in-memory MCP session",
                    anyio.run(in_memory, args.mcp_calls)))
    with tempfile.TemporaryDirectory(prefix="timekeeper-bench-") as log_dir:
        with running_http_server(Path(log_dir)) as http:
            samples = anyio.run(over_http, f"{http.url}/mcp", args.mcp_calls)
    print(summarize("get_current_time via HTTP on loopback", samples))
    print()
    print("Descriptive only: measurements from this machine at this moment, not thresholds.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
