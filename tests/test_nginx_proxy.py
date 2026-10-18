"""Tests for deploy/nginx.conf.example in front of the real app.

The real example file is rendered into a temporary directory (domain, listen
ports, certificate paths, certbot's options include and dhparam file, and
upstream substituted) and run by a local, unprivileged nginx with a throwaway
CA. The wrapper nginx.conf carries the SSL directives that Ubuntu's stock
nginx.conf sets at http level, so a config that would fail `nginx -t` there
fails here too. Everything stays on 127.0.0.1 with ephemeral ports; nothing
leaves the host.

Skipped when `nginx` or `openssl` is not on PATH, on Windows, or as root.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
from contextlib import ExitStack
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import anyio
import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from sse_starlette.sse import EventSourceResponse

from tests.harness import (
    PROJECT_ROOT, READY_DEADLINE_SECONDS, TOOL_CALLS, free_port,
    initialize_body, running_http_server, stop,
)

NGINX = shutil.which("nginx")
OPENSSL = shutil.which("openssl")
EXAMPLE = PROJECT_ROOT / "deploy" / "nginx.conf.example"
DOMAIN = "time.example.test"  # .test is reserved; never resolved

pytestmark = [
    pytest.mark.skipif(NGINX is None, reason="nginx is not on PATH"),
    pytest.mark.skipif(OPENSSL is None, reason="openssl is not on PATH"),
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only nginx test setup"),
    pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                       reason="nginx test must run as a non-root user"),
]


# --- throwaway TLS material --------------------------------------------------

# What certbot installs as /etc/letsencrypt/options-ssl-nginx.conf.
CERTBOT_OPTIONS = """\
ssl_session_cache shared:le_nginx_SSL:10m;
ssl_session_timeout 1440m;
ssl_session_tickets off;

ssl_protocols TLSv1.2 TLSv1.3;
ssl_prefer_server_ciphers off;

ssl_ciphers "ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305:DHE-RSA-AES128-GCM-SHA256:DHE-RSA-AES256-GCM-SHA384";
"""

# SSL directives that Ubuntu's stock /etc/nginx/nginx.conf sets at http level.
UBUNTU_HTTP_SSL = """\
    ssl_protocols TLSv1 TLSv1.1 TLSv1.2 TLSv1.3;
    ssl_prefer_server_ciphers on;
