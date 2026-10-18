"""Tests for scripts/smoke.py, the post-deploy smoke test.

The script is run as a subprocess, exactly as an operator runs it, and only
ever against a temporary server on an ephemeral loopback port (or a loopback
port with nothing listening). It never contacts any other host.
"""
from __future__ import annotations

import subprocess
import sys

from tests.harness import PROJECT_ROOT, free_port

SMOKE = PROJECT_ROOT / "scripts" / "smoke.py"


def run_smoke(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SMOKE), *args], capture_output=True,
                          text=True, timeout=90)


# Runs the real smoke CLI, but expects a tool the server doesn't have, so the
# tools/list check fails against a perfectly healthy server.
FORCE_TOOL_LIST_FAILURE = """
import importlib.util, sys
spec = importlib.util.spec_from_file_location("smoke", sys.argv[1])
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)
smoke.EXPECTED_TOOLS = smoke.EXPECTED_TOOLS | {"no_such_tool"}
sys.exit(smoke.main(sys.argv[2:]))
"""


def test_smoke_passes_against_a_local_server(http_server):
    result = run_smoke(f"{http_server.url}/mcp", "--timeout", "30")
    assert result.returncode == 0, result.stdout + result.stderr
    out = result.stdout
    assert "OK   initialize:" in out
    assert "OK   tools/list: convert_time, get_current_time, time_until" in out
    assert "OK   get_current_time(UTC):" in out
    assert "INFO clock skew (server - this machine):" in out
    assert "OK   get_current_time(Mars/X): isError" in out
    assert out.rstrip().endswith("PASS all smoke checks")


def test_failed_check_reports_its_own_reason(http_server):
    result = subprocess.run(
        [sys.executable, "-c", FORCE_TOOL_LIST_FAILURE, str(SMOKE),
         f"{http_server.url}/mcp", "--timeout", "30"],
        capture_output=True, text=True, timeout=90)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "FAIL tools/list returned ['convert_time', 'get_current_time', 'time_until']" \
        in result.stderr, result.stderr
    assert "no_such_tool" in result.stderr
    assert "ExceptionGroup" not in result.stderr
    assert "PASS" not in result.stdout


def test_smoke_fails_when_nothing_is_listening():
    result = run_smoke(f"http://127.0.0.1:{free_port()}/mcp", "--timeout", "10")
    assert result.returncode == 1, result.stdout + result.stderr
    assert result.stderr.startswith("FAIL ")
    # The underlying connection error is reported, not a wrapping group.
    assert "ExceptionGroup" not in result.stderr, result.stderr
    assert "Connect" in result.stderr, result.stderr
    assert "PASS" not in result.stdout


def test_smoke_refuses_plain_http_to_a_non_loopback_host():
    # Rejected before any connection is attempted (.invalid never resolves).
    result = run_smoke("http://time.example.invalid/mcp")
    assert result.returncode == 2
    assert "only accepted for loopback" in result.stderr
