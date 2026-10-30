<h1>
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/brand/red-homewatch-logo-dark.svg">
    <img alt="homewatch" src="docs/brand/red-homewatch-logo.svg" width="330">
  </picture>
</h1>

A small, self-hosted monitor for your home network, configured from a plain web page.
homewatch watches **your internet line**, **your provider's DSL modem**, **your Proxmox VE host**,
**your OPNsense firewall**, **Home Assistant**, **your Synology NAS**, **your MikroTik switches**, **UniFi**, **Proxmox Backup Server** and **your UPS**. It tells you *what* broke ("router unreachable", "line down", "DNS failing", "DSL link
down", "guest stopped", "backup failed") and sends alerts to Telegram or ntfy.

- Runs as a Docker Compose stack on any Linux host (amd64 or arm64)
- LAN / Tailscale only: it never needs a port open to the internet
- MIT licensed

## What homewatch does

### Internet line module

| Probe | Default | Checks |
|---|---|---|
| Public ping | 1.1.1.1 and 8.8.8.8, every 30 s | Internet reachable, packet loss, latency, jitter |
| Gateway ping | your router, every 30 s | Gateway reachable |
| DNS lookup | `cloudflare.com` via your router, every 60 s | DNS failing / slow |
| Speed test | Cloudflare (or Ookla CLI), every 6 h, off by default | Line speed against your contract |

Every probe can be switched off, and every threshold changed, in the web page.

| Check | WARNING | ERROR |
|---|---|---|
| Internet reachable | | all public targets at 100 % loss |
| Packet loss (5-min average) | ≥ 25 % | ≥ 50 % |
| Latency (5-min median) | ≥ 200 ms | |
| Jitter (10-min median) | ≥ 50 ms | ≥ 100 ms |
| Gateway reachable | | 100 % loss |
| DNS | ≥ 500 ms | lookups fail while ping works |
| Line speed | < 50 % of contract on 2 tests in a row | optional level, e.g. < 30 % |
| Speed test recent | no successful test for 7 h | |
| Speed test latency | > 40 ms on 2 tests in a row | |
| Probe data fresh | | no data for > 3 min |

### ISP modem module (optional)

Reads the DSL line from your provider's modem every 15 minutes. Read-only: nothing on the modem
is changed. Supported model:

- **Telekom Speedport Plus 2** (Sercomm VD4224BDTP / VD4224BDT), the modem-router TELEKOM
  (formerly COSMOTE) provides for VDSL and FTTH connections in Greece.

| Check | WARNING | ERROR |
|---|---|---|
| Modem readable | | can't log in or read for 2.5 read intervals |
| DSL link | | the modem has no sync |
| Sync rate (share of the contracted speed) | < 85 % | < 50 % |
| SNR margin (lower of down and up) | < 6 dB | < 3 dB |
| Resyncs in 24 h | ≥ 1 | ≥ 5 |
| PPP session on the modem | | down (off by default; for router-mode setups) |

Things to know before you add a modem:
- **Password:** the Speedport has only its admin login, so homewatch stores that password
  (encrypted, never shown again).
- **Sessions:** each reading logs in, which ends any open browser session on the modem.
- **Rejected logins:** after a rejected login homewatch waits 6 hours, because the modem slows
  down further attempts.
- **Certificate:** the modem's certificate is self-signed, so homewatch trusts the one it sees at
  the first reading and logs in only to that one. If it changes, the check goes to ERROR and
  homewatch stops logging in until you accept the new one on the module page. The first reading
  itself can't tell an impostor from the modem, so add the modem on a network you trust.

### Proxmox VE module (optional)

Watches a Proxmox VE host or cluster through a **read-only API token** (role PVEAuditor; the module
page shows the three commands to create it). homewatch tests the token before saving anything.

| Check | WARNING | ERROR |
|---|---|---|
| API reachable | | can't connect, or the token is rejected |
| Node online | | a node is offline |
| Node CPU / memory / root disk | ≥ 85 / 90 / 85 % | ≥ 95 % |
| Guests running | | a guest you chose to watch is not running |
| Storage | ≥ 85 % full | ≥ 95 % full, or a storage unavailable |
| Disk health | SSD life left < 20 % | SMART not PASSED, or life left < 10 % |
| Backups (watched guests in a backup job) | newest > 26 h old, or none yet | newest > 50 h old, or the last attempt failed |

Backup results come from Proxmox's backup task logs (a read-only token can't list backup contents);
each log is read once.

### OPNsense module (optional)

Reads an OPNsense firewall through the API key of a user that has only the status pages it needs
(the module page lists them). Missing privileges show as "not readable", not as alarms.

| Check | WARNING | ERROR |
|---|---|---|
| Gateways | packet loss or delay | a gateway down |
| CPU (10-minute average) / memory (without ZFS cache) / root disk | ≥ 85 / 90 / 85 % | ≥ 95 % |
| Services (the ones you choose) | | not running |
| HAProxy servers | | a health-checked server down |
| Certificates in use | expires in < 20 days | < 7 days, or expired |
| Firmware | updates found at the last check, or reboot needed | |

