"""Behavioral checks for deploy/timekeeper-mcp.service.example.

Driven by scripts/verify_systemd_sandbox.sh on a disposable Linux VM; not
meant to be run by hand. Three subcommands:

  probe   Runs *inside* the service sandbox (as the probe unit's ExecStart)
          and checks that each restriction actually holds. Standard library
          only. Prints one PASS/FAIL line per check; exits 1 on any failure.
  listen  Runs on the host: a short-lived TCP listener that the probe tries
          to reach. Writes its port to a file and accepts until its lifetime
          ends.
  client  Runs on the host against the real service: waits (bounded) for
          readiness, then drives an initialized MCP session over loopback
          with the official SDK, sending a marker string in tool arguments.

The probe connects only to the targets it is given (a loopback listener and a
listener on this host's own interface address), to TEST-NET-1 (192.0.2.1) and,
when the host has an IPv6 route for it, to 2001:db8::1. Those two are
documentation-only destinations, assigned to no real host; the script decides
on IPv6 with a read-only route lookup. Connecting to them can only show a
failure of the filter (a connection), never prove it works, because an
unsandboxed attempt can fail the same way; if the filter were missing, a
packet could leave through a local default route towards a non-existent
host. The proof that the filter blocks traffic is the host-interface
listener: reachable outside the sandbox (a positive control the script
runs), and never reached from inside it, as its accept count shows. It never
contacts metadata or private-network services, and it never connects a
Unix-domain socket.
"""
from __future__ import annotations

import argparse
import errno
import json
import os
import pwd
import socket
import stat
import subprocess
import sys
import time

TEST_NET_1 = ("192.0.2.1", 80)
COMMAND_TIMEOUT_SECONDS = 10
# IPAddressDeny is enforced by cgroup BPF programs that drop packets. A
# dropped TCP SYN is not reported to connect(), which keeps retrying, so a
# blocked connect usually ends in a timeout rather than an immediate error.
# A timeout alone therefore proves nothing; see host-interface-connect-failed.
CONNECT_TIMEOUT_SECONDS = 3


# --- probe -------------------------------------------------------------------

class Checks:
    def __init__(self) -> None:
        self.failed = 0
        self.passed = 0
        self.skipped = 0

    def skip(self, name: str, reason: str) -> None:
        self.skipped += 1
        print(f"SKIP {name}: {reason}", flush=True)

    def info(self, name: str, detail: str) -> None:
        """Report an observation that is neither a pass nor a failure."""
        print(f"INFO {name}: {detail}", flush=True)

    def record(self, name: str, ok: bool, detail: str) -> None:
        if ok:
            self.passed += 1
        else:
            self.failed += 1
        print(f"{'PASS' if ok else 'FAIL'} {name}: {detail}", flush=True)

    def run(self, name: str, check) -> None:
        """Run `check() -> (ok, detail)`; an unexpected exception is a failure."""
        try:
            ok, detail = check()
        except Exception as exc:  # noqa: BLE001 - report, don't crash the probe
            ok, detail = False, f"unexpected {type(exc).__name__}: {exc}"
        self.record(name, ok, detail)


def _status_fields() -> dict[str, str]:
    fields = {}
    with open("/proc/self/status") as status:
        for line in status:
            key, _, value = line.partition(":")
            fields[key] = value.strip()
    return fields


def _write_denied(directory: str) -> tuple[bool, str]:
    # The directories are owned by the service user, so ordinary permissions
    # allow the write; only the read-only mount (EROFS) can stop it.
    path = os.path.join(directory, f".timekeeper-probe-{os.getpid()}")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as exc:
        name = errno.errorcode.get(exc.errno, exc.errno)
        return exc.errno == errno.EROFS, f"open for write failed with {name} (expected EROFS)"
    os.close(fd)
    os.unlink(path)
    return False, f"created {path} (write was allowed)"


def _unreadable(path: str) -> tuple[bool, str]:
    try:
        with open(path, "rb") as handle:
            handle.read(1)
    except OSError as exc:
        return True, f"read failed with {errno.errorcode.get(exc.errno, exc.errno)}"
    return False, "file was readable"  # never print the contents


def _try_connect(address: tuple[str, int]) -> tuple[bool, str]:
    """Attempt a TCP connection; return (connected, description)."""
    start = time.monotonic()
    try:
        socket.create_connection(address, timeout=CONNECT_TIMEOUT_SECONDS).close()
    except OSError as exc:
        elapsed = time.monotonic() - start
        name = errno.errorcode.get(exc.errno, str(exc.errno)) if exc.errno else type(exc).__name__
        return False, f"no connection: {name} after {elapsed:.2f}s"
    return True, f"connected after {time.monotonic() - start:.2f}s"


