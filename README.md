<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/brand/red-homewatch-logo-dark.svg">
    <img alt="homeWatch" src="docs/brand/red-homewatch-logo.svg" width="347">
  </picture>
</p>

<p align="center">
  <a href="https://github.com/nikosiaf/homewatch/releases/latest"><img src="https://img.shields.io/github/v/release/nikosiaf/homewatch?style=flat-square&label=release" alt="Latest release"></a>
  <a href="https://github.com/nikosiaf/homewatch/actions/workflows/test.yml"><img src="https://img.shields.io/github/actions/workflow/status/nikosiaf/homewatch/test.yml?branch=main&style=flat-square&label=tests" alt="Tests"></a>
  <img src="https://img.shields.io/badge/arch-amd64%20%7C%20arm64-6b7280?style=flat-square" alt="amd64 and arm64">
  <a href="LICENSE"><img src="https://img.shields.io/github/license/nikosiaf/homewatch?style=flat-square" alt="MIT license"></a>
</p>

<p align="center">
  A small, self-hosted monitor for your home network, configured from a plain web page.<br>
  It tells you <em>what</em> broke, groups problems into one incident with its likely cause, and alerts you where you are.
</p>

![homeWatch's Status page: the overall status, today's key numbers and one tile per module with its 24-hour line](docs/screenshots/status.png)

> [!NOTE]
> **Built with AI, maintained by a human.** homeWatch's code, tests and documentation are written by Claude (Anthropic). A human maintainer decides what homeWatch does and how it behaves, runs every release on a real home network (Proxmox, OPNsense, MikroTik, UniFi, Synology, Home Assistant, Docker hosts) before publishing it, and makes all release decisions. Still, read the setup steps before you give it access to your devices.

> [!IMPORTANT]
> **Read-only access, LAN or Tailscale only.** homeWatch only reads: give it a separate read-only account, token or proxy on each system it watches, never your own admin login. Where a device has no read-only role (Synology DSM, the ISP modem), the module's setup steps say what to create instead. homeWatch never needs a port open to the internet, so don't forward it.

## Why homeWatch?

When something at home breaks, the question is rarely "which graph is red?" but "what happened, and is it
me or the provider?". homeWatch watches your internet line and the devices behind it, and answers in plain
words: "line down", "DNS failing", "guest stopped", "backup failed". Problems that start together become one
incident, and when it's over homeWatch names the likely cause: a power cut, a modem restart, a line or ISP
problem, one of your own devices. Updates go to a To do list instead of raising alarms. Everything runs on
one small Docker host, and nothing on your devices is ever changed.

## Features

- **Plain-language checks** - every module has checks with WARNING and ERROR limits you can change, and a "what to do" line per check.
- **Incidents and likely cause** - problems within 30 minutes are one incident with a timeline; device events (reboots, DSL resyncs, power on battery) explain it.
- **Read-only by design** - every module reads through a read-only account, token or proxy; passwords and tokens are encrypted and never shown again.
- **Eleven modules** - internet line, ISP modem, Proxmox VE, OPNsense, Home Assistant, Synology, MikroTik, UniFi, Proxmox Backup Server, UPS (NUT) and Docker hosts.
- **Network map** - how your devices are connected, found from what the modules read, with root causes and **new devices** on the network.
- **To do, not alarms** - updates and firmware waiting on your devices are listed, and only become a warning after 14 days.
- **Alerts where you are** - Telegram, ntfy (with snooze / acknowledge buttons), email, webhook, quiet hours, an offline queue, a weekly report and a healthchecks.io heartbeat.
- **Grafana dashboards** (optional) - an Overview, Health and one dashboard per module, a year of history, incident markers, behind your homeWatch login.
- **Home Assistant and API** - homeWatch as an MQTT device in Home Assistant, plus a read-only JSON API with tokens.
- **Set up from a web page** - a setup wizard, configuration versions you can restore, export / import, and a one-click update that rolls back on failure.

## Modules

