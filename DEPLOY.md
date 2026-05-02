# DEPLOY.md — Deploying timekeeper-mcp to Oracle Cloud

This guide gets `timekeeper-mcp` running at `https://timekeeper.<your-subdomain>.duckdns.org/mcp` on an Oracle Cloud Always-Free ARM VM with a valid TLS certificate.

**Assumes:** You've done a basic Linux server setup before (SSH, apt, sudo). No Kubernetes or Docker required.

---

## Overview

```
Claude Desktop / Claude Code
        │  HTTPS
        ▼
  nginx (TLS termination)
        │  HTTP 127.0.0.1:8765
        ▼
  timekeeper-mcp (Python, systemd)
```

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

---

## Step 2: Configure DuckDNS

1. Sign in at [duckdns.org](https://www.duckdns.org) with GitHub or Google.
2. Create a subdomain, e.g. `timekeeper-yourname`. The full hostname will be `timekeeper-yourname.duckdns.org`.
3. Set the IP to the Oracle VM public IP from Step 1.
4. Copy your **token** from the DuckDNS dashboard (you'll need it for auto-renewal).

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

### Create a service user

```bash
sudo useradd -r -s /sbin/nologin timekeeper
```

### Clone and install timekeeper-mcp

```bash
sudo mkdir -p /opt/timekeeper-mcp
sudo chown timekeeper:timekeeper /opt/timekeeper-mcp
sudo -u timekeeper git clone https://github.com/codeiosaur/timekeeper-mcp.git /opt/timekeeper-mcp
sudo -u timekeeper python3.11 -m venv /opt/timekeeper-mcp/.venv
sudo -u timekeeper /opt/timekeeper-mcp/.venv/bin/pip install -e /opt/timekeeper-mcp
```

---

## Step 4: Install the systemd service

```bash
sudo cp /opt/timekeeper-mcp/deploy/timekeeper-mcp.service.example \
        /etc/systemd/system/timekeeper-mcp.service
```

Open the file and verify the paths match your setup:

```bash
sudo nano /etc/systemd/system/timekeeper-mcp.service
```

Key lines to confirm:
- `User=timekeeper`
- `WorkingDirectory=/opt/timekeeper-mcp`
- `ExecStart=/opt/timekeeper-mcp/.venv/bin/python -m timekeeper`

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now timekeeper-mcp
sudo systemctl status timekeeper-mcp
```

Expected: `Active: active (running)`. If not, check logs:

```bash
sudo journalctl -u timekeeper-mcp -n 50
```

Quick smoke test (from the VM):

```bash
curl -s http://127.0.0.1:8765/mcp
# Should return an HTTP response (likely 405 or 400 — that's fine; it's the MCP endpoint)
```

---

## Step 5: Obtain a TLS certificate with certbot

```bash
sudo certbot --nginx -d timekeeper-yourname.duckdns.org
```

Certbot will ask for an email address (for expiry reminders) and prompt you to agree to the Let's Encrypt TOS. Answer the prompts, and it will:
1. Verify domain ownership via HTTP-01 challenge.
2. Issue the certificate.
3. Automatically edit `/etc/nginx/sites-enabled/default` to add the HTTPS block.

Verify auto-renewal works:

```bash
sudo certbot renew --dry-run
# Should report "Congratulations, all renewals succeeded"
```

---

## Step 6: Configure nginx

Remove the certbot-generated default config and replace it with the timekeeper config:

```bash
sudo cp /opt/timekeeper-mcp/deploy/nginx.conf.example \
        /etc/nginx/sites-available/timekeeper
sudo ln -s /etc/nginx/sites-available/timekeeper /etc/nginx/sites-enabled/timekeeper
sudo rm -f /etc/nginx/sites-enabled/default
```

Edit the config to set your domain:

```bash
sudo nano /etc/nginx/sites-available/timekeeper
# Replace every occurrence of YOUR_DOMAIN with timekeeper-yourname.duckdns.org
```

Test and reload:

```bash
sudo nginx -t
# "syntax is ok" and "test is successful" — if not, re-read the error and fix it
sudo systemctl reload nginx
```

---

## Step 7: Verify end-to-end

From your **local machine**:

```bash
curl -s https://timekeeper-yourname.duckdns.org/mcp
# Should return an HTTP response (not a TLS error, not a 502)
```

Then connect Claude Desktop:

Claude Desktop does not support a bare `url` field in `claude_desktop_config.json` — it only accepts stdio servers via `command`/`args`. Use [`mcp-remote`](https://www.npmjs.com/package/mcp-remote) as a local stdio proxy:

1. Open `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows).
2. Add an entry under `mcpServers`:

```json
"timekeeper": {
  "command": "npx",
  "args": ["-y", "mcp-remote", "https://timekeeper-yourname.duckdns.org/mcp"]
}
```

3. Save and relaunch Claude Desktop.
4. Open a new conversation and ask: *"What time is it in Tokyo?"*

Expected: Claude calls `get_current_time` and returns the correct time without guessing.

**Note:** Some Claude Desktop Enterprise deployments restrict the custom connector UI. The `mcp-remote` approach via the JSON config file works regardless of those restrictions, as long as `npx` is available.

**Verify failure mode:** Stop the service (`sudo systemctl stop timekeeper-mcp`), ask the same question. Claude should report a tool error, not hallucinate a time.

```bash
sudo systemctl start timekeeper-mcp   # bring it back up when done
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `502 Bad Gateway` from nginx | Python process not running | `sudo systemctl status timekeeper-mcp` |
| TLS cert error in browser | Cert not yet issued or domain mismatch | Re-run `certbot --nginx -d YOUR_DOMAIN` |
| `curl` hangs on port 80/443 | Oracle or OS firewall blocking | Re-check Step 1 firewall rules |
| Tools don't appear in Claude Desktop | Wrong URL or protocol | Confirm URL ends in `/mcp` and uses `https://` |
| `dig` returns wrong IP | DuckDNS not updated | Log into duckdns.org and re-set the IP |

---

## Keeping the server up to date

```bash
sudo -u timekeeper git -C /opt/timekeeper-mcp pull
sudo -u timekeeper /opt/timekeeper-mcp/.venv/bin/pip install -e /opt/timekeeper-mcp
sudo systemctl restart timekeeper-mcp
```

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

Replace `timekeeper-yourname` and `YOUR_TOKEN` with your values.
