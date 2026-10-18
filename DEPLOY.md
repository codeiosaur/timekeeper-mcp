# DEPLOY.md — Deploying timekeeper-mcp behind nginx with TLS

This guide sets up `timekeeper-mcp` at `https://timekeeper-yourname.duckdns.org/mcp` on an Ubuntu 22.04 VM, using Oracle Cloud's Always-Free tier and DuckDNS as the worked example. Any Ubuntu 22.04 server with a public IP and a DNS name works the same way from Step 3 on. Replace `timekeeper-yourname.duckdns.org` with your own hostname throughout.

**Assumes:** you've done a basic Linux server setup before (SSH, apt, sudo). No Kubernetes or Docker required. Every command below is something you run yourself on your server or your own machine.

---

## Overview

```
Claude Desktop / Claude Code / other MCP client
        │  HTTPS
        ▼
  nginx (TLS termination, rate limits)
        │  HTTP 127.0.0.1:8765
        ▼
  timekeeper-mcp (Python, systemd service, user `timekeeper`)
```

Code is installed as immutable, root-owned releases. The service runs whatever `current` points to and cannot modify any of it:

```
/opt/timekeeper/
├── releases/
│   ├── <tag>/            one directory per installed release, owned by root:root, 0755
│   │   ├── src/          a checkout of that tag
│   │   ├── venv/         its own virtualenv (non-editable install)
│   │   ├── freeze.txt    the exact package versions installed into that venv
│   │   └── .complete     written last; only releases that have it may be activated
│   └── <older-tag>/
└── current -> releases/<tag>    symlink, replaced atomically on each upgrade or rollback
```

Upgrading means building a new release next to the old ones and switching `current`; rolling back means switching it back. Configuration (the systemd unit and the nginx site) is backed up with a timestamp before every change, so code, virtualenv and configuration can be rolled back together.

---

## Step 1: Create the Oracle Cloud VM

