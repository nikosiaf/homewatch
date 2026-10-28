# homewatch

A small, self-hosted monitor for your home network, configured from a plain web page.
v0.1 watches one thing well: **your internet line**. It tells you *what* broke ("router
unreachable", "line down", "DNS failing", "slow line") and sends alerts to Telegram or ntfy.
Later versions add one module per release (Proxmox, OPNsense, Home Assistant, …).

- Runs as a Docker Compose stack on any Linux host (amd64 or arm64)
- LAN / Tailscale only: it never needs a port open to the internet
- MIT licensed

## What v0.1 does

| Probe | Default | Checks |
|---|---|---|
| Public ping | 1.1.1.1 and 8.8.8.8, every 30 s | Internet reachable, packet loss, latency, jitter |
| Gateway ping | your router, every 30 s | Gateway reachable |
| DNS lookup | `cloudflare.com` via your router, every 60 s | DNS failing / slow |
| Speed test | Cloudflare (or Ookla CLI), every 3 h | Line speed against your contract |

Every probe can be switched off, and every threshold changed, in the web page.

| Check | WARNING | ERROR |
|---|---|---|
| Internet reachable | | all public targets at 100 % loss |
| Packet loss (5-min average) | ≥ 10 % | ≥ 20 % |
| Latency (5-min average) | ≥ 100 ms | |
| Jitter (10-min average) | ≥ 15 ms | ≥ 30 ms |
| Gateway reachable | | 100 % loss |
| DNS | ≥ 500 ms | lookups fail while ping works |
| Line speed | < 70 % of contract on 2 tests in a row | |
| Probe data fresh | | no data for > 3 min |

Alerting works like this:
- **Confirmation:** a change must hold for 2 runs (about 4 minutes) before it's sent.
- **Reminders:** open problems are repeated every 12 h.
- **Offline queue:** messages that can't be sent while the line is down go out when it's back, with the outage times.
- **Snooze and maintenance windows:** they hold notifications without losing the history.
- **Weekly report:** optional, switched on per channel.
- **healthchecks.io heartbeat:** optional. It tells you when homewatch itself goes quiet.

There are two account roles: **admin**, who can change everything, and **viewer**, who can only see
status and history.

## Install

You need a Linux host with Docker Engine and the Compose plugin
(2 CPU, 2 GB RAM and 10 GB disk are plenty).

```sh
mkdir homewatch && cd homewatch
# download docker-compose.yml, Caddyfile, .env.example and install.sh from the latest release
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
cp .env.example .env && chmod 600 .env
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

Keep a copy of `.env`. Without `HW_MASTER_KEY` the saved tokens can't be decrypted, so you would
enter them again.

## Ookla speed test (optional)

The Ookla Speedtest CLI can't be bundled with an MIT package, so you add it yourself:
1. Download the Linux build from <https://www.speedtest.net/apps/cli>.
2. Copy it into the container: `docker compose cp speedtest controller:/data/ookla/speedtest`.
3. On the Internet page, pick "Ookla" and accept its license.

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
