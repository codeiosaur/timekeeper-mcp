# CLAUDE.md

Behavioral guidelines for Claude Code (and any other AI coding agent) working on `timekeeper-mcp`. Read this first. Then read `ARCHITECTURE.md` for the structural picture and `PLAN.md` for what's being built and what's next.

> **Attribution:** This document is adapted from [Karpathy-inspired skills guidelines](https://github.com/forrestchang/andrej-karpathy-skills) by Forrest Chang, licensed under MIT. Used with permission.

## How to start a session

1. **Read this file completely.**
2. **Read `ARCHITECTURE.md`** — it explains the module boundaries and which file owns which concern. Most "where do I put this?" questions are answered there.
3. **Understand the current task.** This may come from the session prompt, the user, a ticket, or commit history. If uncertain what you're building or why, ask before starting.
4. **Run `pytest`** before making any changes, to confirm the baseline is green.
5. Only then start implementing.

## Four principles

These are adapted from [Karpathy-inspired guidelines](https://github.com/forrestchang/andrej-karpathy-skills) and tuned for this project.

### 1. Think before coding

Don't assume. Don't hide confusion. Surface tradeoffs.

- State assumptions explicitly. If uncertain, ask.
- If multiple interpretations of a request exist, present them — don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

### 2. Simplicity first

Minimum code that solves the problem. Nothing speculative.

- No features beyond what's asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

This project is small on purpose. Three tools. Three modules. Resist the urge to "improve" the structure.

### 3. Surgical changes

Touch only what you must. Clean up only your own mess.

- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code or a real bug elsewhere, mention it — don't fix it as a drive-by.
- Every changed line should trace to the user's request.

### 4. Goal-driven execution

Define success criteria. Loop until verified.

For this project, "done" almost always means:

1. **The change is implemented in the right module** (see ARCHITECTURE.md).
2. **A test exists that would have caught the bug or proves the new behavior.**
3. **`pytest` passes.**
4. **No unrelated changes appear in the diff.**

For multi-step tasks, write a brief plan in chat first:

```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
```

Then execute.

## Project-specific rules

These are non-negotiable for this codebase:

### `core.py` is the only place that does time math

If you find yourself doing arithmetic on datetimes anywhere else (especially in `server.py`), stop and add a function to `core.py` instead. The whole point of the layering is that the MCP shell never knows about DST.

### Always inject `clock_fn` for testability

Any function in `core.py` that needs the current time takes `clock_fn: Callable[[], datetime] = _real_clock`. Tests pass a fake. This is the project's only nod to dependency injection — keep it consistent.

### Errors are exceptions in `core`, dicts in `server`

`core` raises `TimekeeperError`. `server` catches and returns `{"error": "...", ...echoed_inputs}`. Don't return error dicts from `core`; don't raise from `server` tool functions (the MCP framework will surface them poorly).

### Tool descriptions are written for AI agents, not humans

The docstrings on `@mcp.tool()` functions in `server.py` are read by the LLM that's deciding whether to call the tool. They should be directive ("always call this rather than guessing"), not descriptive. Optimize for being-reached-for, not being-pretty.

### No silent fallbacks

If something can't be computed correctly, raise. The Swiss-cheese model says: better to be down than wrong. A clock that returns wrong times is worse than a clock that's unavailable.

### Don't add dependencies casually

Current deps: `mcp[cli]`. That's it. `zoneinfo` and `datetime` are stdlib. If you want to add a dependency, justify it in the PR description and consider whether it could be implemented in stdlib first.

### Pin Python `>=3.10`

`zoneinfo` is 3.9+, FastMCP is 3.10+. Don't bump the floor without discussion.

## When in doubt

- Match the style of existing code in the same file.
- Prefer pure functions over classes.
- Prefer fewer files over more.
- Ask the user before making structural changes.

## What "good" looks like for this project

- A diff for a new feature is `< 50 lines` of production code, plus tests.
- Tests cover the happy path and one obvious failure mode. They don't cover every theoretical edge.
- README and ARCHITECTURE.md stay accurate. If your change makes them wrong, update them in the same PR.
- No new top-level modules without architectural discussion.