1. Sign in at [cloud.oracle.com](https://cloud.oracle.com) and open **Compute → Instances → Create Instance**.
2. Name it (e.g. `timekeeper`).
3. Under **Image and Shape**, click **Change Image** and select **Canonical Ubuntu 22.04**.
4. Under **Shape**, select **VM.Standard.A1.Flex** (ARM Ampere). Set **1 OCPU** and **6 GB RAM** — both are within the Always-Free tier.
5. Under **Networking**, confirm a public subnet is selected and **Assign a public IPv4 address** is checked.
6. Under **Add SSH keys**, paste your public key (or generate one and download the private key).
7. Click **Create**. Wait ~2 minutes for the instance to reach "Running."
8. Note the **public IP address** shown on the instance detail page.

### Open firewall ports in Oracle's security list

Oracle VMs have a default VCN security list that blocks everything except port 22.

1. From the instance page, click **Subnet → Security List → Add Ingress Rule** twice:
   - Source CIDR `0.0.0.0/0`, Protocol TCP, Destination Port **80**
   - Source CIDR `0.0.0.0/0`, Protocol TCP, Destination Port **443**

### Open OS-level firewall (iptables)

Ubuntu 22.04 on Oracle also runs `iptables` rules that block those ports by default:

```bash
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80  -j ACCEPT
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save
```

Port 8765 stays closed: the app listens on loopback only, and only nginx talks to it.

---

## Step 2: Configure DuckDNS

1. Sign in at [duckdns.org](https://www.duckdns.org) with GitHub or Google.
2. Create a subdomain, e.g. `timekeeper-yourname`. The full hostname will be `timekeeper-yourname.duckdns.org`.
3. Set the IP to the Oracle VM public IP from Step 1.
4. Copy your **token** from the DuckDNS dashboard (you'll need it for auto-renewal). Treat it as a secret.

Verify DNS propagates before proceeding:

```bash
dig +short timekeeper-yourname.duckdns.org
# should return your Oracle VM public IP
```

---

## Step 3: Provision the server

SSH in:

```bash
ssh -i /path/to/your/private-key ubuntu@<oracle-public-ip>
```

### Install system packages

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y nginx certbot python3-certbot-nginx python3.11 python3.11-venv git
```

### Check the clock

The server answers with this machine's clock, so the clock must be synchronized. Ubuntu keeps it in sync over NTP (with `systemd-timesyncd` or, on Oracle images, `chrony`); timekeeper-mcp only reads it.

```bash
timedatectl                 # expect "System clock synchronized: yes" and "NTP service: active"
chronyc tracking            # if chrony is installed: "Leap status: Normal" and a small "System time" offset
```

The server's location and its system timezone don't matter: the tools take an IANA timezone per request and default to UTC.

### Create the service user and the release directory

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin timekeeper
sudo install -d -o root -g root -m 0755 /opt/timekeeper /opt/timekeeper/releases
```

The `timekeeper` user owns nothing under `/opt/timekeeper`. (If you are migrating an existing install, the user already exists; see [Migrating from /opt/timekeeper-mcp](#migrating-from-opttimekeeper-mcp).)

---

## Step 4: Build a release

Pick an existing tag of the repository; releases are named after it. To list the published tags:

```bash
git ls-remote --tags https://github.com/codeiosaur/timekeeper-mcp.git
```

Set the tag and the release path (these shell variables are used again in Steps 5 and 7):

```bash
T='<tag>'                                # replace with an existing tag from the list above
R="/opt/timekeeper/releases/$T"
```

Then build the release. The commands run in a subshell that stops at the first error, and refuses to start while `T` is still the placeholder or isn't a single tag-like name (letters, digits, `.`, `_`, `-`; no leading `.` or `-`, no `..`), while `R` doesn't match `T` (for example if `T` was corrected after `R` was set), or when the release already exists. The last command writes `.complete`; Step 5 refuses to activate a release without it:

```bash
(
  set -eu
  case "$T" in   # one tag-like path component only: letters, digits, . _ -; no leading . or -, no ..
    ''|*[!A-Za-z0-9._-]*|[.-]*|*..*) echo "T must be a single tag such as v0.2.0 (letters, digits, . _ -; no '..')" >&2; exit 1 ;;
  esac
  [ "${R:-}" = "/opt/timekeeper/releases/$T" ] \
    || { echo "R must be /opt/timekeeper/releases/$T; run: R=\"/opt/timekeeper/releases/\$T\"" >&2; exit 1; }
  test ! -e "$R" || { echo "$R already exists; releases are never modified in place" >&2; exit 1; }
  sudo git clone --depth 1 --branch "$T" https://github.com/codeiosaur/timekeeper-mcp.git "$R/src"
  sudo python3.11 -m venv "$R/venv"
  sudo "$R/venv/bin/pip" install --no-cache-dir "$R/src"        # non-editable install
  sudo "$R/venv/bin/pip" freeze --all | sudo tee "$R/freeze.txt" >/dev/null
  sudo "$R/venv/bin/python" -m compileall -q "$R/venv"          # the service can't write .pyc files
  sudo "$R/venv/bin/python" -I -c 'import timekeeper'           # preflight: the package imports
  test -x "$R/venv/bin/timekeeper-mcp"
  sudo chown -R root:root "$R"
  sudo chmod -R go-w "$R"
  sudo touch "$R/.complete"                                     # last: the release is complete
  echo "release ready: $R"
)
```

If the build stops with an error, the partial release stays behind without `.complete` and is never activated. Remove it, then run the build again. This removes only an incomplete release that `current` doesn't point to, and refuses any `T` that isn't a single tag-like name, so it can never reach outside `/opt/timekeeper/releases`:

```bash
(
  set -eu
  case "$T" in   # one tag-like path component only: letters, digits, . _ -; no leading . or -, no ..
    ''|*[!A-Za-z0-9._-]*|[.-]*|*..*) echo "T must be a single tag such as v0.2.0 (letters, digits, . _ -; no '..')" >&2; exit 1 ;;
  esac
  P="/opt/timekeeper/releases/$T"
  sudo test -e "$P" || { echo "$P does not exist; nothing to remove" >&2; exit 1; }
  if sudo test -f "$P/.complete"; then echo "$P is a complete release; not removing it" >&2; exit 1; fi
  if [ "$(readlink /opt/timekeeper/current || true)" = "$P" ]; then
    echo "$P is the current release; not removing it" >&2; exit 1
  fi
  sudo rm -rf -- "$P"
  echo "removed incomplete release $P"
)
```

If the repository has no tag yet, use a full commit hash instead: name the release after it (`T='<commit>'` with the real hash), replace the `git clone` line with `sudo git clone https://github.com/codeiosaur/timekeeper-mcp.git "$R/src"` followed by `sudo git -C "$R/src" checkout --detach "$T"`, and run the rest unchanged.

`pip` resolves dependency versions from PyPI at install time; `freeze.txt` records exactly what this release got.

---

## Step 5: Install the service and activate the release

The commands below use `$R` from Step 4. In a new shell, set it again first: `R="/opt/timekeeper/releases/$T"` with `T` set to the release's tag.

### Back up the current state

Do this before every change, including the first install (the backup is then mostly empty):

```bash
B=/var/backups/timekeeper/$(date +%Y%m%d-%H%M%S)
sudo install -d -o root -g root -m 0700 "$B"
for f in /etc/systemd/system/timekeeper-mcp.service /etc/systemd/system/timekeeper-mcp.service.d \
         /etc/nginx/sites-available/timekeeper; do
  if [ -e "$f" ]; then sudo cp -a "$f" "$B/"; fi      # the .service.d directory holds any drop-ins
done
if [ -L /opt/timekeeper/current ]; then readlink /opt/timekeeper/current | sudo tee "$B/current.target"; fi
echo "backup: $B"     # note this path; rollback uses it
```

### Install the unit

```bash
sudo install -o root -g root -m 0644 "$R/src/deploy/timekeeper-mcp.service.example" \
     /etc/systemd/system/timekeeper-mcp.service
sudo sed -i 's/YOUR_DOMAIN/timekeeper-yourname.duckdns.org/g' /etc/systemd/system/timekeeper-mcp.service
sudo systemctl daemon-reload
systemctl cat timekeeper-mcp          # review: no unexpected drop-ins listed below the unit
```

Key lines in the installed unit:
- `User=timekeeper`
- `WorkingDirectory=/` (keep it: the service then has no code directory on its import path)
- `ExecStart=/opt/timekeeper/current/venv/bin/timekeeper-mcp` (matches the layout above; no edit needed)
- `Environment=MCP_ALLOWED_HOSTS=timekeeper-yourname.duckdns.org`: the public name nginx will forward as `Host`. The app answers `421` to any other `Host` that nginx forwards. This must be in place **before** nginx starts forwarding your domain (Step 7).

The unit also sandboxes the service. It is configured so that the operating system is read-only to it, home directories are hidden, it has no capabilities and cannot gain privileges, it may only send and receive IP traffic on loopback (so a compromised process should not be able to reach cloud metadata, your private network, or the internet over IP), and it cannot create Unix-domain sockets (so it should not be able to connect to host sockets such as the D-Bus system bus; `socketpair()`, which Python's asyncio uses internally, is not affected). Loopback stays reachable: the service can connect to anything else listening on `127.0.0.1` or `::1` on the VM, so don't run unauthenticated services there.

These restrictions are not yet confirmed on Linux. `scripts/verify_systemd_sandbox.sh` is intended to verify them by behavior once it is run on a disposable test VM. It creates users and system files and must only run on a disposable test VM, never on this server.

### Switch `current` to the new release and start it

The switch refuses a release whose build did not finish (no `.complete`), and the rename is atomic, so `current` only ever points at a complete release:

```bash
(
  set -eu
  case "${R:-}" in /opt/timekeeper/releases/*) ;; *) echo "Set R to the release path first" >&2; exit 1 ;; esac
  case "$R" in *'<'*) echo "R still contains a placeholder" >&2; exit 1 ;; esac
  sudo test -f "$R/.complete" \
    || { echo "$R is not a complete release (no .complete); finish or redo Step 4" >&2; exit 1; }
  sudo test -x "$R/venv/bin/timekeeper-mcp"
  sudo ln -sfn "$R" /opt/timekeeper/current.new
  sudo mv -T /opt/timekeeper/current.new /opt/timekeeper/current
  sudo systemctl enable timekeeper-mcp
  sudo systemctl restart timekeeper-mcp
  systemctl status timekeeper-mcp --no-pager
)
```

Expected: `Active: active (running)`. If not, check logs:

```bash
sudo journalctl -u timekeeper-mcp -n 50
```

Check for signs that the IP filter was not installed. `IPAddressDeny=` and `IPAddressAllow=` need cgroup v2 with BPF support (Ubuntu 22.04 has it), and systemd applies them best-effort: if it cannot compile or attach the filter it logs an error (lines starting `bpf-firewall:`) and starts the service without it. A "not supported" notice ("unit configures an IP firewall, but ...") may be logged only once per boot against another unit, or only at debug level (inside containers, for example), so search the system manager's messages for the whole boot, plus this unit's own log:

```bash
{ sudo journalctl -b _PID=1; sudo journalctl -b -u timekeeper-mcp; } | grep -i -E 'bpf-firewall|IP firewall|firewalling'
```

Any match means the IP restriction may not be active for that unit. Restricting the search to the system manager (PID 1) and this unit leaves out unrelated firewall messages from other software, such as cloud agents, ufw, or rootless containers' own user managers. No matching output is consistent with an active filter but does not prove it, and a container host may not log the problem at all. `systemctl show -p IPAddressDeny timekeeper-mcp` reports only what is configured, not whether the filter is in effect.

### Smoke-test on loopback

`scripts/smoke.py` opens one MCP session with the official SDK (about five requests): it initializes, checks the tool list, calls `get_current_time` for UTC and reports the clock skew against this machine, and checks that an invalid timezone returns an error result. Run it from the release you just installed:

```bash
/opt/timekeeper/current/venv/bin/python /opt/timekeeper/current/src/scripts/smoke.py \
    http://127.0.0.1:8765/mcp
# expect lines starting OK/INFO and finally "PASS all smoke checks"
```

---

## Step 6: Obtain a TLS certificate with certbot

Do this once, before installing the timekeeper nginx site: the site refers to the certificate files and to certbot's TLS options, so `nginx -t` fails until they exist.

```bash
sudo certbot --nginx -d timekeeper-yourname.duckdns.org
```

Certbot asks for an email address (for expiry reminders) and for agreement to the Let's Encrypt terms. It then proves domain ownership over HTTP on port 80, issues the certificate, and edits nginx's default site to use it (Step 7 replaces that site). Afterwards these must exist:

```bash
sudo ls /etc/letsencrypt/live/timekeeper-yourname.duckdns.org/fullchain.pem \
        /etc/letsencrypt/options-ssl-nginx.conf /etc/letsencrypt/ssl-dhparams.pem
```

---

## Step 7: Configure nginx

nginx will forward your public hostname to the app as `Host`, so first confirm that the running service accepts it (Step 5):

```bash
systemctl show -p Environment timekeeper-mcp     # must include MCP_ALLOWED_HOSTS=timekeeper-yourname.duckdns.org
```

Install the site from the release and replace the default site (the timekeeper config has its own catch-all `default_server`, which would conflict with the default site's):

```bash
sudo install -o root -g root -m 0644 "$R/src/deploy/nginx.conf.example" /etc/nginx/sites-available/timekeeper
sudo sed -i 's/YOUR_DOMAIN/timekeeper-yourname.duckdns.org/g' /etc/nginx/sites-available/timekeeper
sudo ln -sfn /etc/nginx/sites-available/timekeeper /etc/nginx/sites-enabled/timekeeper
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
sudo certbot renew --dry-run      # renewal works with the final config
```

If `nginx -t` fails, read the error, fix it, and run it again before reloading; nginx keeps serving the old configuration until a reload succeeds.

The config closes connections for any other `Host` (status 444), limits each client IP address to 2 requests per second (burst 20) and 10 connections (excess requests get `429`), rejects request bodies over 16 KB (`413`), hides the nginx version, and closes an upstream connection on which the app sends nothing for 30 seconds. The app sends a keep-alive ping on open event streams every 15 seconds, so healthy streams stay open; keep `proxy_read_timeout` well above that interval, because at 15 seconds nginx closes healthy streams before the ping arrives.

---

## Step 8: Verify end-to-end

From your **local machine**, in a checkout of this repository with the package installed (`pip install -e .`):

```bash
python scripts/smoke.py https://timekeeper-yourname.duckdns.org/mcp
# expect "PASS all smoke checks"; a clock-skew warning points at NTP on either machine
```

Then connect Claude Desktop. Claude Desktop does not support a bare `url` field in `claude_desktop_config.json` — it only accepts stdio servers via `command`/`args`. Use [`mcp-remote`](https://www.npmjs.com/package/mcp-remote) as a local stdio proxy:

1. Open `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows).
2. Add an entry under `mcpServers`. Pinning `mcp-remote` to the version this repository's compatibility test uses (`MCP_REMOTE_VERSION` in `tests/test_http_transport.py`) avoids surprises from new releases:

```json
"timekeeper": {
  "command": "npx",
  "args": ["-y", "mcp-remote@0.15.1", "https://timekeeper-yourname.duckdns.org/mcp"]
}
```

3. Save and relaunch Claude Desktop.
4. Open a new conversation and ask: *"What time is it in Tokyo?"*

Expected: Claude calls `get_current_time` and returns the correct time without guessing.

**Note:** Some Claude Desktop Enterprise deployments restrict the custom connector UI. The `mcp-remote` approach via the JSON config file works regardless of those restrictions, as long as `npx` is available.

**Verify failure mode:** Stop the service (`sudo systemctl stop timekeeper-mcp`), ask the same question. Claude should report a tool error, not hallucinate a time. Start it again afterwards:

```bash
sudo systemctl start timekeeper-mcp
```

---

## Upgrading to a new release

1. Build the new release (Step 4) with the new tag. The running release is not touched.
2. Back up the current state (Step 5, "Back up the current state") and note the backup path.
3. Compare the shipped unit and nginx site with what is installed, and reinstall whichever changed (Step 5 "Install the unit", Step 7 commands), with your domain substituted:
   ```bash
   diff -u /etc/systemd/system/timekeeper-mcp.service "$R/src/deploy/timekeeper-mcp.service.example"
   diff -u /etc/nginx/sites-available/timekeeper "$R/src/deploy/nginx.conf.example"
   ```
   Apart from `YOUR_DOMAIN`, any difference is a change in the new release. If the unit changed, run `sudo systemctl daemon-reload`.
4. Switch `current` and restart (Step 5, "Switch `current`..."), then run the loopback smoke test.
5. If the nginx site changed: `sudo nginx -t && sudo systemctl reload nginx`.
6. Run the end-to-end smoke test from your machine (Step 8).

Restart the service after any timezone-data update as well (for example after `apt upgrade` updates the `tzdata` package): the server loads timezone data once per process and does not pick up changes until it restarts.

## Rolling back

Roll back code, virtualenv and configuration **together**. The nginx config forwards your public `Host`, and an older release or unit (or drop-in) without the matching `MCP_ALLOWED_HOSTS` would answer `421`. Using the backup taken before the change, the block below checks everything it needs first and changes nothing if a check fails:

```bash
B='<backup directory>'     # replace with the path printed before the change, e.g. /var/backups/timekeeper/20261018-101500
(
  set -eu
  case "$B" in *'<'*|'') echo "Set B to the backup directory first" >&2; exit 1 ;; esac
  sudo test -d "$B" || { echo "$B is not a backup directory" >&2; exit 1; }
  PREV=""
  if sudo test -f "$B/current.target"; then
    PREV=$(sudo cat "$B/current.target")
    if ! sudo test -d "$PREV"; then
      echo "The release this backup points to, $PREV, no longer exists (pruned?)." >&2
      echo "Rebuild it with Step 4 (same tag), or roll back to a different backup. Nothing was changed." >&2
      exit 1
    fi
    if ! sudo test -x "$PREV/venv/bin/timekeeper-mcp" || ! sudo test -f "$PREV/.complete"; then
      echo "The release this backup points to, $PREV, is incomplete (no entry point or no .complete)." >&2
      echo "Remove it and rebuild it with Step 4 (same tag) until the build finishes, then verify" >&2
      echo "that $PREV/.complete exists, or roll back to a different backup. Nothing was changed." >&2
      exit 1
    fi
  fi
  if [ -n "$PREV" ]; then
    sudo ln -sfn "$PREV" /opt/timekeeper/current.new
    sudo mv -T /opt/timekeeper/current.new /opt/timekeeper/current
  fi
  if sudo test -f "$B/timekeeper-mcp.service"; then
    sudo install -o root -g root -m 0644 "$B/timekeeper-mcp.service" /etc/systemd/system/timekeeper-mcp.service
  fi
  if sudo test -d "$B/timekeeper-mcp.service.d"; then    # the drop-ins as they were before the change
    sudo rm -rf -- /etc/systemd/system/timekeeper-mcp.service.d
    sudo cp -a "$B/timekeeper-mcp.service.d" /etc/systemd/system/
  fi
  if sudo test -f "$B/timekeeper"; then
    sudo install -o root -g root -m 0644 "$B/timekeeper" /etc/nginx/sites-available/timekeeper
  fi
  sudo systemctl daemon-reload
  sudo systemctl restart timekeeper-mcp
  sudo nginx -t
  sudo systemctl reload nginx
)
```

Then run the loopback and end-to-end smoke tests again. If the backup has no `timekeeper-mcp.service.d` (there were no drop-ins then), drop-ins added since are left in place; check `systemctl cat timekeeper-mcp` and remove any you no longer want.

## Migrating from /opt/timekeeper-mcp

Earlier versions of this guide installed the code into `/opt/timekeeper-mcp`, owned by the `timekeeper` user, as an editable install that was updated with `git pull`. That procedure is retired: the service user should never be able to change the code it runs. To migrate:

1. Leave `/opt/timekeeper-mcp` exactly as it is. It is the rollback target.
2. Create `/opt/timekeeper/releases` (Step 3; the user already exists), then build a release (Step 4).
3. Back up (Step 5). This backup holds the old unit, which points at `/opt/timekeeper-mcp`, any drop-ins under `/etc/systemd/system/timekeeper-mcp.service.d/`, and the old nginx site; there is no `current.target` yet.
4. Check for drop-ins: `systemctl cat timekeeper-mcp` lists any files under `/etc/systemd/system/timekeeper-mcp.service.d/`, for example overrides you added with `sudo systemctl edit timekeeper-mcp`. They keep overriding the new unit. Remove or update them deliberately; the backup from step 3 keeps a copy.
5. Install the unit, switch `current`, restart, and smoke-test on loopback (Step 5), then install the nginx site (Step 7) and verify (Step 8).

Rollback for this first migration uses the rollback commands above: they restore the old unit, the drop-ins and the old nginx site from the backup, and skip the `current` switch because the backup has no `current.target`. That puts the service back on `/opt/timekeeper-mcp`. Remove `/opt/timekeeper-mcp` only once you no longer need that rollback.

## Pruning old releases

Keep the last three releases. List them newest first and check which one is current:

```bash
ls -1t /opt/timekeeper/releases
readlink /opt/timekeeper/current
```

Remove older releases one at a time, never the current one and never one that a backup you might still roll back to names in its `current.target`:

```bash
sudo rm -rf -- "/opt/timekeeper/releases/<old-tag>"
```

---

## Logs and retention

- **The service** logs to the journal: startup messages and one line per HTTP request (method, path, status, and the client address). Request bodies and tool arguments are not logged. Read them with `sudo journalctl -u timekeeper-mcp`.
- **nginx** writes access and error logs under `/var/log/nginx/`, including client IP addresses.
- **Retention:** on Ubuntu, nginx logs are rotated by `/etc/logrotate.d/nginx` (check its `rotate` and `daily`/`weekly` lines), and the journal keeps entries until its size limits are reached. Check `journalctl --disk-usage`, and set `MaxRetentionSec=` or `SystemMaxUse=` in `/etc/systemd/journald.conf` if you want a time or size limit. Decide how long you need client IP addresses and set these accordingly.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `502 Bad Gateway` from nginx | The service is not running | `systemctl status timekeeper-mcp`, then `sudo journalctl -u timekeeper-mcp -n 50` |
| `421 Misdirected Request` | `MCP_ALLOWED_HOSTS` doesn't name the domain nginx forwards | Fix the unit (Step 5), `daemon-reload`, restart |
| `nginx -t` fails on a certificate or `options-ssl-nginx.conf` path | The certificate wasn't obtained yet | Step 6, then retry |
| `nginx -t` reports a duplicate `default_server` | The default site is still enabled | `sudo rm -f /etc/nginx/sites-enabled/default` |
| `429 Too Many Requests` | The per-IP rate or connection limit | Expected under bursts; wait and retry |
| TLS cert error in browser | Cert not yet issued or domain mismatch | Re-run Step 6 for the right domain |
| `curl` hangs on port 80/443 | Oracle or OS firewall blocking | Re-check the Step 1 firewall rules |
| Tools don't appear in Claude Desktop | Wrong URL or protocol | Confirm the URL ends in `/mcp` and uses `https://`; run `scripts/smoke.py` against it |
| Smoke test warns about clock skew | NTP not synchronized on the server or your machine | `timedatectl` on both |
| `dig` returns wrong IP | DuckDNS not updated | Log into duckdns.org and re-set the IP |

---

## DuckDNS IP auto-update (optional but recommended)

Oracle VMs keep their public IP across reboots, but if you ever resize/recreate the instance the IP changes. A cron job keeps DuckDNS in sync:

```bash
crontab -e
```

Add:

```
*/5 * * * * curl -s "https://www.duckdns.org/update?domains=timekeeper-yourname&token=YOUR_TOKEN&ip=" > /dev/null
```

Replace `timekeeper-yourname` and `YOUR_TOKEN` with your values. The crontab then contains your DuckDNS token; keep it out of anything you share.