### Home Assistant module (optional)

Reads Home Assistant with the long-lived token of a dedicated **non-admin** user.

| Check | WARNING | ERROR |
|---|---|---|
| Core | starting, stopping, or safe / recovery mode | |
| Unavailable entities (30 min or longer, not ignored) | ≥ 1 | ≥ 10 |
| Updates (skipped versions don't count) | updates available | |
| Automatic backups | last success > 26 h ago | > 50 h ago, or the last attempt failed |
| Alarm panels | unavailable | triggered |
| Safety sensors (smoke, water / moisture, gas, CO, heat, safety) | | detecting |
| Tamper sensors | | tampered |
| Batteries (lowest battery sensor, %) | < 25 % | < 10 % |

The alarm, safety, tamper and battery checks find their sensors by Home Assistant's device class
and follow the "Entities to watch" list: untick a phone there to keep its battery out of the check.

Entities that are unavailable when you connect are ignored (you can change the list), so only
entities that *become* unavailable are reported.

### Synology NAS module (optional)

Reads DSM through its Web API every 10 minutes (log in, read, log out). DSM has no read-only
administrator role, so the setup asks for a separate `homewatch` administrator with no access to
shared folders or applications.

| Check | WARNING | ERROR |
|---|---|---|
| System | ≥ 60 °C, or DSM's temperature warning | ≥ 70 °C, or a crashed system |
| CPU / memory | ≥ 85 / 90 % | ≥ 95 % |
| Disks | hottest ≥ 50 °C | ≥ 60 °C, a disk not normal, SMART failing or bad sectors |
| Pools and volumes | fullest volume ≥ 85 %, or repairing / scrubbing | ≥ 95 %, degraded or crashed |
| DSM updates | update available | |

### MikroTik module (optional)

Reads one or more RouterOS 7 devices over the REST API with a user in the `read` group (the same
user on every device). The device's `www` service must allow the homewatch host.

| Check | WARNING | ERROR |
|---|---|---|
| Every device read | | one of several devices can't be read |
| CPU / memory / temperature | ≥ 85 % / 90 % / 60 °C | ≥ 95 % / 95 % / 70 °C |
| Ports up (the ones you choose) | | a chosen port down |
| Port errors in the last hour | ≥ 10 | ≥ 1000 |
| Uptime | rebooted in the last 60 min | |
| RouterBOOT | older than the installed RouterOS | |

### UniFi module (optional)

Reads the devices of a UniFi Network application (UniFi OS Server or a UniFi console) as a local
**view-only** user: log in, read, log out.

| Check | WARNING | ERROR |
|---|---|---|
| Devices online (the ones you choose) | upgrading or provisioning | disconnected, heartbeat missed |
| CPU (busiest watched device) | ≥ 90 % | |
| Wi-Fi | WLAN subsystem not OK | |
| Uptime | restarted in the last 60 min | |
| Firmware | update available | |

### Proxmox Backup Server module (optional)

Reads PBS every 5 minutes through an API token with the read-only **Audit** role (the module page
shows the commands to create it).

| Check | WARNING | ERROR |
|---|---|---|
| Datastores | fullest ≥ 85 % | ≥ 95 %, or a datastore error (e.g. not mounted) |
| Backups (the groups you choose) | newest snapshot > 26 h old | > 50 h old, or no snapshots |
| Verify, prune, garbage collection, sync | latest run had warnings | latest run failed (last 8 days) |
| Updates | package updates available | |

### UPS module (optional)

Reads a UPS from a **NUT server** (Network UPS Tools), for example on your Proxmox host or NAS.
Reading needs no login; the NUT server must listen on the network.

| Check | WARNING | ERROR |
|---|---|---|
| NUT server reachable | | unreachable, or the UPS data is stale |
| Mains power | | on battery, or a forced shutdown under way |
| Battery charge | < 50 % | < 30 % |
| Runtime | < 10 min | < 5 min |
| Load | ≥ 80 % | ≥ 95 % |
| UPS health | replace battery | overload |

### Alerts and notifications

Notification providers: **Telegram**, **ntfy** (one topic URL), **email** (any SMTP server; port
465 = TLS, other ports STARTTLS, a password is never sent unencrypted), **webhook** and the
**healthchecks.io heartbeat**.

Alerting works like this:
- **Confirmation:** a change must hold for 2 runs (about 4 minutes) before it's sent.
- **Reminders:** open problems are repeated every 12 h.
- **Offline queue:** messages that can't be sent while the line is down go out when it's back, with the outage times.
- **Snooze and maintenance windows:** they hold notifications without losing the history.
- **Quiet hours** per channel: warnings wait until they end, errors are always sent.
- **Weekly report:** optional; pick the day, the time and the channels.
- **healthchecks.io heartbeat:** optional. homewatch pings it after every run, and healthchecks.io
  alerts you when the pings stop (host, power or internet down).
- **History** is a timeline of every problem and its return to OK.

**Configuration versions** (System page): every saved change to modules, thresholds, timing, channels
or maintenance windows is kept as a version (last 200), showing who changed what. Each can be viewed
and restored; a restore is itself a version, so it can be undone. Passwords, tokens and users are
never part of a version.

### Webhook

The webhook channel POSTs every message as JSON to the URL you enter. The URL is stored encrypted
like a password (webhook URLs usually contain a token) and is never part of an export:

```json
{"text": "🔴 Home: ERROR\n\n…", "content": "🔴 Home: ERROR\n\n…", "title": "🔴 Home: ERROR",
 "body": "…", "level": 2, "level_name": "ERROR", "sent": 1793260000}
```

`text` makes it work as-is with Slack, Mattermost and Rocket.Chat incoming webhooks, `content` with
Discord (cut to 2000 characters). For Home Assistant, Node-RED or n8n use `level` / `level_name`,
`title` and `body`.

There are two account roles: **admin**, who can change everything, and **viewer**, who can only see
status and history.

## Install

You need a Linux host with Docker Engine and the Compose plugin
(2 CPU, 2 GB RAM and 10 GB disk are plenty).

```sh
mkdir homewatch && cd homewatch
# download docker-compose.yml, Caddyfile, env.example and install.sh from the latest release
# https://github.com/nikosiaf/homewatch/releases
chmod +x install.sh
less install.sh          # please read it first; it's short
./install.sh
```

`install.sh` does three things:
- writes `.env` with a new master key, the host's time zone, and its gateway and DNS server as defaults
- creates a self-signed certificate in `certs/`
- runs `docker compose up -d`

At the end it prints the certificate's SHA-256 fingerprint. Open `https://<host>` and compare that
fingerprint when the browser warns about the certificate (once). Then follow the setup wizard:
1. Admin account
2. Home name and time zone
3. Internet probes and contracted speed
4. A notification channel
5. Heartbeat

### Manual install (without install.sh)

```sh
cp env.example .env && chmod 600 .env    # .env.example in a git checkout
openssl rand -base64 32          # put it in .env as HW_MASTER_KEY=…; also set TZ
mkdir certs && openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 3650 \
  -keyout certs/key.pem -out certs/cert.pem -subj "/CN=homewatch" -addext "subjectAltName=IP:<host ip>"
docker compose up -d
```

## Update

Set `HW_VERSION` in `.env` to the new release, then run:

```sh
docker compose pull && docker compose up -d
```

Database changes apply on start. The System page tells you when a newer release exists (you can
switch that check off there).

## Back up

Everything homewatch knows lives in two Docker volumes:
- `homewatch_hw-data`: settings, users, history, encrypted tokens
- `homewatch_vm-data`: metrics

**Settings export:** System → Export and import downloads all settings as one YAML file (modules,
thresholds, timing, channels, maintenance windows; no passwords, tokens or users). homewatch also
writes one every night into the data volume (`exports/`, last 14 kept). Importing a file replaces
the settings; the previous ones stay under Configuration versions, so an import can be undone.

Keep a copy of `.env`. Without `HW_MASTER_KEY` the saved tokens can't be decrypted, so you would
enter them again.

## Ookla speed test (optional)

The default speed test uses Cloudflare and needs nothing extra. To use Ookla instead:
1. Go to **Modules → Internet line** and pick **Ookla Speedtest CLI** as the engine.
2. Accept Ookla's EULA and privacy policy.
3. Press **Save and install Ookla CLI**.

homewatch then downloads the official CLI from speedtest.net for your processor (x86_64, arm64 or
armhf) into its data volume. It isn't bundled with homewatch, because Ookla's license doesn't
allow redistribution. **System → Health** shows the installed version.

## Security notes

- **Exposure:** don't forward homewatch to the internet. Use it on your LAN or over Tailscale.
- **Network:** only Caddy publishes ports (80 redirects to 443). VictoriaMetrics, Telegraf and the
  controller stay on the internal Docker network.
- **Secrets:** tokens and URLs are encrypted at rest (AES-GCM, key from `.env`) and never shown
  again after saving.
- **Logins:** passwords are hashed with Argon2. Sessions use HttpOnly, Secure, SameSite=Strict
  cookies. Every form is CSRF-protected, and logins lock for 5 minutes after 5 failures.
- **Containers:** image versions are pinned, and the controller runs as a non-root user on a
  read-only filesystem.

## How it fits together

```
browser ──HTTPS──▶ caddy ──▶ controller (FastAPI: UI, checks every 120 s, notifier, speed test)
                                 │  ▲                        │
                     SQLite ◀────┘  │ reads              Telegram / ntfy / healthchecks.io
                                    │
telegraf (ping, DNS) ──writes──▶ victoriametrics
```

## Development

```sh
cd controller && pip install -r requirements-dev.txt && python -m pytest -q
cd deploy && ./install.sh --dev      # builds the controller from source
```

## License

MIT, see [LICENSE](LICENSE).
