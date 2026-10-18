# timekeeper-mcp

> An [MCP](https://modelcontextprotocol.io) server providing authoritative current-time tools for AI agents.

LLMs don't know what time it is. They guess from conversation context — and they drift. This server gives them a real clock, real timezone math, and real "time until X" arithmetic, all behind three small tools designed for AI consumption.

## Why this exists

Anyone who has asked an LLM (Claude, ChatGPT, or otherwise) to plan a schedule and watched it silently regress to "it's morning" when it was actually 9pm has felt this problem. The model has no clock, so it interpolates from cues in the conversation, and small errors compound. This server solves that with three tools:

- **`get_current_time`** — Returns the current time in any IANA timezone, in both machine-readable (ISO 8601, Unix timestamp, weekday/month numbers) and human-readable (12h/24h formatted) forms.
- **`convert_time`** — Converts an ISO 8601 timestamp from one timezone to another. Useful for translating deadlines ("rate limit resets at 02:00 UTC, when's that for me?") or surfacing the same event in multiple zones.
- **`time_until`** — Computes time remaining until or elapsed since a target timestamp. Returns both a signed total-seconds value and a `days`/`hours`/`minutes`/`seconds` breakdown.

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

## Quick start (HTTP)

For testing the streamable-HTTP transport locally before deploying:

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

**Known risk: anonymous session exhaustion.** The 200-session cap is shared by every caller and there is no authentication. One client that opens 200 sessions and holds a GET stream open on each can keep all slots busy, so every new client gets `503` until those connections close. The app can't tell such clients from legitimate ones. Mitigate it at the reverse proxy with per-IP connection limits and read timeouts. Planned hardening (phase 5) adds these to the example nginx config (`limit_conn`, `limit_req`, short proxy timeouts) and checks that the timeouts don't cut off legitimate clients' streams.

There is no authentication: the time tools are deliberately anonymous.

For remote deployment, see `DEPLOY.md` (coming in v0.2 — for now, terminate TLS in nginx and reverse-proxy to the server).

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

## Roadmap

- [ ] HTTP deployment guide (nginx + Let's Encrypt + Oracle Cloud)
- [ ] Optional API-key tier for prioritizing operator's own requests
- [ ] Internationalized human-readable strings
- [ ] Business-day arithmetic (`time_until` with weekend/holiday skipping)

## License

MIT. See [LICENSE](./LICENSE).
