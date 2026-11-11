# Modules

Every module is optional except the internet line, read-only and pull-based, and starts off. Each module's page in homeWatch has the setup steps, the limits and a **Read every** box.

## Internet line module

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
| DNS (5-min median) | ≥ 1000 ms | lookups fail while ping works |
| Line speed | < 50 % of contract on 2 tests in a row | optional level, e.g. < 30 % |
| Speed test recent | no successful test for 14 h | |
| Speed test latency (off by default; Ookla only) | > 40 ms on 2 tests in a row | |
| Latency under load (off by default) | ≥ 200 ms more than idle on 2 tests in a row (grade D) | |
| Probe data fresh | | no data for 5 min |

## ISP modem module (optional)

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
| Resyncs in 24 h (each one is a Grafana marker) | ≥ 3 | ≥ 5 |
| PPP session on the modem | | down (off by default; for router-mode setups) |

Things to know before you add a modem:
- **Password:** the Speedport has only its admin login, so homeWatch stores that password
  (encrypted, never shown again).
- **Sessions:** each reading logs in, which ends any open browser session on the modem.
- **Rejected logins:** after a rejected login homeWatch waits 6 hours, because the modem slows
  down further attempts.
- **Certificate:** the modem's certificate is self-signed, so homeWatch trusts the one it sees at
  the first reading and logs in only to that one. If it changes, the check goes to ERROR and
  homeWatch stops logging in until you accept the new one on the module page. The first reading
  itself can't tell an impostor from the modem, so add the modem on a network you trust.

## Proxmox VE module (optional)

Watches a Proxmox VE host or cluster through a **read-only API token** (role PVEAuditor; the module
page shows the three commands to create it). homeWatch tests the token before saving anything.

| Check | WARNING | ERROR |
|---|---|---|
| API reachable | | can't connect, or the token is rejected |
| Node online | | a node is offline |
| Node CPU / memory / root disk | ≥ 85 / 90 / 85 % | ≥ 95 % |
| Guests running | | a guest you chose to watch is not running |
| Storage | ≥ 85 % full | ≥ 95 % full, or a storage unavailable |
| Full in (at the rate of the last 7 days) | < 30 days | < 7 days |
| Disk health | SSD life left < 20 % | SMART not PASSED, or life left < 10 % |
| Backups (watched guests in a backup job) | newest > 26 h old, or none yet | newest > 50 h old, or the last attempt failed |

Backup results come from Proxmox's backup task logs (a read-only token can't list backup contents);
each log is read once.

## OPNsense module (optional)

