"""The package version is declared twice; the two must agree.

`pyproject.toml` sets the version installers and wheels see (read here from
the installed distribution's metadata), and `timekeeper/__init__.py` sets
`__version__`. See RELEASING.md for the other places a release touches.
"""
from importlib.metadata import version

import timekeeper


def test_installed_metadata_version_matches_package_version():
    assert version("timekeeper-mcp") == timekeeper.__version__
