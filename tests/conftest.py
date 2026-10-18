"""Shared fixtures: HTTP servers running the real entrypoint as subprocesses.

See `tests/harness.py` for the readiness, port, and teardown rules.
"""
from __future__ import annotations

from contextlib import ExitStack

import pytest

from tests.harness import running_http_server


@pytest.fixture(scope="module")
def http_server(tmp_path_factory):
    """A default-configured HTTP server shared by one test module."""
    with running_http_server(tmp_path_factory.mktemp("http-server")) as server:
        yield server


@pytest.fixture(scope="module")
def deployed_http_server(tmp_path_factory):
    """An HTTP server configured as if behind a proxy for time.example.org."""
    with running_http_server(
        tmp_path_factory.mktemp("deployed-http-server"),
        MCP_ALLOWED_HOSTS="time.example.org",
        MCP_ALLOWED_ORIGINS="https://time.example.org",
    ) as server:
        yield server


@pytest.fixture
def start_http_server(tmp_path):
    """Factory for a fresh server owned by one test; stopped at teardown.

    Call as `start_http_server(python_args=[...], ENV_VAR="value")`.
    """
    with ExitStack() as stack:
        def start(python_args: list[str] | None = None, **env_overrides: str):
            return stack.enter_context(
                running_http_server(tmp_path, python_args, **env_overrides)
            )
        yield start