Reads an OPNsense firewall through the API key of a user that has only the status pages it needs
(the module page lists them). Missing privileges show as "not readable", not as alarms. For the
Grafana traffic graphs, also give it **Reporting: Traffic**. For IP addresses and names on the
Map page, give it **Diagnostics: ARP Table** and **Services: Dnsmasq DNS/DHCP:
Settings** (the only privilege that opens the DHCP leases, `ui/dnsmasq/*` and `api/dnsmasq/*`; it would
also allow changing Dnsmasq's settings, homeWatch only reads the leases).

| Check | WARNING | ERROR |
|---|---|---|
| Gateways | packet loss or delay | a gateway down |
| CPU (10-minute average) / memory (without ZFS cache) / root disk | ≥ 85 / 90 / 85 % | ≥ 95 % |
| Services (the ones you choose) | | not running |
| HAProxy servers | | a health-checked server down |
| Certificates in use | expires in < 20 days | < 7 days, or expired |
| Firmware (to do) | updates found at the last check, or reboot needed, for 7 days | |

## Home Assistant module (optional)

Reads Home Assistant with the long-lived token of a dedicated **non-admin** user. Setting it up has
three steps on the module's page: **connect** (address and token), review **Checks and limits** and
the optional **host sensors**, then, if you want, tick **entities** under "Additional configuration".

Home Assistant's own health is checked from the start:

| Check | WARNING | ERROR |
|---|---|---|
| Core | starting, stopping, or safe / recovery mode | |
| Updates (to do; skipped versions don't count) | updates available for 14 days | |
| Automatic backups | last success > 26 h ago | > 50 h ago, or the last attempt failed |
| Disk use of the host (%) | ≥ 80 % | ≥ 90 % |
| Memory use of the host (%) | ≥ 80 % | ≥ 90 % |
| CPU use of the host (%) | ≥ 90 % | ≥ 98 % |

**Disk, memory and CPU** come from the System Monitor integration's disk use, memory use and processor
use sensors, or from any sensors in % you name under **Host sensors** (after connecting). Without
them, the checks stay OK with a note. CPU peaks are normal, so its limits start high (90 % / 98 %).
Their values are always kept and shown on the Home Assistant dashboard in Grafana.

**Entities.** Every entity is listed by domain with its state, and **nothing is ticked at first**.
Domains with nothing worth watching or graphing (automations, scripts, scenes, buttons, events,
updates, cameras, calendars, voice and notify services…) are left out. Entities that appeared since
you last saved the list carry a **new** badge.

- **Watch**: homeWatch reads it at every check (2 minutes by default) and reports it when it has been
  unavailable for 30 minutes. Some kinds also get their own check:

  | Check (only listed once such an entity is watched) | WARNING | ERROR |
  |---|---|---|
  | Unavailable entities (30 min or longer) | ≥ 1 | ≥ 10 |
  | Silent entities (no report for 3 × their usual gap, at least 1 h; learned over their first 7 days) | ≥ 1 | half or more of one integration's watched entities silent together |
  | Alarm panels | | triggered |
  | Safety sensors (smoke, water / moisture, gas, CO, heat, safety) | | detecting |
  | Tamper sensors | | tampered |
  | Batteries (lowest watched battery, %) | < 25 % | < 10 % |
  | Add-ons (their Running sensors) | | not running |
  | Entity rules (your own) | a rule set to warning fires | a rule set to error fires |

- **Show in Grafana** (only when [Grafana](grafana.md) runs): the entity is on the
  Home Assistant dashboards. Numbers are kept as they are, on/off-like states as 1/0; thermostats also
  keep the current and target temperature and whether they heat, lights their brightness.
- **Rule**: your own condition on a watched entity: `is` / `is not` compare the state as text,
  `below` / `above` as a number, then `warning` or `error`; several rules with `;` between them, the
  worst that fires counts.

  | Entity | Rule | Means |
  |---|---|---|
  | `sensor.heating_oil` | `below 200 warning; below 100 error` | WARNING under 200, ERROR under 100 |
  | `sensor.nvme_temperature` | `above 65 warning` | a disk getting hot |
  | `binary_sensor.panel_ac_trouble` | `is on error` | an alarm panel trouble |
  | `person.anna` | `is not home warning` | away |
  | `sensor.panel_link` | `is not online error` | anything but "online" |

Values are stored only for ticked entities (either column). When you untick both boxes of an entity,
homeWatch asks whether to keep or delete what it has stored so far.

**Silent entities.** When a hub, bridge or coordinator stops, Home Assistant keeps showing its
entities' last values. homeWatch learns how often each watched entity reports (its "last reported"
time, over its first 7 days) and names the ones silent for 3 times their usual gap, at least an hour;
half or more of one integration's watched entities silent together is an ERROR. Entities that report
only when something happens (door contacts) have long gaps and are noticed late; sensors that report
every few minutes are noticed within the hour.

**Flapping entities.** An entity that went unavailable 3 times in 7 days and came back by itself within
2 hours each time (phone and cloud sensors often do) is named under Unavailable entities but not
counted. Untick "Leave out entities that keep flapping" in the module's settings to count every one.

**Add-ons** (Home Assistant OS / Supervised): a non-admin user can't read the Supervisor, so homeWatch
uses the add-ons' **Running** sensors. They are disabled by default: in Settings → Devices & services →
Home Assistant Supervisor, open each add-on that must always run, enable its "Running" sensor, then
tick Watch for it in homeWatch.

## Synology NAS module (optional)

Reads DSM through its Web API every 10 minutes by default (log in, read, log out). DSM has no read-only
administrator role, so the setup asks for a separate `homewatch` administrator with no access to
shared folders or applications.

| Check | WARNING | ERROR |
|---|---|---|
| System | ≥ 60 °C, or DSM's temperature warning | ≥ 70 °C, or a crashed system |
| CPU / memory | ≥ 85 / 90 % | ≥ 95 % |
| Disks | hottest ≥ 50 °C | ≥ 60 °C, a disk not normal, SMART failing or bad sectors |
| Pools and volumes | fullest volume ≥ 85 %, or repairing / scrubbing | ≥ 95 %, degraded or crashed |
| Full in (at the rate of the last 7 days) | < 30 days | < 7 days |
| DSM updates (to do) | update available for 14 days | |
| Fans and power (optional, SNMPv3) | SNMP can't be read | a fan, the power supply or the system failed |

DSM's Web API doesn't report fans or the power supply; the NAS's SNMP agent does. To watch them,
enable SNMP in Control Panel → Terminal & SNMP, tick SNMPv3 and create a user (authentication SHA
and privacy AES are the safe choices; MD5, SHA256, DES and no privacy work too). On the module's
page, tick SNMPv3 and enter the same user, protocols and passwords, with "SNMP privacy" ticked or
not as in DSM. homeWatch reads UDP port 161 of the NAS at each DSM read. Untick SNMPv3 to skip it.

## MikroTik module (optional)

Reads one or more RouterOS 7 devices over the REST API with a user in the `read` group (the same
user on every device). The device's `www` service must allow the homeWatch host.

| Check | WARNING | ERROR |
|---|---|---|
| Every device read | | one of several devices can't be read |
| CPU / memory / temperature | ≥ 85 % / 90 % / 60 °C | ≥ 95 % / 95 % / 70 °C |
| Ports up (the ones you choose) | | a chosen port down |
| Port errors in the last hour | ≥ 10 | ≥ 1000 |
| Reboots in 24 h (each one is a Grafana marker) | ≥ 2 | |
| RouterBOOT (to do) | older than the installed RouterOS for 14 days | |
| RouterOS updates (option, to do) | a newer RouterOS for the device's channel, waiting 7 days | |
| Configuration copies (option) | no good copy for 2 days | |
| Configuration changed (option, off by default) | for 24 h after a device's configuration changed | |

CPU, memory and temperature list every device, worst first (`core 46 % | lan 44 %`).

Two options on the module page, both off until you tick them:

- **Check daily for RouterOS updates:** once a day homeWatch reads the newest RouterOS of each
  device's update channel (stable, long-term…) from `upgrade.mikrotik.com`. It needs internet access
  from the homeWatch host. Nothing is downloaded to, or changed on, the devices; update them yourself
  (System → Packages → Check For Updates), then upgrade RouterBOOT (System → RouterBOARD → Upgrade)
  and reboot.
- **Keep a daily copy of the configuration:** once a day homeWatch saves each device's `/export`,
  read with the same read-only user, and keeps a new copy only when something changed (60 per
  device). The module page lists the copies, shows **What changed** between two of them and offers
  each as a `.rsc` download. The `read` group can't see passwords, keys and secrets, so the copies
  leave them out. To restore: reset the device without its default configuration, `/import` the file,
  then enter the passwords, Wi-Fi keys and secrets again. The copies live in homeWatch's data volume,
  so they are part of its [backups](update-and-backup.md#back-up).

## UniFi module (optional)

Reads the devices of a UniFi Network application (UniFi OS Server or a UniFi console) as a local
**view-only** user: log in, read, log out.

| Check | WARNING | ERROR |
|---|---|---|
| Devices online (the ones you choose) | upgrading or provisioning | disconnected, heartbeat missed |
| CPU (busiest watched device) | ≥ 90 % | |
| Wi-Fi | WLAN subsystem not OK | |
| Restarts in 24 h (each one is a Grafana marker) | ≥ 2 | |
| Firmware (to do) | update available for 14 days | |

## Proxmox Backup Server module (optional)

Reads PBS every 15 minutes by default through an API token with the read-only **Audit** role (the module page
shows the commands to create it).

| Check | WARNING | ERROR |
|---|---|---|
| Datastores | fullest ≥ 85 % | ≥ 95 %, or a datastore error (e.g. not mounted) |
| Full in (at the rate of the last 7 days) | < 30 days | < 7 days |
| Backups (the groups you choose) | newest snapshot > 26 h old | > 50 h old, or no snapshots |
| Verify, prune, garbage collection, sync | latest run had warnings | latest run failed (last 8 days) |
| Updates (to do) | package updates available for 14 days | |

The Datastores table and the Grafana dashboard also show each datastore's **deduplication factor**,
as the PBS web interface does: the size of all backups over the space their chunks take on disk,
from the last garbage collection.

## UPS module (optional)

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

## Docker hosts module (optional)

Reads one or more Docker hosts through a **read-only proxy** in front of the Docker socket
([linuxserver/socket-proxy](https://docs.linuxserver.io/images/docker-socket-proxy/) with only
`CONTAINERS=1` and `INFO=1`; `POST=0`, so nothing can be started, stopped or changed).
Reading containers includes their environment variables, which often hold passwords: **only homeWatch
may reach the proxy**. On homeWatch's own host the proxy joins homeWatch's network and publishes no
port; on other hosts it runs with `network_mode: host` and the host firewall (ufw / nftables) allows
port 2375 from homeWatch only. The setup steps on the module page have the compose file and rules.

Containers with a restart policy (`always`, `unless-stopped`, `on-failure`) are watched; untick any
under "Containers to watch" (grouped per host and compose project).

Optional **host updates**: install node_exporter with its apt collector on each Debian / Ubuntu host
(`sudo apt install prometheus-node-exporter prometheus-node-exporter-collectors`) and list the hosts
under "node_exporter" (port 9100 when none is given). homeWatch reads `apt_upgrades_pending` and
`node_reboot_required` from `/metrics`; pending updates and a needed reboot go to the To do list. node_exporter
serves only metrics, but allow port 9100 from homeWatch only, like the proxy port.

| Check | WARNING | ERROR |
|---|---|---|
| Hosts reachable / every host read | | a host can't be read |
| Containers running | | a watched container is not running |
| Restarts (watched containers, last hour) | ≥ 1 | ≥ 3 |
| Health checks | a watched container is unhealthy | |
| Containers removed (save the page to accept) | a watched container is gone | |
| Host updates (node_exporter, To do) | waiting ≥ 14 days | |

## How often each module is read

Each module's page has a **Read every (minutes)** box next to its password or token. The defaults keep
alerts quick where they matter and spare devices where they don't. A problem is alerted after two
readings agree, so expect an alert one to two read intervals after it starts.

| Module | Default | Why |
|---|---|---|
| Internet line | continuous probes, checked every 2 min | the line itself |
| UPS, Home Assistant | 2 min | power events, alarm and safety sensors |
| MikroTik, UniFi, OPNsense, Proxmox VE, Docker | 5 min | each read logs in; a stop or a down port waits a few minutes |
| Synology NAS | 10 min | each read is a DSM login |
| Proxmox Backup Server | 15 min | backups run nightly |
| ISP modem | 15 min | its own setting |


## Ookla speed test (optional)

The default speed test uses Cloudflare and needs nothing extra. To use Ookla instead:
1. Go to **Modules → Internet line** and pick **Ookla Speedtest CLI** as the engine.
2. Accept Ookla's EULA and privacy policy.
3. Press **Save and install Ookla CLI**.

homeWatch then downloads the official CLI from speedtest.net for your processor (x86_64, arm64 or
armhf) into its data volume. It isn't bundled with homeWatch, because Ookla's license doesn't
allow redistribution. **System → Health** shows the installed version.

---

[← homeWatch README](../README.md) · [All documentation](README.md)
