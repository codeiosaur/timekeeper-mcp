# Changelog

## 0.2.0 (unreleased)

The first tagged release. Version 0.1.0 was declared in the initial commit but never tagged or published; the code has since changed incompatibly, so this release gets a new version rather than reusing 0.1.0.

### Breaking changes

- **Python 3.11 or newer is required** (was 3.10). Earlier versions can't parse the documented `...Z` timestamps.
- **The MCP Python SDK must be 1.30 or newer, and below 2.**
- **Errors are now tool results with `isError: true`.** Before, a bad timezone or timestamp came back as a normal (successful) result whose JSON had an `error` key. The JSON payload is unchanged (`{"error": ..., ...echoed inputs}`), except that echoed inputs longer than 64 characters are truncated.
- **HTTP transport checks `Host` and `Origin`.** Requests for hosts that are not allowed get `421`, and disallowed browser origins get `403`. Behind a reverse proxy that forwards the public hostname, list it in `MCP_ALLOWED_HOSTS`. Request bodies are limited to 64 KiB, and sessions to 200 at a time with a 10-minute idle timeout.

### Fixed

- `time_until` decides `is_past` from the exact difference, so a target half a second ago is in the past, and very large durations are exact to the second.
- Conversions whose result falls outside years 1–9999 return an error instead of crashing.
- Timezone names are validated against the IANA list the same way on every operating system: path-like input, wrong case (`utc`), and host-specific entries such as `localtime` and `posixrules` are rejected. The list is read once per process instead of on every request.
- The human-readable strings show the zone abbreviation in effect (`PDT`; `EDT` then `EST` for the repeated hour when clocks fall back), and day and month names are always English, whatever the server's locale.
- Unexpected server faults return `"error": "Internal error"` without internal details; the details go to the server log.

### Added

- Tool annotations (read-only, closed-world) and an output schema for each tool.
- HTTP configuration through `MCP_ALLOWED_HOSTS` and `MCP_ALLOWED_ORIGINS`.
- `tzdata` as a dependency on Windows, which has no system timezone database.
- Deployment: a sandboxed systemd unit, an nginx example (TLS, per-client rate and connection limits, a catch-all for unknown hosts), and a rewritten `DEPLOY.md` with root-owned, immutable releases, configuration backups and rollback.
- Scripts: `smoke.py` (post-deploy check), `bench.py` (descriptive latency measurements), and `verify_systemd_sandbox.sh` (behavioral check of the systemd unit on a disposable Linux VM).
- CI: tests on Linux, Windows and macOS, a run on the lowest allowed dependencies, a clean wheel install, the nginx tests, a pinned `mcp-remote` check, and the systemd sandbox verification.
- Installation instructions for an isolated install from a source checkout, with clients pointed at the installed `timekeeper-mcp` executable.

### Known limitations

- Not published on PyPI. Install from a source checkout (see the README), or, once this release is tagged, from its Git tag.
- No authentication, by design: the server is anonymous.
- The server reports the MCP SDK's version, not timekeeper-mcp's, in the `serverInfo` it sends clients during initialization.
- The systemd sandbox has been verified on a disposable GitHub-hosted Ubuntu 22.04 VM only; see "What the Linux verification covers" in the README.
