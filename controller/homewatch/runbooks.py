"""What to do when a check goes wrong: a few lines in the message and on the Status page. Built-in defaults for
the checks where the first steps are always the same; the admin can write their own for any check (module
page → Checks and limits → What to do), which replaces the default."""

from __future__ import annotations

DEFAULTS = {
    "internet.reachable": "Look at the modem's lights; restart the modem if the DSL light is off or blinking. If the "
                          "Modem line module shows the line up, the ISP is the likely cause: check its outage page.",
    "internet.gateway": "The router doesn't answer pings: is it on, and is this homeWatch host still on its network?",
    "internet.loss": "Packet loss on the line: look at the modem's noise margin and errors (Modem line dashboard). "
                     "Lasting loss with a good line is the ISP's to fix.",
    "internet.dns": "Names don't resolve while ping works: check the router's DNS service (Unbound / dnsmasq) and "
                    "its upstream servers.",
    "opnsense.gateways": "The firewall's WAN gateway is down: look at Interfaces → Overview and the PPPoE log; a stale "
                         "PPPoE session may need the modem restarted.",
    "speedport.link": "The DSL line is down: check the cable and the modem; a line that doesn't come back is the ISP's "
                      "to fix (have the modem's line data ready).",
    "homeassistant.api": "Home Assistant doesn't answer: open it; if it hangs, restart it from the Proxmox console.",
    "homeassistant.unavailable": "Open the entity's device in Home Assistant: is it powered and on the network? For "
                                 "Zigbee devices look at the Zigbee2MQTT add-on's log first.",
    "homeassistant.silent": "Silent entities keep their last value: restart the integration or add-on they come "
                            "through (the Map shows what they hang off), then the device.",
    "homeassistant.addons": "Open the add-on's log in Home Assistant (Settings → Add-ons) and start it again.",
    "homeassistant.backups": "Settings → System → Backups: look at the last attempt's error; check the backup "
                             "location's free space and mount.",
    "proxmox.guests": "Start the guest from the Proxmox web page and look at its task log for why it stopped.",
    "proxmox.disks": "A disk reports trouble: check SMART in Proxmox (Node → Disks) and make sure backups are fresh.",
    "proxmox.backups": "Look at the backup job's task log in Proxmox (Datacenter → Backup) and the PBS datastore.",
    "pbs.datastores": "Free space on the datastore: run a prune and garbage collection, or add space.",
    "synology.disks": "Storage Manager on the NAS: check the disk's health and replace it if it is failing.",
    "synology.storage": "Storage Manager: a degraded pool needs a disk replaced; make sure the backups are fresh.",
    "ups.power": "The UPS is on battery: is the power out at home? Shut down what can't run on battery for long.",
    "docker.stopped": "Look at the container's log (docker logs <name>) and start it again.",
    "mikrotik.ports": "A chosen port is down: check the cable and the device on it (the Map shows what is there).",
    "unifi.devices": "An access point is offline: check its PoE port and cable; the Map shows which switch port.",
}


def get(db, site: str, key: str) -> str:
    row = db.one("SELECT runbook FROM checks WHERE site=? AND check_key=?", (site, key))
    if row and row["runbook"] is not None:
        return row["runbook"]
    return DEFAULTS.get(key, "")


def all_for(db, site: str) -> dict[str, str]:
    out = dict(DEFAULTS)
    for r in db.all("SELECT check_key, runbook FROM checks WHERE site=? AND runbook IS NOT NULL", (site,)):
        out[r["check_key"]] = r["runbook"]
    return {k: v for k, v in out.items() if v}
