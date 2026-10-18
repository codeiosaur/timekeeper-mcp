# Architecture

This is a small project. The architecture is correspondingly small. The point of writing it down is to keep it small as it changes hands between sessions and contributors.

## Layered structure (hexagonal-lite)

```
┌─────────────────────────────────────────────┐
│              Transport (MCP)                │
│  ┌───────────────────────────────────────┐  │
│  │           Adapter (server.py)         │  │
│  │  ┌─────────────────────────────────┐  │  │
│  │  │      Domain (core.py)           │  │  │
│  │  │  Pure functions. No I/O.        │  │  │
│  │  │  No MCP. No HTTP.               │  │  │
│  │  └─────────────────────────────────┘  │  │
│  │      ┌──────────────────────────┐     │  │
│  │      │   Formatting             │     │  │
│  │      │   (formatting.py)        │     │  │
│  │      │   Pure helpers.          │     │  │
│  │      └──────────────────────────┘     │  │
│  └───────────────────────────────────────┘  │
└─────────────────────────────────────────────┘
            ▲
            │
       __main__.py
       picks transport
       (stdio | http)
       from MCP_TRANSPORT env
```

The principle: **the inner layers don't know about the outer layers.** `core.py` has no idea it's being called by an MCP server. `server.py` doesn't know whether it's running over stdio or HTTP. This is what lets us add HTTP transport without touching `core.py` at all.

## Module responsibilities

### `timekeeper/core.py` — Domain

**Owns:** All time computation. Timezone validation, DST handling, ISO parsing, duration arithmetic.

**Knows about:** `datetime`, `zoneinfo`. That's it.

**Does NOT know about:** MCP, HTTP, FastMCP, formatting strings, dictionaries-as-error-shapes.

**API:** Three pure functions (`get_time_in_zone`, `convert_to_zone`, `time_until`); those that read the current time accept an optional `clock_fn` for testability. A fourth helper, `zoned`, returns a `ZoneInfo`-backed datetime that `server.py` passes to formatting so output shows zone abbreviations (e.g. `PDT`). Errors are raised as `TimekeeperError`.

### `timekeeper/formatting.py` — Formatting helpers

**Owns:** Turning `datetime` objects into human-readable strings (12h and 24h, US English).

**Knows about:** `datetime` and strftime format codes.

**Does NOT know about:** MCP, the domain, anything stateful.

**Why separate from `core`:** Consumers who only want machine-readable output (Unix timestamps, ISO strings, weekday numbers) shouldn't pay the cost of formatting they don't use. Also: when we add internationalization in v2, only this file changes.

### `timekeeper/server.py` — MCP adapter

**Owns:** Tool registration with FastMCP. Translation between MCP's protocol and the domain. Error-shape translation: every tool returns an explicit `CallToolResult` whose JSON text (and `structuredContent`) is the result dict, and errors set `isError=True` with `{"error": ..., ...echoed inputs}`. Unexpected exceptions are logged to stderr and reported only as `"Internal error"`. Each tool also declares an output model (`CurrentTimeOutput`, `ConversionOutput`, `DurationOutput`), which FastMCP publishes as its `outputSchema` and checks every result against. Because SDK 1.x checks error results against it too, each model is one object that types both the success fields and the error/echoed-input fields. All fields are optional, unknown fields are rejected, and types are checked strictly. Each tool also validates its success payload against that model inside its own error handling before returning, so malformed internal output becomes the generic `"Internal error"` result (details logged server-side) rather than reaching the client. The SDK's validation remains as a backstop; on its own it would return raw validation details as plain text.

**Knows about:** FastMCP, the domain, the formatting helpers.

**Does NOT know about:** The transport (stdio vs HTTP). That's `__main__.py`'s job.

**Why this layering:** If FastMCP releases a breaking change, this is the only file that changes. If the MCP protocol itself evolves (it has, recently — see deprecated SSE transport), this file is also the blast radius.

### `timekeeper/__main__.py` — Entry point

