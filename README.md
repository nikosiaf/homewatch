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
  A small, self-hosted monitor for your home network, set up entirely from a web page.<br>
  It tells you <em>what</em> broke, groups related problems into one incident with its likely cause, alerts you
  wherever you are, and keeps a year of history in ready-made Grafana dashboards.
</p>

![homeWatch's Status page: the overall status, today's key numbers and one tile per module with its 24-hour line](docs/screenshots/status.png)

> [!NOTE]
> **Built with AI, maintained by a human.** homeWatch's code, tests and documentation are written by Claude (Anthropic). A human maintainer decides what homeWatch does and how it behaves, tests every release on a real home network (Proxmox, OPNsense, MikroTik, UniFi, Synology, Home Assistant, Docker hosts) before publishing it, and makes all release decisions. Even so, read the setup steps before you give it access to your devices.

> [!IMPORTANT]
> **Read-only access, LAN or Tailscale only.** homeWatch never changes anything on your devices. Give it its own read-only account, token or proxy on each system it watches, never your personal admin login. Where a device has no read-only role (Synology DSM, the ISP modem), the module's setup steps explain what to create instead. homeWatch never needs a port open to the internet, so don't forward one.

## Why homeWatch?

When something breaks at home, the real question is rarely "which graph is red?". It's "what happened, and is it
me or my provider?"

homeWatch watches your internet line and the devices behind it and answers in plain words: line down, DNS failing,
guest stopped, backup failed. Problems that start together are grouped into one incident, and once it's over
homeWatch names the likely cause: a power cut, a modem restart, a line or ISP fault, or one of your own devices.

Pending updates land on a To do list instead of setting off alarms. And when you want to look back (how the line
did last month, when the NAS started running warm, what happened on the night of an outage), the Grafana
dashboards are already built and waiting. Everything runs on one small Docker host.

## Features

