"""New devices on the network: a MAC on a switch port or an access point, or a VM, that homeWatch has never seen.
The first time it runs, everything there is (and UniFi's client history) counts as known, so only what joins
afterwards is new. A new device is listed on the Status page and marked on the Map until it is marked as known,
and a WARNING message is sent once.

A switch's MAC table also picks up stray addresses now and then (a corrupted or oddly bridged frame shows up as a
MAC for one reading and is gone). So a MAC that only a switch has seen must stay for a while before it counts:
something on Wi-Fi (UniFi has it as a client), with an IP address, or a VM is real at once; a bare MAC on a switch
port counts after CONFIRM_READINGS readings over CONFIRM_S. Guests on a guest network count at once too (they get
an address while still at the captive portal) and are labelled as guests."""

from __future__ import annotations

import re

from .engine import Event
from .modules import WARNING

KINDS = ("device", "proxy", "guest", "ap")  # what counts as a device (not ports, switches, integrations)
CONFIRM_S = 600  # a bare MAC on a switch port must be seen for 10 minutes ...
CONFIRM_READINGS = 3  # ... in at least 3 readings
FORGET_S = 1800  # a waiting MAC not seen for 30 minutes is dropped
WAITING = "newdevices.waiting"  # setting: {node: [first seen, last seen, readings]}


def private(nid: str) -> bool:
    """A locally administered MAC: phones and tablets use a private, changing Wi-Fi address."""
    m = re.match(r"mac:[0-9A-F]([26AE])", nid)
    return bool(m) and not nid.startswith("mac:02:11:32")  # Synology VMM guests are local too, but stable


def junk(nid: str) -> bool:
    """Not a device address: multicast/broadcast (lowest bit of the first byte) or all zeros."""
    m = re.fullmatch(r"mac:([0-9A-F]{2})((?::[0-9A-F]{2}){5})", nid)
    return bool(m) and (int(m.group(1), 16) & 1 == 1 or nid == "mac:00:00:00:00:00:00")


def real_now(n: dict, nid: str) -> bool:
    """Counts at once: a VM, a Wi-Fi client UniFi has associated, anything with an IP address."""
    return nid.startswith("guest:") or n.get("via") == "Wi-Fi" or bool(n.get("ip")) or n["kind"] in ("ap", "proxy")


def place(nodes: dict, n: dict) -> str:
    p = nodes.get(n.get("parent") or "", {})
    return p.get("name", "") + (f" ({n['via']})" if n.get("via") in ("Wi-Fi",) else "")


def update(db, site: str, nodes: dict, now: float, history_macs=()) -> list[dict]:
    """Note every device on the map; return the ones never seen before (and mark them "new" on the map)."""
    present = {i: n for i, n in nodes.items() if n["kind"] in KINDS and n.get("parent") and not n.get("offline")
               and i.startswith(("mac:", "guest:"))}
    first_run = db.one("SELECT 1 FROM known_devices WHERE site=? LIMIT 1", (site,)) is None
    known = {r["node"]: r for r in db.all("SELECT * FROM known_devices WHERE site=?", (site,))}
    new = []
    with db.tx() as t:
        if first_run:  # the baseline: what is there now, and what UniFi remembers, is known
            for nid in set(present) | {f"mac:{m}" for m in history_macs}:
                n = nodes.get(nid, {})
                t.run("INSERT OR IGNORE INTO known_devices(site,node,first_seen,last_seen,name,ip,place,acked) "
                      "VALUES(?,?,?,?,?,?,?,?)", (site, nid, now, now, n.get("name", ""), n.get("ip", ""),
                                                  place(nodes, n) if n else "", now))
            return []
        waiting = db.get(WAITING) or {}
        for nid, n in present.items():
            if nid in known:
                t.run("UPDATE known_devices SET last_seen=?, name=?, ip=?, place=? WHERE site=? AND node=?",
                      (now, n["name"], n.get("ip", ""), place(nodes, n), site, nid))
                continue
            if junk(nid):
                continue
            if not real_now(n, nid):  # only a switch has seen it: wait until it has stayed a while
                first, _, readings = waiting.get(nid, (now, now, 0))
                waiting[nid] = [first, now, readings + 1]
                if readings + 1 < CONFIRM_READINGS or now - first < CONFIRM_S:
                    continue
            first = waiting.pop(nid, [now])[0]
            t.run("INSERT INTO known_devices(site,node,first_seen,last_seen,name,ip,place) VALUES(?,?,?,?,?,?,?)",
                  (site, nid, first, now, n["name"], n.get("ip", ""), place(nodes, n)))
            new.append({"node": nid, "name": n["name"], "ip": n.get("ip", ""), "place": place(nodes, n),
                        "private": private(nid), "guest_wifi": n.get("guest_wifi", "")})
        db.set(WAITING, {k: v for k, v in waiting.items() if now - v[1] < FORGET_S and k not in known})
    for r in db.all("SELECT node FROM known_devices WHERE site=? AND acked IS NULL", (site,)):
        if r["node"] in nodes:
            nodes[r["node"]]["new"] = True
    return new


def events(new: list[dict], now: float) -> list[Event]:
    """One WARNING message per new device (it opens no issue: Known on the Status page dismisses it)."""
    out = []
    for d in new:
        what = d["name"] if d["name"] != d["node"].split(":", 1)[1] else "unknown device"
        detail = " · ".join(x for x in (what, d["ip"], d["node"].split(":", 1)[1], f"on {d['place']}" if d["place"] else "",
                                        ("guest Wi-Fi" + ("" if d["guest_wifi"] == "no portal state" else f", {d['guest_wifi']}"))
                                        if d.get("guest_wifi") else "",
                                        "private Wi-Fi address (a phone or tablet)" if d["private"] else "") if x)
        out.append(Event("open", "network.new_device", "New device on the network", WARNING, WARNING, detail, now))
    return out


def pending(db, site: str) -> list[dict]:
    """The devices still to be marked as known, with the guest-portal state from the map when there is one."""
    nodes = db.get("topology.map") or {}
    return [dict(r) | {"guest_wifi": nodes.get(r["node"], {}).get("guest_wifi", "")} for r in
            db.all("SELECT * FROM known_devices WHERE site=? AND acked IS NULL ORDER BY first_seen DESC", (site,))]