def _documentation_address(checks: Checks, name: str, address: tuple[str, int]) -> None:
    """Connect to a reserved documentation address that no host answers.

    Success would mean the filter let traffic out (and something answered), so
    it fails. Anything else is inconclusive: an unfiltered connect to such an
    address can also time out or be refused, so it is reported, not counted.
    """
    connected, detail = _try_connect(address)
    if connected:
        checks.record(name, False, f"{detail} to {address[0]} (must not be reachable)")
    else:
        checks.info(name, f"{detail} to {address[0]}; inconclusive on its own (an "
                          "unsandboxed attempt could fail the same way)")


def _block_devices() -> list[str]:
    found = []
    for root, dirs, files in os.walk("/dev", onerror=lambda _: None):
        for name in dirs + files:
            path = os.path.join(root, name)
            try:
                if stat.S_ISBLK(os.lstat(path).st_mode):
                    found.append(path)
            except OSError:
                pass
    return found


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=COMMAND_TIMEOUT_SECONDS)


def probe(args: argparse.Namespace) -> int:
    checks = Checks()
    expected_uid = pwd.getpwnam(args.user).pw_uid

    checks.run("uid", lambda: (
        os.getuid() == expected_uid and os.geteuid() == expected_uid,
        f"uid={os.getuid()} euid={os.geteuid()}, expected {expected_uid} ({args.user})"))

    def capabilities():
        fields = _status_fields()
        eff, bnd = int(fields["CapEff"], 16), int(fields["CapBnd"], 16)
        return eff == 0 and bnd == 0, f"CapEff={fields['CapEff']} CapBnd={fields['CapBnd']}"
    checks.run("no-capabilities", capabilities)

    def no_new_privs():
        value = _status_fields().get("NoNewPrivs")
        return value == "1", f"NoNewPrivs={value}"
    checks.run("no-new-privileges", no_new_privs)

    def setuid_helper():
        # A root-owned setuid copy of `id`; outside the sandbox it reports 0.
        result = _run([args.setuid_helper, "-u"])
        euid = result.stdout.strip()
        return euid == str(expected_uid), f"setuid helper ran with euid={euid!r}"
    checks.run("setuid-helper-not-elevated", setuid_helper)

    for directory in args.deny_write:
        checks.run(f"write-denied:{directory}", lambda d=directory: _write_denied(d))

    def tmp_private():
        marker_visible = os.path.exists(os.path.join("/tmp", args.host_marker))
        path = os.path.join("/tmp", f"timekeeper-probe-{os.getpid()}")
        with open(path, "w") as handle:
            handle.write("ok")
        os.unlink(path)
        return not marker_visible, f"/tmp writable; host marker visible={marker_visible}"
    checks.run("private-tmp", tmp_private)

    for secret in args.secret:
        checks.run(f"unreadable:{secret}", lambda p=secret: _unreadable(p))

    def no_block_devices():
        found = _block_devices()
        return not found, f"block devices: {found[:5]}"
    checks.run("no-block-devices", no_block_devices)

    def loopback_allowed():
        socket.create_connection(("127.0.0.1", args.loopback_port),
                                 timeout=CONNECT_TIMEOUT_SECONDS).close()
        return True, f"connected to 127.0.0.1:{args.loopback_port} (loopback stays reachable)"
    checks.run("loopback-allowed", loopback_allowed)

    def unix_socket_creation_denied():
        # RestrictAddressFamilies filters socket(2) only; nothing is connected.
        try:
            socket.socket(socket.AF_UNIX, socket.SOCK_STREAM).close()
        except OSError as exc:
            name = errno.errorcode.get(exc.errno, exc.errno)
            return exc.errno == errno.EAFNOSUPPORT, f"socket(AF_UNIX) failed with {name}"
        return False, "socket(AF_UNIX) succeeded"
    checks.run("unix-socket-creation-denied", unix_socket_creation_denied)

    def socketpair_allowed():
        # asyncio's event loop needs socketpair(2), which the filter leaves alone.
        left, right = socket.socketpair()
        with left, right:
            left.sendall(b"x")
            return right.recv(1) == b"x", "socketpair() created and carried a byte"
    checks.run("socketpair-allowed", socketpair_allowed)

    def host_interface_connect_failed():
        # The verification script made the same connection as the same user
        # outside the sandbox (it succeeded and the listener counted it), and
        # after this probe it checks the listener counted nothing more. That
        # listener-side count is the evidence; this only records the attempt.
        connected, detail = _try_connect((args.host_ip, args.host_port))
        return not connected, (f"{detail} to {args.host_ip}:{args.host_port}; the "
                               "listener's accept count is checked by the script")
    checks.run("host-interface-connect-failed", host_interface_connect_failed)
    _documentation_address(checks, "test-net-1-not-reachable", TEST_NET_1)
    if args.ipv6_route == "yes":
        _documentation_address(checks, "ipv6-documentation-prefix-not-reachable",
                               (args.ipv6_target, 80))
    else:
        checks.skip("ipv6-documentation-prefix-not-reachable",
                    "the host has no IPv6 stack or no route to the documentation prefix")

    print(f"PROBE RESULT: {checks.passed} passed, {checks.failed} failed, "
          f"{checks.skipped} skipped", flush=True)
    return 1 if checks.failed else 0