- **Plain-language checks**: every check has adjustable WARNING and ERROR limits and a "what to do" hint.
- **Incidents with a likely cause**: problems within 30 minutes of each other become one incident with a timeline; device events (reboots, DSL resyncs, running on battery) explain what happened.
- **Read-only by design**: every module reads through a read-only account, token or proxy. Passwords and tokens are stored encrypted and never shown again.
- **Eleven modules**: internet line, ISP modem, Proxmox VE, OPNsense, Home Assistant, Synology, MikroTik, UniFi, Proxmox Backup Server, UPS (NUT) and Docker hosts.
- **Ready-made Grafana dashboards** (optional): 14 dashboards built for you, with a year of history and incident markers. [More below](#grafana-dashboards).
- **Network map**: how your devices are connected, built from what the modules read, with root causes and newly seen devices highlighted.
- **To do, not alarms**: updates and firmware waiting on your devices are listed, and only become a warning after 14 days.
- **Alerts wherever you are**: Telegram, ntfy (with snooze and acknowledge buttons), email and webhooks, plus quiet hours, an offline queue, a weekly report and a healthchecks.io heartbeat.
- **Home Assistant and API**: homeWatch appears as an MQTT device in Home Assistant, and offers a read-only JSON API with tokens.
- **Managed from a web page**: a setup wizard, restorable configuration versions, export / import, and one-click updates (with the optional updater) that roll back on their own if the new release doesn't start.

## Grafana dashboards

Turn Grafana on during the install, or later on the System page, and homeWatch builds the dashboards for you. There
is nothing to import, no data source to set up and no query to write. They read the history homeWatch already
keeps: a year of it by default.

<table>
  <tr>
    <td width="50%"><img src="docs/screenshots/grafana-overview.png" alt="Grafana Overview: problems now, a status tile per module, checks needing attention, WAN traffic and the internet line's numbers"><br><sub><b>Overview</b>: current problems, a status tile per module, checks needing attention, WAN traffic and the internet line</sub></td>
    <td width="50%"><img src="docs/screenshots/grafana-internet.png" alt="Grafana Internet dashboard: latency, jitter, packet loss and DNS lookup time"><br><sub><b>Internet</b>: latency, jitter, packet loss and DNS lookup time</sub></td>
  </tr>
  <tr>
    <td><img src="docs/screenshots/grafana-synology.png" alt="Grafana Synology dashboard: temperature, CPU, memory, volumes, disk health and temperatures"><br><sub><b>Synology</b>: temperature, CPU, memory, volumes, disk health and temperatures</sub></td>
    <td><img src="docs/screenshots/grafana-ups.png" alt="Grafana UPS dashboard: status, battery, runtime, load, power draw and voltages"><br><sub><b>UPS</b>: status, battery, runtime, load, power draw and voltages</sub></td>
  </tr>
</table>

- **14 dashboards, ready to browse**: Overview, Health, Internet, Modem line, Proxmox VE, OPNsense, Home Assistant, Home Assistant Entity explorer, Synology, MikroTik, UniFi, Proxmox Backup Server, UPS and Docker. One appears for each module you turn on.
- **A year of history**: every device, port, guest, disk and Home Assistant entity you choose. Zoom out to last month or last year, or into the minutes around an outage.
- **Incidents and events on every graph**: incidents show as a red band from start to end, with their likely cause, and reboots, DSL resyncs, power cuts and gateway drops are marked at their exact time.
- **Health at a glance**: each module's status over time, how much of the last 30 days each device could be read, time to recover per check and the noisiest checks.
- **Your homeWatch login**: no second account. Admins can edit, viewers can only look, and an opened Status tile has a Grafana icon that goes straight to its dashboard.
- **Make your own**: open a built-in dashboard, **Save as**, and keep the copy in **My dashboards**. Updates never touch that folder, and homeWatch backs it up every night.

Grafana needs about 2 GB of disk and 150 MB of memory. Details: [docs/grafana.md](docs/grafana.md).

## Modules

| Module | What it watches | Access it needs |
|---|---|---|
| [Internet line](docs/modules.md#internet-line-module) | reachability, packet loss, latency, jitter, DNS, speed vs. your contract | none |
| [ISP modem](docs/modules.md#isp-modem-module-optional) | DSL link, sync rate, noise margin, resyncs, PPP session | the modem's login (Telekom Speedport Plus 2) |
| [Proxmox VE](docs/modules.md#proxmox-ve-module-optional) | nodes, guests, storage, disk health, backups | API token with the PVEAuditor role |
| [OPNsense](docs/modules.md#opnsense-module-optional) | gateways, CPU, services, HAProxy, certificates, firmware | API key limited to status pages |
| [Home Assistant](docs/modules.md#home-assistant-module-optional) | core, updates, backups, host load, the entities you choose | token of a non-admin user |
| [Synology NAS](docs/modules.md#synology-nas-module-optional) | system, disks, pools and volumes, DSM updates, fans (SNMPv3) | DSM user with no folder or app access |
| [MikroTik](docs/modules.md#mikrotik-module-optional) | ports, port errors, CPU, reboots, RouterOS, daily config copies | user in the `read` group |
| [UniFi](docs/modules.md#unifi-module-optional) | devices online, CPU, Wi-Fi, restarts, firmware | view-only user |
| [Proxmox Backup Server](docs/modules.md#proxmox-backup-server-module-optional) | datastores, backups, verify / prune / GC jobs, updates | API token with the Audit role |
| [UPS](docs/modules.md#ups-module-optional) | mains power, battery, runtime, load | a NUT server on your network |
| [Docker hosts](docs/modules.md#docker-hosts-module-optional) | containers, crash loops, health checks, OS updates | read-only socket proxy (node_exporter optional) |

## Install

**Requirements:** a Linux host (amd64 or arm64) with Docker Engine and the Compose plugin. 2 CPUs, 2 GB RAM and
10 GB of disk are plenty (add 2 GB of disk if you turn on Grafana).

```sh
mkdir homewatch && cd homewatch
for f in docker-compose.yml Caddyfile env.example install.sh update.sh; do
  curl -fsSLO "https://github.com/nikosiaf/homewatch/releases/latest/download/$f"
done
chmod +x install.sh update.sh
less install.sh   # it's short: please read it before running it
./install.sh
```

`install.sh` asks whether to add Grafana (you can change your mind later on the System page). Then:

1. Open `https://<host>:5443`.
2. Check that the certificate fingerprint matches the one `install.sh` printed.
3. Create the admin account straight away.
4. Follow the setup wizard.

For ports, the wizard's steps, a manual install and reverse proxies, see [docs/install.md](docs/install.md).

## Documentation

- [Install](docs/install.md)
- [Modules](docs/modules.md): checks and limits for every module, read intervals, Ookla speed test
- [Network map](docs/map.md)
- [Alerts and notifications](docs/alerts.md)
- [Grafana dashboards](docs/grafana.md)
- [API and Home Assistant (MQTT)](docs/api-and-mqtt.md)
- [Update and back up](docs/update-and-backup.md)
- [Security notes](docs/security.md)

## How it fits together

```text
browser ──HTTPS──▶ caddy ──▶ controller (FastAPI: UI, checks every 120 s, map, notifier, speed test)
                                 │  ▲                        │
                     SQLite ◀────┘  │ reads              Telegram / ntfy / email / webhook /
                                    │                     healthchecks.io / MQTT → Home Assistant
                                    │
telegraf (ping, DNS) ──writes──▶ victoriametrics ◀──reads── grafana (optional, /grafana/ via caddy)
```

## Development

From the repository root:

```sh
# run the tests
(cd controller && pip install -r requirements-dev.txt && python -m pytest -q)

# build and run the controller from source
(cd deploy && ./install.sh --dev)
```

## License

MIT, see [LICENSE](LICENSE). The IBM Plex fonts in `controller/homewatch/static/fonts` are licensed under the SIL
Open Font License (see `OFL.txt` in that folder).
