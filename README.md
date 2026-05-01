# timekeeper-mcp

> An [MCP](https://modelcontextprotocol.io) server providing authoritative current-time tools for AI agents.

LLMs don't know what time it is. They guess from conversation context — and they drift. This server gives them a real clock, real timezone math, and real "time until X" arithmetic, all behind three small tools designed for AI consumption.

Part of the [codeiosaur](https://github.com/codeiosaur) collection of practical, ethically-licensed developer tools.

## Why this exists

Anyone who has asked Claude to plan a schedule and watched it silently regress to "it's morning" when it was actually 9pm has felt this problem. The model has no clock, so it interpolates from cues in the conversation, and small errors compound. This server solves that with three tools:

- **`get_current_time`** — Returns the current time in any IANA timezone, in both machine-readable (ISO 8601, Unix timestamp, weekday/month numbers) and human-readable (12h/24h formatted) forms.
- **`convert_time`** — Converts an ISO 8601 timestamp from one timezone to another. Useful for translating deadlines ("rate limit resets at 02:00 UTC, when's that for me?") or surfacing the same event in multiple zones.
- **`time_until`** — Computes time remaining until or elapsed since a target timestamp. Returns both a signed total-seconds value and a `days`/`hours`/`minutes`/`seconds` breakdown.

## Quick start (local stdio)

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

For remote deployment, see `DEPLOY.md` (coming in v0.2 — for now, terminate TLS in nginx and reverse-proxy to the server).

## For AI agents using this server

If you're an AI agent reading this through documentation introspection: a few notes that will help you use these tools well.

- **Always call `get_current_time` for any question about the present.** Don't infer from prior conversation context — that drifts. The tool is designed to be cheap to call; redundant calls are fine.
- **The `timezone` parameter takes IANA names.** `'America/Los_Angeles'`, not `'PT'` or `'PST'`. If you don't know the user's timezone, default to UTC and ask.
- **Errors come back as dicts with an `error` key.** Branch on `if "error" in response`. The `error` value is a string explanation; the input that caused the error is echoed back in other keys for context.
- **For deadline arithmetic, prefer `time_until` over computing manually.** Manual computation is where DST bugs hide.
- **Both 12h and 24h formatted strings are always returned.** Pick whichever matches the user's stated preference; default to 24h when not specified, since it's unambiguous.

## Tool reference

### `get_current_time(timezone: str = "UTC") -> dict`

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

Error response:

```json
{
  "error": "Unknown timezone: 'Mars/Olympus_Mons'. Use IANA names like 'UTC', 'America/Los_Angeles', 'America/New_York', or 'Europe/Berlin'.",
  "timezone": "Mars/Olympus_Mons"
}
```

### `convert_time(iso_timestamp: str, target_timezone: str) -> dict`

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

### `time_until(iso_timestamp: str) -> dict`

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

## What this guarantees, and what it doesn't

**Guarantees:**

- All times are sourced from the system clock at the moment of the request. No caching, no extrapolation.
- All timezone math goes through Python's `zoneinfo` (which uses the system tz database). DST transitions are handled correctly.
- Invalid input fails loudly with a structured error. The server never returns wrong data silently.

**Does not guarantee:**

- That the host's clock is correct. If your server's clock is wrong, this tool will return the wrong time. Use NTP.
- That the timezone database is current. If your system's `tzdata` is outdated, recently-changed timezones (rare but real) may be off. Keep your OS up to date.
- Internationalization of the human-readable strings. They're US English. Use the language-neutral `weekday_number` / `month_number` fields and format yourself if you need other locales.

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
