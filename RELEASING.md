# Releasing

A maintainer checklist for tagging a release. Nothing here is automated, and nothing is published to PyPI. Run the build and install checks in a temporary directory, not in your checkout. The commands use a shell variable for the version being released:

```bash
VERSION=0.2.0
```

## 1. Before tagging

1. The release's changes are merged to `main`, and you are on a clean `main` at the commit to be tagged.
2. CI passed on that commit, including the "systemd sandbox verification", nginx and `mcp-remote` jobs (check the Actions tab, or `gh run list --branch main --limit 5`).
3. The version agrees everywhere:
   - `version` in `pyproject.toml` and `__version__` in `timekeeper/__init__.py` (`tests/test_packaging.py` checks that they match);
   - `uv.lock` (`uv lock --check` must pass);
   - the newest heading in `CHANGELOG.md`, which carries the release date instead of "(unreleased)" (change that in the release's pull request, before merging).
4. Build and inspect the distributions in a temporary directory:
   ```bash
   uv build --out-dir /tmp/timekeeper-dist .
   ls /tmp/timekeeper-dist   # timekeeper_mcp-$VERSION-py3-none-any.whl and timekeeper_mcp-$VERSION.tar.gz
   unzip -p /tmp/timekeeper-dist/timekeeper_mcp-$VERSION-py3-none-any.whl '*/METADATA' \
     | grep -E '^(Version|Requires-Python):'    # Version: $VERSION, Requires-Python: >=3.11
   ```
5. Install the wheel into an isolated, temporary tool environment and smoke-test it from outside the checkout:
   ```bash
   export UV_TOOL_DIR=/tmp/timekeeper-tools UV_TOOL_BIN_DIR=/tmp/timekeeper-bin
   uv tool install /tmp/timekeeper-dist/timekeeper_mcp-$VERSION-py3-none-any.whl
   MCP_TRANSPORT=http MCP_PORT=8799 /tmp/timekeeper-bin/timekeeper-mcp &
   python scripts/smoke.py http://127.0.0.1:8799/mcp     # expect "PASS all smoke checks"
   kill %1
   ```

## 2. Tag and release

The tag is `v` plus the version (`v$VERSION`, for example `v0.2.0`), which is also the form `DEPLOY.md` expects. Write this version's `CHANGELOG.md` section to a file for the release notes first:

```bash
git tag -a "v$VERSION" -m "timekeeper-mcp $VERSION"
git push origin "v$VERSION"
gh release create "v$VERSION" --verify-tag --title "timekeeper-mcp $VERSION" --notes-file /tmp/timekeeper-release-notes.md
```

## 3. After tagging

1. Check that the tag installs, in a temporary tool environment as above:
   ```bash
   uv tool install "git+https://github.com/codeiosaur/timekeeper-mcp@v$VERSION"
   ```
   Then run the same smoke test.
2. If that works, consider offering this command in the README's install section, next to the source-checkout instructions.
3. Before upgrading a server, deploy the tag on a test VM first, following `DEPLOY.md` with `T="v$VERSION"`.
4. Add a new "(unreleased)" section at the top of `CHANGELOG.md` when the next change lands.