| Module | What it watches | Access it needs |
|---|---|---|
| [Internet line](docs/modules.md#internet-line-module) | reachability, loss, latency, jitter, DNS, speed against your contract | none |
| [ISP modem](docs/modules.md#isp-modem-module-optional) | DSL link, sync rate, noise margin, resyncs, PPP session | the modem's login (Telekom Speedport Plus 2) |
| [Proxmox VE](docs/modules.md#proxmox-ve-module-optional) | nodes, guests, storage, disk health, backups | API token, role PVEAuditor |
| [OPNsense](docs/modules.md#opnsense-module-optional) | gateways, CPU, services, HAProxy, certificates, firmware | API key with status pages only |
| [Home Assistant](docs/modules.md#home-assistant-module-optional) | core, updates, backups, host load, the entities you choose | non-admin user's token |
| [Synology NAS](docs/modules.md#synology-nas-module-optional) | system, disks, pools and volumes, DSM updates, fans (SNMPv3) | DSM user without folder or app access |
| [MikroTik](docs/modules.md#mikrotik-module-optional) | ports, port errors, CPU, reboots, RouterOS, daily config copies | user in the `read` group |
| [UniFi](docs/modules.md#unifi-module-optional) | devices online, CPU, Wi-Fi, restarts, firmware | view-only user |
| [Proxmox Backup Server](docs/modules.md#proxmox-backup-server-module-optional) | datastores, backups, verify / prune / GC, updates | API token, role Audit |
| [UPS](docs/modules.md#ups-module-optional) | mains power, battery, runtime, load | a NUT server on the network |
| [Docker hosts](docs/modules.md#docker-hosts-module-optional) | containers, crash loops, health, OS updates | read-only socket proxy, optional node_exporter |

## Screenshots

<table>
  <tr>
    <td width="50%"><img src="docs/screenshots/grafana-overview.png" alt="Grafana Overview: problems now, a status tile per module, checks needing attention, WAN traffic and the internet line's numbers"></td>
    <td width="50%"><img src="docs/screenshots/grafana-internet.png" alt="Grafana Internet dashboard: latency, jitter, packet loss and DNS lookup time"></td>
  </tr>
  <tr>
    <td><img src="docs/screenshots/grafana-synology.png" alt="Grafana Synology dashboard: temperature, CPU, memory, volumes, disk health and temperatures"></td>
    <td><img src="docs/screenshots/grafana-ups.png" alt="Grafana UPS dashboard: status, battery, runtime, load, power draw and voltages"></td>
  </tr>
</table>

## Install

You need a Linux host (amd64 or arm64) with Docker Engine and the Compose plugin; 2 CPU, 2 GB RAM and
10 GB disk are plenty (add 2 GB of disk for Grafana).

```sh
mkdir homewatch && cd homewatch
for f in docker-compose.yml Caddyfile env.example install.sh update.sh; do
  curl -fsSLO "https://github.com/nikosiaf/homewatch/releases/latest/download/$f"
done
chmod +x install.sh update.sh
less install.sh
./install.sh
```

Please read `install.sh` before running it; it's short. Then open `https://<host>:5443`, compare the
certificate fingerprint `install.sh` printed, create the admin account right away and follow the setup
wizard. Ports, the wizard's steps, a manual install and reverse proxies: [docs/install.md](docs/install.md).

## Documentation

- [Install](docs/install.md)
- [Modules](docs/modules.md): checks and limits of every module, read intervals, Ookla speed test
- [Map](docs/map.md)
- [Alerts and notifications](docs/alerts.md)
- [Grafana dashboards](docs/grafana.md)
- [API and Home Assistant (MQTT)](docs/api-and-mqtt.md)
- [Update and back up](docs/update-and-backup.md)
- [Security notes](docs/security.md)

## How it fits together

```
browser ──HTTPS──▶ caddy ──▶ controller (FastAPI: UI, checks every 120 s, map, notifier, speed test)
                                 │  ▲                        │
                     SQLite ◀────┘  │ reads              Telegram / ntfy / email / webhook /
                                    │                     healthchecks.io / MQTT → Home Assistant
                                    │
telegraf (ping, DNS) ──writes──▶ victoriametrics ◀──reads── grafana (optional, /grafana/ via caddy)
```

## Development

```sh
cd controller && pip install -r requirements-dev.txt && python -m pytest -q
cd deploy && ./install.sh --dev      # builds the controller from source
```

## License

MIT, see [LICENSE](LICENSE). The IBM Plex fonts in `controller/homewatch/static/fonts` are under the SIL Open
Font License (`OFL.txt` there).
