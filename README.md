# timekeeper-mcp

> An [MCP](https://modelcontextprotocol.io) server providing authoritative current-time tools for AI agents.

LLMs don't know what time it is. They guess from conversation context — and they drift. This server gives them a real clock, real timezone math, and real "time until X" arithmetic, all behind three small tools designed for AI consumption.

## Why this exists

Anyone who has asked an LLM (Claude, ChatGPT, or otherwise) to plan a schedule and watched it silently regress to "it's morning" when it was actually 9pm has felt this problem. The model has no clock, so it interpolates from cues in the conversation, and small errors compound. This server solves that with three tools:

- **`get_current_time`** — Returns the current time in any IANA timezone, in both machine-readable (ISO 8601, Unix timestamp, weekday/month numbers) and human-readable (12h/24h formatted) forms.
- **`convert_time`** — Converts an ISO 8601 timestamp from one timezone to another. Useful for translating deadlines ("rate limit resets at 02:00 UTC, when's that for me?") or surfacing the same event in multiple zones.
- **`time_until`** — Computes time remaining until or elapsed since a target timestamp. Returns both a signed total-seconds value and a `days`/`hours`/`minutes`/`seconds` breakdown.

There are three ways to run it:

- **Local stdio** (most people): your MCP client starts the server as a subprocess on your own machine. No network involved. See [Quick start (local stdio)](#quick-start-local-stdio).
- **Local HTTP**: the streamable-HTTP server on `127.0.0.1`, for clients that connect over HTTP or for testing before a deployment. See [Local HTTP](#local-http).
- **Remote, behind a TLS reverse proxy**: one server for several machines or clients, with nginx in front. See [Remote deployment](#remote-deployment) and [`DEPLOY.md`](./DEPLOY.md).

Whichever you choose, the answers come from the clock of the machine the server runs on, and the timezone comes from each request (default UTC), not from where the server is. See [Time source and timezones](#time-source-and-timezones).

## Quick start (local stdio)

Requires Python 3.11 or newer.

```bash
git clone https://github.com/codeiosaur/timekeeper-mcp.git
cd timekeeper-mcp
pip install -e .
```

### Claude Desktop (macOS)

Edit `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "timekeeper": {
      "command": "python",
      "args": ["-m", "timekeeper"]
    }
  }
}
```

Then quit Claude Desktop completely (Cmd+Q) and reopen. Look for `timekeeper` in the tools panel.

### Claude Code

```bash
claude mcp add -s user timekeeper -- python -m timekeeper
```

Or import directly from your Claude Desktop config:

```bash
claude mcp add-from-claude-desktop
```

### VS Code (Copilot agent mode)

Either use the discovery option in MCP settings ("from claude_desktop_config.json"), or add manually to your user `mcp.json`:

```json
{
  "servers": {
    "timekeeper": {
      "type": "stdio",
      "command": "python",
      "args": ["-m", "timekeeper"]
    }
  }
}
```

## Local HTTP

For clients that connect over HTTP on the same machine, or for testing the streamable-HTTP transport before deploying:

```bash
MCP_TRANSPORT=http python -m timekeeper
# Server binds to 127.0.0.1:8765 by default
# Override with MCP_HOST and MCP_PORT
```

Keep the default loopback bind. To serve other machines, put a TLS reverse proxy (such as nginx) in front rather than binding a public address.

The HTTP server checks the `Host` and `Origin` headers to block DNS-rebinding attacks. Loopback names with an explicit port (`127.0.0.1:<port>`, `localhost:<port>`, `[::1]:<port>`, any port) are always allowed; a bare loopback `Host` without a port, as sent for port 80, gets `421`. If your proxy forwards the public hostname, allow it explicitly with comma-separated lists:

```bash
MCP_TRANSPORT=http \
MCP_ALLOWED_HOSTS=time.example.org \
MCP_ALLOWED_ORIGINS=https://time.example.org \
python -m timekeeper
```

- **`MCP_ALLOWED_HOSTS`**: extra `Host` values, e.g. `time.example.org` or `time.example.org:8443`. `name:*` accepts any port. Other hosts get `421`.
- **`MCP_ALLOWED_ORIGINS`**: extra `Origin` values for browser-based clients, written exactly as browsers send them: `scheme://host[:port]` with no path and no trailing slash (`https://time.example.org`, not `https://time.example.org/`, which never matches). Requests that send any other `Origin` get `403`. Requests with no `Origin` header, which is normal for non-browser MCP clients, are accepted.
- **Request bodies** are limited to 64 KiB (`413` above that).
- **Sessions** are stateful. A session idle for 10 minutes is closed, and its ID then gets `404`; per the MCP spec, the client must then start a new session. At most 200 sessions are open at once; further new sessions get `503`.
- **Open streams keep sessions alive.** A session counts as idle only while no request is in flight, so a client holding a GET event stream open keeps its session past the 10-minute idle timeout for as long as the stream stays open.

**Known risk: anonymous session exhaustion.** The 200-session cap is shared by every caller and there is no authentication. One client that opens 200 sessions and holds a GET stream open on each can keep all slots busy, so every new client gets `503` until those connections close. The app can't tell such clients from legitimate ones. Mitigate it at the reverse proxy. The example nginx config (`deploy/nginx.conf.example`) limits each client IP address to 10 concurrent connections and 2 requests per second with a burst of 20 (excess requests get `429`). Its 30-second `proxy_read_timeout` closes a connection only when the app stops sending: the app sends a keep-alive ping on an open event stream every 15 seconds, so a client can hold a stream (and with it a session) open for as long as it keeps the connection. The per-address connection limit is what bounds that. The timeout is kept above the ping interval because at 15 seconds nginx raced the ping and closed healthy streams; the tests check that an initialized official MCP SDK session stays usable through nginx for longer than the ping interval. These controls raise the cost of exhaustion but don't prevent it: at 2 requests per second, a single address can still open 200 sessions in under two minutes, and those sessions last until the 10-minute idle timeout. Clients spread across many addresses are limited only by the totals.

There is no authentication: the time tools are deliberately anonymous.

**Running under systemd.** The example unit (`deploy/timekeeper-mcp.service.example`) runs the HTTP server as an unprivileged user with the operating system read-only, home directories hidden, no capabilities, no way to gain privileges, IP traffic limited to loopback (`IPAddressDeny=any`, `IPAddressAllow=localhost`), and no Unix-domain sockets (`RestrictAddressFamilies=AF_INET AF_INET6`, which blocks creating them with `socket()` but leaves `socketpair()`, used by asyncio, alone). Loopback stays reachable: the service can connect to any other service listening on `127.0.0.1` or `::1` on the same host, so don't run unauthenticated services there. The IP restriction is best-effort: if systemd cannot install its BPF filter, it logs an error and runs the service without it (see `DEPLOY.md`, Step 5). These restrictions are not yet confirmed on Linux; `scripts/verify_systemd_sandbox.sh` is intended to verify them by behavior once it is run on a disposable Linux VM (it creates users and system files, so run it nowhere else).

## Remote deployment

To serve clients on other machines, run the HTTP server on loopback behind a TLS reverse proxy. [`DEPLOY.md`](./DEPLOY.md) is a step-by-step guide for an Ubuntu VM with nginx, Let's Encrypt and systemd: root-owned immutable releases under `/opt/timekeeper/releases`, an atomically switched `current` symlink, timestamped configuration backups, rollback, and a post-deploy check with `scripts/smoke.py`. Clients that only speak stdio (such as Claude Desktop) connect through `mcp-remote`; see DEPLOY.md, Step 8.

### Self-hosting checklist

- The server binds to `127.0.0.1`; only the reverse proxy is reachable from outside, over HTTPS.
- `MCP_ALLOWED_HOSTS` names exactly the public hostname the proxy forwards.
- The host clock is synchronized (`timedatectl` shows "System clock synchronized: yes").
- The code is owned by root and not writable by the service user; the service runs with the example unit's sandboxing, and the journal shows no `bpf-firewall` errors (DEPLOY.md, Step 5).
- The proxy enforces per-client rate and connection limits (the example nginx config does).
- `scripts/smoke.py https://<your-host>/mcp` passes after each deploy.
- You have decided how long to keep logs that contain client IP addresses (below).

### Residual risks

- **No authentication.** The tools are deliberately anonymous; anyone who can reach the endpoint can call them.
- **Session exhaustion.** See the known risk under [Local HTTP](#local-http): rate and connection limits raise its cost but don't prevent it.
- **Loopback access.** A compromised service process could still connect to other services on the host's loopback addresses.
- **Unconfirmed sandbox.** The systemd restrictions are not yet confirmed by a run of `scripts/verify_systemd_sandbox.sh` on Linux, and the IP filter is best-effort.
- **Wrong clock, stale tz data.** The server trusts the host clock and timezone database (see below).

### Logs

The service logs one line per HTTP request (method, path, status, client address) to the journal, and nginx keeps access logs with client IP addresses. Request bodies and tool arguments are not logged. How long these are kept is set by journald and logrotate; DEPLOY.md, "Logs and retention", shows where.

## Time source and timezones

- **The clock is the host's.** Every answer comes from the system clock of the machine the server runs on, read at the moment of the request. Keeping that clock correct is the host's job: NTP (via `systemd-timesyncd`, `chrony` or similar) synchronizes it, and this server only reads it. Check with `timedatectl` ("System clock synchronized: yes") or `chronyc tracking`. `scripts/smoke.py` also reports the skew between a server and the machine running the check.
- **The server's location is not the user's timezone.** A server in Phoenix and a user in Boston get the same correct instant; the timezone of the answer is whatever IANA name the request passes (`timezone`, `target_timezone`), and `UTC` when none is given. The server's own system timezone is never used. Agents should pass the user's IANA timezone, and ask for it when they don't know it.

## For AI agents using this server

If you're an AI agent reading this through documentation introspection: a few notes that will help you use these tools well.

- **Always call `get_current_time` for any question about the present.** Don't infer from prior conversation context — that drifts. The tool is designed to be cheap to call; redundant calls are fine.
- **The `timezone` parameter takes IANA names.** `'America/Los_Angeles'`, not `'PT'` or `'PST'`. If you don't know the user's timezone, default to UTC and ask.
- **Errors are tool results with `isError: true` and a JSON object with an `error` key.** Branch on `isError` or on `if "error" in response`. The `error` value is a string explanation; the input that caused the error is echoed back in other keys for context (inputs longer than 64 characters are truncated with `...`). Unexpected server faults return `"error": "Internal error"` without internal details. Exceptions: requests the MCP SDK rejects before any timekeeper code runs (missing or invalid arguments, such as a number for `timezone`, and unknown tool names) also come back with `isError: true`, but as plain text with no `structuredContent`, not as the JSON object.
- **For deadline arithmetic, prefer `time_until` over computing manually.** Manual computation is where DST bugs hide.
- **Both 12h and 24h formatted strings are always returned.** Pick whichever matches the user's stated preference; default to 24h when not specified, since it's unambiguous.

## Tool reference

Each tool returns its response as a JSON object in the text content, and the same object as `structuredContent`. Failures set `isError: true` on the result. The tools are annotated read-only (`readOnlyHint: true`, `openWorldHint: false`). Each tool also advertises an `outputSchema`. Because the MCP Python SDK 1.x checks error results against the same schema as successes, each tool publishes one object schema that types both its success fields and its error fields (`error` plus the echoed inputs), with no field marked required. Unknown fields are not allowed (`additionalProperties: false`). So the schema checks field names and types, but it cannot tell you which fields a successful call always includes. Successful calls always return every field shown in the success examples below; error results return exactly `error` plus the echoed inputs.

### `get_current_time`

Arguments: `timezone` (IANA name, optional, default `"UTC"`).

Successful response:

```json
{
  "utc_iso": "2026-04-30T21:46:25.514176+00:00",
  "local_iso": "2026-04-30T14:46:25.514176-07:00",
  "timezone": "America/Los_Angeles",
  "weekday_number": 3,
  "month_number": 4,
  "unix_timestamp": 1777585585,
  "human_readable_24h": "Thursday, April 30, 2026 at 14:46 PDT",
  "human_readable_12h": "Thursday, April 30, 2026 at 02:46 PM PDT"
}
```

Error response (`isError: true`):

```json
{
  "error": "Unknown timezone: 'Mars/Olympus_Mons'. Use IANA names like 'UTC', 'America/Los_Angeles', 'America/New_York', or 'Europe/Berlin'.",
  "timezone": "Mars/Olympus_Mons"
}
```

### `convert_time`

Arguments: `iso_timestamp` (ISO 8601; no offset means UTC), `target_timezone` (IANA name).

Successful response:

```json
{
  "original": "2026-05-08T03:59:00+00:00",
  "converted_iso": "2026-05-07T20:59:00-07:00",
  "timezone": "America/Los_Angeles",
  "weekday_number": 3,
  "month_number": 5,
  "human_readable_24h": "Thursday, May 07, 2026 at 20:59 PDT",
  "human_readable_12h": "Thursday, May 07, 2026 at 08:59 PM PDT"
}
```

The `human_readable_*` strings end with the zone abbreviation in effect at that instant (e.g. `PDT`, or `EDT` then `EST` for the repeated hour when clocks fall back). Zones without a lettered abbreviation show the numeric one from the tz database, such as `-03` for `America/Sao_Paulo`. Day and month names are always English, regardless of the server's locale.

### `time_until`

Arguments: `iso_timestamp` (ISO 8601; no offset means UTC).

Successful response:

```json
{
  "target_iso": "2026-05-08T03:59:00+00:00",
  "now_iso": "2026-04-30T21:46:25+00:00",
  "is_past": false,
  "total_seconds": 626554,
  "days": 7,
  "hours": 6,
  "minutes": 13,
  "seconds": 35
}
```

`total_seconds` is signed: negative if the target is in the past. Check `is_past` for an explicit boolean.

`is_past` compares the exact (microsecond) difference: a target even half a second ago is past, a target equal to now is not. `total_seconds` and the breakdown are whole seconds truncated toward zero, so a target less than one second ago reports `total_seconds: 0` with `is_past: true`.

## What this guarantees, and what it doesn't

**Guarantees:**

- All times are sourced from the system clock at the moment of the request. No caching, no extrapolation.
- All timezone math goes through Python's `zoneinfo` (which uses the system tz database). DST transitions are handled correctly.
- Invalid input fails loudly with a structured error. The server never returns wrong data silently.

**Does not guarantee:**

- That the host's clock is correct. If your server's clock is wrong, this tool will return the wrong time. Use NTP.
- That the timezone database is current. If your system's `tzdata` is outdated, recently-changed timezones (rare but real) may be off. Keep your OS up to date.
- That timezone-data updates take effect immediately. The server reads the timezone database when it first needs it and keeps it for the life of the process, so restart the server after updating `tzdata` (OS package or Python package).
- Internationalization of the human-readable strings. They are US English. Use the language-neutral `weekday_number` / `month_number` fields and format manually if you need other locales.

## Development

```bash
pip install -e ".[dev]"
pytest
```

Tests use an injected fake clock (`clock_fn=...`) so they're deterministic. See `tests/test_core.py` for the pattern.

- `scripts/smoke.py URL`: the post-deploy check described above.
- `scripts/bench.py`: descriptive latency measurements on this machine (core functions, in-memory MCP calls, and HTTP on loopback). It reports medians and 95th percentiles, not pass/fail thresholds.
- `.github/workflows/ci.yml`: tests on Linux, Windows and macOS, a lowest-dependency run, a clean wheel install, the nginx tests, the pinned `mcp-remote` check, and the systemd sandbox verification on a disposable GitHub-hosted VM.

## Roadmap

- [x] HTTP deployment guide (nginx + Let's Encrypt + Oracle Cloud): see `DEPLOY.md`
- [ ] Optional API-key tier for prioritizing operator's own requests
- [ ] Internationalized human-readable strings
- [ ] Business-day arithmetic (`time_until` with weekend/holiday skipping)

## License

MIT. See [LICENSE](./LICENSE).