**Owns:** Picking the transport based on environment variables. Starting the server. For HTTP only, `configure_http()` applies transport settings from the environment: bind address, `Host`/`Origin` allowlists (DNS-rebinding protection always on), a 64 KiB request-body limit, and stateful-session bounds (10-minute idle timeout, 200 sessions). It must run before `mcp.run(transport="streamable-http")`, which is when FastMCP reads these settings to build the HTTP app.

**Knows about:** The server module, environment variables, `os.environ`, and the MCP SDK's `TransportSecuritySettings`.

**Does NOT know about:** The domain, formatting, or any specifics of what the server does.

**Why this exists:** This is the only file that knows whether we're stdio-mode or HTTP-mode. Adding new transports means adding branches here, not modifying `server.py`.

## Adding new tools

When adding a new MCP tool, follow this sequence:

1. **Add the pure function to `core.py`.** It should take inputs and return a dict. Take `clock_fn` if it needs the current time. Raise `TimekeeperError` for bad inputs.
2. **Add tests to `tests/test_core.py`.** Test happy path and at least one failure mode. Use a fake clock.
3. **If human-readable strings are needed, add helpers to `formatting.py`** and tests in `tests/test_formatting.py`.
4. **Wrap the core function in an `@mcp.tool()` in `server.py`.** Write the docstring for the LLM (directive, not descriptive). Return `_result(...)` on success. Catch `TimekeeperError` and return `_error(str(exc), **inputs)`, and catch any other `Exception`, log it, and return `_error("Internal error", **inputs)`. Add `annotations=_annotations(...)`, and annotate the return type as `Annotated[CallToolResult, <Output model>]`, where the output model lists every success and error field.
5. **Add tests to `tests/test_server.py`.** Through the in-memory MCP client, verify a success result and an error result (`isError`, JSON payload, echoed inputs).
6. **Update the README's "Tool reference" section.**

A new tool should be `< 50 lines` of production code total (across `core` and `server`) plus its tests. If it's more, ask whether you're solving the right problem.

## Why so few abstractions?

Lecture 8 of CS 3100 (and most senior engineers anywhere) will tell you to favor composition over inheritance. This project takes that further: favor *functions* over composition, favor *modules* over classes, and favor *no abstraction* over premature abstraction.

The only classes in this codebase are `TimekeeperError` and the pydantic output models in `server.py`, which exist only because FastMCP builds a tool's `outputSchema` from a model class; they hold no behavior. The "interfaces" are function signatures with type hints. The "dependency injection" is a default argument. This isn't dogma — it's that for a project this small, anything more is overhead.

If the project grows to need genuine OOP (state to manage, behavior to vary at runtime, etc.), revisit. Until then, resist.

## Testing strategy

- **`tests/test_core.py`** — Tests pure functions with injected fake clocks. These should be the bulk of the test suite. Every behavior worth guaranteeing lives here.
- **`tests/test_formatting.py`** — Smoke tests for string formatting. We trust strftime.
- **`tests/test_server.py`** — Adapter tests through a real MCP client session connected in memory: tool listing, annotations, success and error results as seen on the wire (`isError`, JSON text, `structuredContent`), and non-disclosure of internal errors. Transport-level tests live in `tests/test_http_integration.py`.

We do not aim for 100% coverage. We aim for: every behavior the README documents has a test. Every bug we've ever hit has a regression test. That's enough.

## Out of scope (for now)

These are deliberately not done. Resist the urge to do them as drive-bys.

- **Internationalized formatted strings.** v2 concern.
- **Natural language time parsing** ("next Tuesday at noon"). Adds a dependency on a parser, which itself can hallucinate. Bad fit for an authoritative-clock service.
- **Caching.** The system clock is already fast. Caching introduces staleness bugs.
- **Telemetry.** No metrics, no logs of who called what. If we add this in v2 it's via a structured logging interface, not scattered prints.
- **Auth.** v2, when remote deployment exists. See PLAN.md.