"""


@dataclass
class Tls:
    ca: Path
    cert: Path
    key: Path
    certbot_options: Path
    dhparam: Path

    def context(self) -> ssl.SSLContext:
        """A client context that trusts only the throwaway CA."""
        return ssl.create_default_context(cafile=str(self.ca))


def _openssl(*args: str, cwd: Path) -> None:
    subprocess.run([OPENSSL, *args], cwd=cwd, check=True, capture_output=True, timeout=60)


@pytest.fixture(scope="module")
def tls(tmp_path_factory) -> Tls:
    d = tmp_path_factory.mktemp("tls")
    (d / "ca.cnf").write_text(
        "[req]\ndistinguished_name=dn\n[dn]\n"
        "[v3_ca]\nbasicConstraints=critical,CA:TRUE\nkeyUsage=critical,keyCertSign,cRLSign\n"
        "subjectKeyIdentifier=hash\n"
    )
    (d / "leaf.ext").write_text(
        "basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=serverAuth\nsubjectKeyIdentifier=hash\n"
        "authorityKeyIdentifier=keyid,issuer\n"
        f"subjectAltName=DNS:{DOMAIN},IP:127.0.0.1\n"
    )
    _openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
             "-keyout", "ca.key", "-out", "ca.pem", "-subj", "/CN=timekeeper test CA",
             "-config", "ca.cnf", "-extensions", "v3_ca", cwd=d)
    _openssl("req", "-newkey", "rsa:2048", "-nodes", "-keyout", "leaf.key",
             "-out", "leaf.csr", "-subj", f"/CN={DOMAIN}", "-config", "ca.cnf", cwd=d)
    _openssl("x509", "-req", "-in", "leaf.csr", "-CA", "ca.pem", "-CAkey", "ca.key",
             "-CAcreateserial", "-days", "1", "-out", "leaf.pem", "-extfile", "leaf.ext", cwd=d)
    # certbot's ssl-dhparams.pem is the RFC 7919 ffdhe2048 group.
    try:
        _openssl("genpkey", "-genparam", "-algorithm", "DH", "-pkeyopt", "group:ffdhe2048",
                 "-out", "ssl-dhparams.pem", cwd=d)
    except subprocess.CalledProcessError:
        pytest.skip("this openssl cannot emit ffdhe2048 DH parameters (OpenSSL 3 can)")
    (d / "options-ssl-nginx.conf").write_text(CERTBOT_OPTIONS)
    return Tls(ca=d / "ca.pem", cert=d / "leaf.pem", key=d / "leaf.key",
               certbot_options=d / "options-ssl-nginx.conf", dhparam=d / "ssl-dhparams.pem")


# --- rendering the example ---------------------------------------------------

def _replace(text: str, old: str, new: str, count: int = 1) -> str:
    assert text.count(old) == count, f"expected {count} x {old!r} in {EXAMPLE.name}"
    return text.replace(old, new)


def render_site(http_port: int, https_port: int, upstream_port: int, tls: Tls) -> str:
    """Render the example with test values. Every substitution must match the
    stated number of times (once, or once per SSL server block), so the
    example and this test cannot drift apart silently."""
    text = EXAMPLE.read_text()
    # Certbot's lines belong in each SSL server block (catch-all and main).
    for old, new in [
        ("include     /etc/letsencrypt/options-ssl-nginx.conf;",
         f"include     {tls.certbot_options};"),
        ("ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;", f"ssl_dhparam {tls.dhparam};"),
    ]:
        text = _replace(text, old, new, count=2)
    for old, new in [
        ("listen 80 default_server;", f"listen 127.0.0.1:{http_port} default_server;"),
        ("listen 443 ssl default_server;", f"listen 127.0.0.1:{https_port} ssl default_server;"),
        ("listen 80;\n    server_name YOUR_DOMAIN;",
         f"listen 127.0.0.1:{http_port};\n    server_name {DOMAIN};"),
        ("listen 443 ssl;\n    server_name YOUR_DOMAIN;",
         f"listen 127.0.0.1:{https_port} ssl;\n    server_name {DOMAIN};"),
        ("ssl_certificate     /etc/letsencrypt/live/YOUR_DOMAIN/fullchain.pem;",
         f"ssl_certificate     {tls.cert};"),
        ("ssl_certificate_key /etc/letsencrypt/live/YOUR_DOMAIN/privkey.pem;",
         f"ssl_certificate_key {tls.key};"),
        ("proxy_pass http://127.0.0.1:8765/mcp;",
         f"proxy_pass http://127.0.0.1:{upstream_port}/mcp;"),
    ]:
        text = _replace(text, old, new)
    directives = [line for line in text.splitlines()
                  if line.strip() and not line.strip().startswith("#")]
    for leftover in ("YOUR_DOMAIN", "letsencrypt", ":8765"):
        assert not any(leftover in line for line in directives), leftover
    return text


@dataclass
class Proxy:
    https_port: int
    http_port: int
    prefix: Path

    @property
    def url(self) -> str:
        return f"https://127.0.0.1:{self.https_port}"


def write_nginx_conf(prefix: Path, site: str) -> list[str]:
    """Write site.conf plus an Ubuntu-like wrapper nginx.conf under `prefix`;
    return the nginx command prefix for that configuration."""
    (prefix / "site.conf").write_text(site)
    temp_paths = "\n".join(
        f"    {name}_temp_path {prefix / name};"
        for name in ("client_body", "proxy", "fastcgi", "uwsgi", "scgi")
    )
    (prefix / "nginx.conf").write_text(
        f"worker_processes 1;\npid {prefix / 'nginx.pid'};\n"
        f"error_log {prefix / 'error.log'} info;\n"
        "events { worker_connections 128; }\n"
        f"http {{\n    access_log {prefix / 'access.log'};\n{temp_paths}\n"
        f"{UBUNTU_HTTP_SSL}"
        f"    include {prefix / 'site.conf'};\n}}\n"
    )
    return [NGINX, "-p", str(prefix), "-c", str(prefix / "nginx.conf")]


def test_certbot_options_at_http_scope_fail_ubuntu_style_config(tmp_path, tls):
    # Guards the wrapper itself: moving certbot's include to the top of the
    # site file (http scope) must fail `nginx -t`, as it does on Ubuntu.
    include = f"include     {tls.certbot_options};"
    site = render_site(free_port(), free_port(), free_port(), tls)
    site = site.replace(f"    {include}\n", "", 1)
    assert site.count(include) == 1
    args = write_nginx_conf(tmp_path, f"{include}\n{site}")
    check = subprocess.run([*args, "-t"], capture_output=True, text=True, timeout=30)
    assert check.returncode != 0
    assert '"ssl_prefer_server_ciphers" directive is duplicate' in check.stderr


@pytest.fixture
def start_nginx(tmp_path, tls):
    """Factory: run nginx with the rendered example in front of `upstream_port`."""
    with ExitStack() as stack:
        def start(upstream_port: int) -> Proxy:
            prefix = tmp_path / f"nginx-{upstream_port}"
            prefix.mkdir()
            https_port = free_port()
            http_port = free_port()
            while http_port == https_port:
                http_port = free_port()
            args = write_nginx_conf(
                prefix, render_site(http_port, https_port, upstream_port, tls))
            check = subprocess.run([*args, "-t"], capture_output=True, text=True, timeout=30)
            assert check.returncode == 0, check.stderr

            with open(prefix / "stderr.log", "w") as log:
                proc = subprocess.Popen([*args, "-g", "daemon off;"], stdin=subprocess.DEVNULL,
                                        stdout=log, stderr=subprocess.STDOUT)
            stack.callback(stop, proc)
            deadline = time.monotonic() + READY_DEADLINE_SECONDS
            while True:
                assert proc.poll() is None, (prefix / "stderr.log").read_text()
                try:
                    socket.create_connection(("127.0.0.1", https_port), timeout=0.5).close()
                    break
                except OSError:
                    assert time.monotonic() < deadline, "nginx did not start listening"
                    time.sleep(0.05)
            return Proxy(https_port, http_port, prefix)
        yield start


def client(proxy: Proxy, tls: Tls, host: str = DOMAIN) -> httpx.Client:
    return httpx.Client(base_url=proxy.url, verify=tls.context(), headers={"Host": host},
                        timeout=10)


def mcp_post(http: httpx.Client, body, **headers) -> httpx.Response:
    return http.post("/mcp", content=body if isinstance(body, bytes) else json.dumps(body),
                     headers={"Content-Type": "application/json",
                              "Accept": "application/json, text/event-stream", **headers})


# --- stdlib echo upstream ----------------------------------------------------

@pytest.fixture
def echo_upstream():
    """An upstream that records each request's headers and answers 200 {}."""
    seen: list[dict[str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def _answer(self):
            seen.append({k.lower(): v for k, v in self.headers.items()})
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        do_GET = do_POST = _answer

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture(scope="module")
def app(tmp_path_factory):
    """The real app, allowing the Host that nginx forwards."""
    with running_http_server(tmp_path_factory.mktemp("app"), MCP_ALLOWED_HOSTS=DOMAIN) as server:
        yield server


# --- forwarded headers -------------------------------------------------------

def test_forwarded_headers_are_set_by_nginx_not_the_client(start_nginx, echo_upstream, tls):
    port, seen = echo_upstream
    proxy = start_nginx(port)
    with client(proxy, tls) as http:
        response = mcp_post(http, {"jsonrpc": "2.0", "id": 1, "method": "ping"}, **{
            "X-Forwarded-For": "203.0.113.9",
            "X-Real-IP": "203.0.113.9",
            "X-Forwarded-Host": "evil.example",
            "X-Forwarded-Proto": "http",
        })
    assert response.status_code == 200
    assert len(seen) == 1
    headers = seen[0]
    assert headers["host"] == DOMAIN
    assert headers["x-forwarded-for"] == "127.0.0.1"
    assert headers["x-real-ip"] == "127.0.0.1"
    assert headers["x-forwarded-proto"] == "https"
    assert "x-forwarded-host" not in headers


def test_body_over_16k_is_rejected_by_nginx(start_nginx, echo_upstream, tls):
    port, seen = echo_upstream
    proxy = start_nginx(port)
    with client(proxy, tls) as http:
        response = mcp_post(http, b"x" * (20 * 1024))
    assert response.status_code == 413
    assert response.headers.get("server") == "nginx"  # server_tokens off: no version
    assert seen == []  # never reached the upstream


def test_unknown_host_connection_is_closed(start_nginx, echo_upstream, tls):
    port, seen = echo_upstream
    proxy = start_nginx(port)
    for host in ("other.example.test", f"127.0.0.1:{proxy.https_port}"):
        with client(proxy, tls, host=host) as http:
            # 444: no response at all; seen as a clean close or a reset.
            with pytest.raises((httpx.RemoteProtocolError, httpx.ReadError)):
                mcp_post(http, {"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert seen == []


def _raw_exchange(port: int, request: bytes) -> bytes:
    """Send raw bytes to 127.0.0.1:`port` and read until nginx closes (bounded)."""
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(request)
        chunks = []
        while chunk := sock.recv(4096):
            chunks.append(chunk)
    return b"".join(chunks)


def test_catch_all_error_pages_do_not_disclose_the_nginx_version(
        start_nginx, echo_upstream, tls):
    # Errors nginx raises before routing never reach `return 444`; the default
    # (catch-all) server answers them with a 400 page instead. With no Host to
    # match, both of these land on the catch-all.
    port, seen = echo_upstream
    proxy = start_nginx(port)
    banner = subprocess.run([NGINX, "-v"], capture_output=True, text=True, timeout=10).stderr
    version = re.search(r"nginx/[0-9.]+", banner).group().encode()
    for listen_port, request in [
        (proxy.http_port, b"NOT A VALID REQUEST LINE\r\n\r\n"),
        (proxy.https_port, b"GET / HTTP/1.0\r\n\r\n"),  # plain HTTP to the TLS port
    ]:
        response = _raw_exchange(listen_port, request)
        assert response.startswith(b"HTTP/1.1 400 "), response[:200]
        assert b"\r\nServer: nginx\r\n" in response, response[:400]
        assert version not in response, response
    assert seen == []


def test_only_the_exact_mcp_path_is_proxied(start_nginx, echo_upstream, tls):
    port, seen = echo_upstream
    proxy = start_nginx(port)
    with client(proxy, tls) as http:
        assert http.get("/").status_code == 404
        assert http.get("/mcp/extra").status_code == 404
        assert http.get("/mcpx").status_code == 404
    assert seen == []


# --- the real app behind nginx -----------------------------------------------

async def _sdk_session(proxy: Proxy, tls: Tls, hold_seconds: float = 0):
    """Initialize through nginx, list tools, keep the initialized session open
    for `hold_seconds`, then call every tool. Bounded by an overall deadline."""
    http_client = httpx.AsyncClient(verify=tls.context(), headers={"Host": DOMAIN},
                                    timeout=httpx.Timeout(30, read=60))
    with anyio.fail_after(60 + hold_seconds):
        async with http_client:
            async with streamable_http_client(f"{proxy.url}/mcp",
                                              http_client=http_client) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = {tool.name for tool in (await session.list_tools()).tools}
                    await anyio.sleep(hold_seconds)
                    results = [await session.call_tool(name, arguments)
                               for name, arguments, _, _ in TOOL_CALLS]
    return tools, results


def test_all_tools_through_tls_proxy(start_nginx, app, tls):
    proxy = start_nginx(app.port)
    tools, results = anyio.run(_sdk_session, proxy, tls)
    assert tools == {"get_current_time", "convert_time", "time_until"}
    for (name, arguments, expect_error, fields), result in zip(TOOL_CALLS, results):
        assert result.isError is expect_error, (name, arguments)
        payload = json.loads(result.content[0].text)
        assert set(payload) == fields
        assert payload == result.structuredContent


def test_app_without_host_allowlist_returns_421(start_nginx, tls, tmp_path):
    # Proves nginx really forwards the public Host: the app rejects it unless
    # MCP_ALLOWED_HOSTS lists it.
    with running_http_server(tmp_path) as bare_app:
        proxy = start_nginx(bare_app.port)
        with client(proxy, tls) as http:
            assert mcp_post(http, initialize_body()).status_code == 421


def test_burst_gets_429_and_normal_use_recovers(start_nginx, app, tls):
    proxy = start_nginx(app.port)
    with client(proxy, tls) as http:
        assert mcp_post(http, initialize_body()).status_code == 200
        statuses = [mcp_post(http, initialize_body()).status_code for _ in range(30)]
        assert statuses.count(429) >= 5, statuses
        assert set(statuses) <= {200, 429}, statuses
        time.sleep(1.5)  # the bucket refills at 2 requests per second
        assert mcp_post(http, initialize_body()).status_code == 200


# The app's event stream (GET /mcp) sends a keep-alive ping at sse-starlette's
# default interval; the MCP SDK does not override it.
PING_SECONDS = EventSourceResponse.DEFAULT_PING_INTERVAL
# Long enough to cross the first ping and the 15 s read timeout this config
# used to have, which raced that ping.
HOLD_SECONDS = 20


def test_read_timeout_leaves_a_full_ping_interval_of_margin():
    timeouts = re.findall(r"^\s*proxy_read_timeout\s+(\d+)s;", EXAMPLE.read_text(), re.M)
    assert len(timeouts) == 1, timeouts
    assert int(timeouts[0]) >= 2 * PING_SECONDS, (timeouts[0], PING_SECONDS)


def test_initialized_session_stays_usable_past_the_ping_interval(start_nginx, app, tls):
    assert HOLD_SECONDS > PING_SECONDS
    proxy = start_nginx(app.port)
    tools, results = anyio.run(_sdk_session, proxy, tls, HOLD_SECONDS)
    assert tools == {"get_current_time", "convert_time", "time_until"}
    assert [r.isError for r in results] == [expect for _, _, expect, _ in TOOL_CALLS]

    assert "upstream timed out" not in (proxy.prefix / "error.log").read_text()
    # nginx logs the event stream once, when it ends at session close. An
    # earlier entry would mean the stream was closed mid-session.
    deadline = time.monotonic() + 5
    while True:
        streams = [line for line in (proxy.prefix / "access.log").read_text().splitlines()
                   if '"GET /mcp ' in line]
        if streams or time.monotonic() > deadline:
            break
        time.sleep(0.1)
    assert len(streams) == 1, streams
    status, sent = re.search(r'"GET /mcp [^"]*" (\d{3}) (\d+)', streams[0]).groups()
    assert status == "200", streams[0]
    # A stream that carried nothing logs only the chunked terminator; the app
    # sends nothing unprompted on this stream except its keep-alive pings.
    assert int(sent) > len(b"0\r\n\r\n"), "no keep-alive ping reached the client"