# --- listen ------------------------------------------------------------------

def _write_atomically(path: str, text: str) -> None:
    temp = f"{path}.tmp"
    with open(temp, "w") as handle:
        handle.write(text)
    os.replace(temp, path)


def listen(args: argparse.Namespace) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind((args.bind, 0))
        server.listen(16)
        server.settimeout(1)
        accepted = 0
        if args.count_file:
            _write_atomically(args.count_file, "0")
        _write_atomically(args.port_file, str(server.getsockname()[1]))
        deadline = time.monotonic() + args.lifetime
        while time.monotonic() < deadline:
            try:
                connection, _ = server.accept()
            except socket.timeout:
                continue
            connection.close()
            accepted += 1
            if args.count_file:
                _write_atomically(args.count_file, str(accepted))
    return 0


# --- client ------------------------------------------------------------------

def _wait_ready(url: str, deadline_seconds: float) -> None:
    import urllib.error
    import urllib.request

    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "sandbox-readiness", "version": "0"}},
    }).encode()
    deadline = time.monotonic() + deadline_seconds
    last = "no attempt"
    while time.monotonic() < deadline:
        request = urllib.request.Request(url, data=body, method="POST", headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream"})
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                if response.status == 200:
                    return
                last = f"status {response.status}"
        except (urllib.error.URLError, OSError) as exc:
            last = str(exc)
        time.sleep(0.25)
    raise SystemExit(f"service not ready within {deadline_seconds}s: {last}")


async def _session(url: str, marker: str) -> None:
    import anyio
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    calls = [
        ("get_current_time", {"timezone": "UTC"}, False),
        ("get_current_time", {"timezone": marker}, True),
        ("convert_time", {"iso_timestamp": marker, "target_timezone": "UTC"}, True),
        ("time_until", {"iso_timestamp": marker}, True),
    ]
    with anyio.fail_after(60):
        async with httpx.AsyncClient(timeout=httpx.Timeout(10, read=30)) as http_client:
            async with streamable_http_client(url, http_client=http_client) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = {tool.name for tool in (await session.list_tools()).tools}
                    expected = {"get_current_time", "convert_time", "time_until"}
                    if tools != expected:
                        raise SystemExit(f"unexpected tools: {sorted(tools)}")
                    for name, arguments, expect_error in calls:
                        result = await session.call_tool(name, arguments)
                        payload = json.loads(result.content[0].text)
                        if result.isError is not expect_error:
                            raise SystemExit(f"{name}: isError={result.isError}, payload={payload}")
                        print(f"OK {name} isError={result.isError}", flush=True)


def client(args: argparse.Namespace) -> int:
    import anyio

    _wait_ready(args.url, args.ready_deadline)
    anyio.run(_session, args.url, args.marker)
    print("CLIENT RESULT: initialized session and all tool calls succeeded", flush=True)
    return 0


# --- command line ------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)

    p = commands.add_parser("probe")
    p.add_argument("--user", required=True)
    p.add_argument("--setuid-helper", required=True)
    p.add_argument("--deny-write", action="append", required=True)
    p.add_argument("--host-marker", required=True)
    p.add_argument("--secret", action="append", required=True)
    p.add_argument("--loopback-port", type=int, required=True)
    p.add_argument("--host-ip", required=True)
    p.add_argument("--host-port", type=int, required=True)
    p.add_argument("--ipv6-target", required=True)
    p.add_argument("--ipv6-route", choices=("yes", "no"), required=True)

    p = commands.add_parser("listen")
    p.add_argument("--bind", required=True)
    p.add_argument("--port-file", required=True)
    p.add_argument("--lifetime", type=float, default=600)
    p.add_argument("--count-file", help="kept up to date with the number of accepted connections")

    p = commands.add_parser("client")
    p.add_argument("--url", required=True)
    p.add_argument("--marker", required=True)
    p.add_argument("--ready-deadline", type=float, default=30)

    args = parser.parse_args(argv)
    return {"probe": probe, "listen": listen, "client": client}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
