"""End-to-end tests over the stdio transport.

stdout is reserved for MCP messages: every line must be a JSON-RPC message,
and diagnostics (logs, tracebacks) must go to stderr.
"""
from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import time

import anyio
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.version import SUPPORTED_PROTOCOL_VERSIONS
from mcp.types import LATEST_PROTOCOL_VERSION

from tests.harness import PROJECT_ROOT, STOP_WAIT_SECONDS, TOOL_CALLS, child_env, stop

SYNTHETIC_MARKER = "SYNTHETIC_SECRET_/fake/internal/path"


def _server_params(python_args: list[str] | None = None) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=python_args or ["-m", "timekeeper"],
        cwd=PROJECT_ROOT,
        env=child_env(),  # no MCP_TRANSPORT: stdio is the default
    )


def test_all_tools_over_initialized_sdk_session(tmp_path):
    async def flow():
        with open(tmp_path / "stderr.log", "w") as errlog, anyio.fail_after(30):
            async with stdio_client(_server_params(), errlog=errlog) as (read, write):
                async with ClientSession(read, write) as client:
                    init = await client.initialize()
                    tools = {tool.name for tool in (await client.list_tools()).tools}
                    results = [await client.call_tool(name, arguments)
                               for name, arguments, _, _ in TOOL_CALLS]
        return init, tools, results

    init, tools, results = anyio.run(flow)

    assert init.protocolVersion in SUPPORTED_PROTOCOL_VERSIONS
    assert tools == {"get_current_time", "convert_time", "time_until"}
    for (name, arguments, expect_error, fields), result in zip(TOOL_CALLS, results):
        assert result.isError is expect_error, (name, arguments)
        payload = json.loads(result.content[0].text)
        assert set(payload) == fields, (name, arguments)
        assert payload == result.structuredContent


def _raw_session(python_args: list[str] | None = None) -> tuple[list[dict], str]:
    """Run an initialized session's worth of raw messages over stdio.

    Returns (parsed stdout messages, stderr text) and asserts that every
    stdout line is a JSON-RPC message. stdin stays open until every response
    arrives, because the server stops (cancelling in-flight requests) on
    stdin EOF. Background threads drain stdout and stderr so neither pipe
    can fill and block the child; the deadline bounds every wait, and a child
    that closes stdout early fails the test at once, with its stderr.
    """
    messages = [
        {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
            "protocolVersion": LATEST_PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": {"name": "raw-stdio-test", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    ] + [
        {"jsonrpc": "2.0", "id": 2 + i, "method": "tools/call",
         "params": {"name": name, "arguments": arguments}}
        for i, (name, arguments, _, _) in enumerate(TOOL_CALLS)
    ]
    expected_ids = {m["id"] for m in messages if "id" in m}
    proc = subprocess.Popen(
        [sys.executable, *(python_args or ["-m", "timekeeper"])],
        cwd=PROJECT_ROOT, env=child_env(), text=True,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    stdout_lines: queue.Queue[str | None] = queue.Queue()
    stderr_parts: list[str] = []

    def read_stdout():
        for line in proc.stdout:
            stdout_lines.put(line)
        stdout_lines.put(None)  # end of stream: the child closed stdout

    readers = [
        threading.Thread(target=read_stdout, daemon=True),
        threading.Thread(target=lambda: stderr_parts.append(proc.stderr.read()), daemon=True),
    ]
    for reader in readers:
        reader.start()
    raw_lines = []
    answered: set = set()
    failure = None
    try:
        proc.stdin.write("".join(json.dumps(m) + "\n" for m in messages))
        proc.stdin.flush()
        deadline = time.monotonic() + 30
        while answered != expected_ids:
            try:
                line = stdout_lines.get(timeout=max(deadline - time.monotonic(), 0))
            except queue.Empty:
                failure = "timed out"
                break
            if line is None:
                failure = "server closed stdout"
                break
            raw_lines.append(line)
            answered |= {json.loads(line).get("id")} & expected_ids
        if failure is None:
            proc.stdin.close()
            proc.wait(timeout=STOP_WAIT_SECONDS)
    except BrokenPipeError:
        failure = "server closed stdin"
    finally:
        stop(proc)
        for reader in readers:
            reader.join(timeout=STOP_WAIT_SECONDS)
    stderr = "".join(stderr_parts)
    if failure is not None:
        raise AssertionError(
            f"{failure} before answering ids {sorted(expected_ids - answered)} "
            f"(exit code {proc.returncode}); stderr:\n{stderr[-2000:]}"
        )
    while not stdout_lines.empty():
        line = stdout_lines.get()
        if line is not None:
            raw_lines.append(line)
    assert proc.returncode == 0, stderr[-2000:]

    parsed = []
    for line in raw_lines:
        message = json.loads(line)  # fails on any non-JSON output
        assert message.get("jsonrpc") == "2.0", line
        parsed.append(message)
    return parsed, stderr


def test_stdout_carries_only_json_rpc_responses():
    messages, _ = _raw_session()

    responses = {m["id"]: m for m in messages if "id" in m}
    assert set(responses) == set(range(2 + len(TOOL_CALLS)))
    assert responses[0]["result"]["protocolVersion"] in SUPPORTED_PROTOCOL_VERSIONS
    for i, (name, _, expect_error, _) in enumerate(TOOL_CALLS):
        result = responses[2 + i]["result"]
        assert result["isError"] is expect_error, name
        assert json.loads(result["content"][0]["text"]) == result["structuredContent"]


def test_internal_errors_log_to_stderr_not_stdout():
    launcher = (
        "import timekeeper.core as core\n"
        "def boom(*args, **kwargs):\n"
        f"    raise RuntimeError({SYNTHETIC_MARKER!r})\n"
        "core.time_until = boom\n"
        "from timekeeper.__main__ import main\n"
        "main()\n"
    )
    messages, stderr = _raw_session(["-c", launcher])

    responses = {m["id"]: m for m in messages if "id" in m}
    failed = [responses[2 + i]["result"] for i, (name, _, _, _)
              in enumerate(TOOL_CALLS) if name == "time_until"]
    assert len(failed) == 2
    for result in failed:
        assert result["isError"] is True
        assert json.loads(result["content"][0]["text"])["error"] == "Internal error"
    assert all(SYNTHETIC_MARKER not in json.dumps(m) for m in messages)
    assert SYNTHETIC_MARKER in stderr
    assert "Traceback" in stderr


def test_raw_session_fails_fast_with_stderr_when_server_exits_early():
    launcher = "import sys; sys.stderr.write('SYNTHETIC_CHILD_DIAGNOSTIC\\n'); sys.exit(3)"
    started = time.monotonic()
    with pytest.raises(AssertionError) as excinfo:
        _raw_session(["-c", launcher])
    assert time.monotonic() - started < 10  # not the 30 s response deadline
    assert "exit code 3" in str(excinfo.value)
    assert "SYNTHETIC_CHILD_DIAGNOSTIC" in str(excinfo.value)
