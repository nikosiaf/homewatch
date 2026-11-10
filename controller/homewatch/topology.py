"""The home's map: what hangs off what, built from what the modules already read.

- MikroTik: each switch's MAC table (bridge hosts). A device sits on the port where the fewest MACs are seen
  (an uplink carries many); a switch hangs off the port of another switch where its own MACs are seen.
- UniFi: Wi-Fi clients hang off their access point; access points off their switch port (from the MAC tables).
- Home Assistant: devices with a MAC (ESPHome, Tasmota, Shelly…) get their names and their place on the map,
  and an integration can be tied to the device it runs through (e.g. Zigbee2MQTT → the Zigbee bridge) by hand.
- Proxmox: guests hang off their node.
- Edges added by hand on the Map page win over the found ones.

Node ids: sw:<switch> · port:<switch>/<port> · mac:<MAC> · pve:<node> · guest:<node>/<vmid> · integ:<integration>.

The engine uses it when a problem starts: a device behind something that has an open problem, or that just
restarted or lost link, is named as the likely reason, and its own message is held when that parent's problem
was already sent. causes.py uses it when an incident ends.
"""

from __future__ import annotations

import json
import time

RECENT = 15 * 60  # s: a parent's restart or link loss this recent explains a problem behind it
EVENT_KINDS = ("reboot", "port_down", "restart")
MAX_DEPTH = 8


def mac(m: str) -> str:
    return (m or "").strip().upper().replace("-", ":")


def build(mikrotik: list[dict] | None, unifi: dict | None, ha: list[dict] | None, proxmox: list[dict] | None,
          manual: list[tuple[str, str]] = ()) -> dict[str, dict]:
    """{node id: {name, kind, parent, via}}. Pure: the inputs are the modules' last readings."""
    nodes: dict[str, dict] = {}

    def node(nid, name="", kind="device", parent=None, via=""):
        n = nodes.setdefault(nid, {"name": "", "kind": kind, "parent": None, "via": ""})
        if name and not n["name"]:
            n["name"] = name
        if kind != "device":
            n["kind"] = kind
        if parent and not n["parent"]:
            n["parent"], n["via"] = parent, via
        return n

    # switches, their ports and their MAC tables
    own: dict[str, str] = {}  # MAC -> switch
    seen: dict[str, list[tuple[str, str]]] = {}  # MAC -> [(switch, port)]
    count: dict[tuple[str, str], int] = {}
    for d in mikrotik or []:
        sw = d["name"]
        node(f"sw:{sw}", sw, "switch")
        for p in d.get("ports", []):
            node(f"port:{sw}/{p['name']}", f"{sw} {p['name']}" + (f" ({p['comment']})" if p.get("comment") else ""),
                 "port", f"sw:{sw}", "MikroTik")
        for m in d.get("macs", []):
            own[mac(m)] = sw
        for m, port in d.get("hosts", []):
            m = mac(m)
            if (sw, port) not in [x for x in seen.get(m, [])]:
                seen.setdefault(m, []).append((sw, port))
                count[(sw, port)] = count.get((sw, port), 0) + 1

    def edge_port(m):
        cands = [c for c in seen.get(m, []) if own.get(m) != c[0]]
        return min(cands, key=lambda c: (count[c], c)) if cands else None

    for m, sw in own.items():  # a switch hangs off the port where another switch sees its MACs
        c = edge_port(m)
        if c and c[0] != sw and not nodes[f"sw:{sw}"]["parent"]:
            node(f"sw:{sw}", parent=f"port:{c[0]}/{c[1]}", via="MAC table")

    # UniFi access points and their Wi-Fi clients
    aps = {mac(d["mac"]): d["name"] for d in (unifi or {}).get("devices", []) if d.get("mac")}
    for m, name in aps.items():
        node(f"mac:{m}", name, "ap")
    for c in (unifi or {}).get("clients", []):
        m = mac(c.get("mac"))
        if not m:
            continue
        ap = mac(c.get("ap_mac"))
        node(f"mac:{m}", c.get("name") or "", "device",
             f"mac:{ap}" if ap and not c.get("wired") and ap in aps else None, "Wi-Fi" if ap else "")

    # Home Assistant devices with a MAC
    for d in ha or []:
        m = mac(d.get("mac"))
        if m:
            node(f"mac:{m}", d.get("name") or "", "device")
        if d.get("integration"):
            node(f"integ:{d['integration']}", d["integration"], "integration")

    # Proxmox: guests by their MACs; the host on the port where most of its guests are seen (they share its bridge)
    guest_mac = {}
    for g in proxmox or []:
        for m in g.get("macs") or []:
            guest_mac[mac(m)] = f"guest:{g['node']}/{g['vmid']}"
    host_ports: dict[str, dict] = {}
    for m, gid in guest_mac.items():
        c = edge_port(m)
        if c:
            host = gid.split(":", 1)[1].split("/")[0]
            host_ports.setdefault(host, {}).setdefault(c, 0)
            host_ports[host][c] += 1

    # everything with a MAC the switches know: its port
    for m in list(seen):
        if m in own or m in guest_mac:
            continue
        c = edge_port(m)
        nid = f"mac:{m}"
        if c and (nid not in nodes or not nodes[nid]["parent"]):
            node(nid, "", "device", f"port:{c[0]}/{c[1]}", "MAC table")

    # Proxmox
    for g in proxmox or []:
        node(f"pve:{g['node']}", g["node"], "host")
        node(f"guest:{g['node']}/{g['vmid']}", g.get("name") or str(g["vmid"]), "guest", f"pve:{g['node']}", "Proxmox")
    for host, ports in host_ports.items():
        (sw, port), _ = max(ports.items(), key=lambda x: (x[1], x[0]))
        node(f"pve:{host}", parent=f"port:{sw}/{port}", via="its guests' MACs")
    for m, gid in guest_mac.items():  # a guest known by MAC elsewhere (UniFi, Home Assistant) is the guest
        if f"mac:{m}" in nodes and gid in nodes:
            if nodes[f"mac:{m}"]["name"] and nodes[gid]["name"] == gid.rsplit("/", 1)[1]:
                nodes[gid]["name"] = nodes[f"mac:{m}"]["name"]
            del nodes[f"mac:{m}"]

    for child, parent in manual:  # by hand: wins
        if child == parent:
            continue
        node(child)
        node(parent)
        nodes[child]["parent"], nodes[child]["via"] = parent, "by hand"
    # names for MAC-only devices; drop loops
    for nid, n in nodes.items():
        if not n["name"]:
            n["name"] = nid.split(":", 1)[1]
        p, hops = n["parent"], 0
        while p and hops < MAX_DEPTH:
            if p == nid:
                n["parent"], n["via"] = None, ""
                break
            p, hops = nodes.get(p, {}).get("parent"), hops + 1
    return nodes


def ancestors(nodes: dict, nid: str) -> list[str]:
    out, p = [], nodes.get(nid, {}).get("parent")
    while p and p not in out and len(out) < MAX_DEPTH:
        out.append(p)
        p = nodes.get(p, {}).get("parent")
    return out


def event_node(nodes: dict, module: str, device: str) -> str | None:
    """Which node a device event is about (the events name devices as the modules show them)."""
    if module == "mikrotik":
        if f"sw:{device}" in nodes:
            return f"sw:{device}"
        sw, _, rest = device.partition(" ")
        port = rest.split(" ")[0] if rest else ""
        return f"port:{sw}/{port}" if f"port:{sw}/{port}" in nodes else None
    if module == "proxmox":
        return f"pve:{device}" if f"pve:{device}" in nodes else None
    return next((nid for nid, n in nodes.items() if n["name"] == device and n["kind"] in ("ap", "switch", "host")), None)


# ---- stored map ---------------------------------------------------------------------------------------------
def load(db) -> dict:
    return db.get("topology.map") or {}


def manual_edges(db, site: str) -> list[tuple[str, str]]:
    return [(r["child"], r["parent"]) for r in db.all("SELECT child, parent FROM topology_edges WHERE site=? ORDER BY id",
                                                      (site,))]


def refresh(rt) -> dict:
    """Rebuild the map from the modules' last readings and store it."""
    from .modules.registry import MODULES

    def snap(mid):
        m = MODULES.get(mid)
        return getattr(m, "snap", None) if m and m.enabled(m.load(rt)) else None

    mt, uf, ha, px = snap("mikrotik"), snap("unifi"), snap("homeassistant"), snap("proxmox")
    nodes = build((mt or {}).get("devices"), uf, (ha or {}).get("devices") if ha else None,
                  [{"node": g.get("node"), "vmid": g.get("vmid"), "name": g.get("name"), "macs": g.get("macs", [])}
                   for g in (getattr(px, "guests", None) or [])], manual_edges(rt.db, rt.cfg.site))
    rt.db.set("topology.map", nodes)
    rt.db.set("topology.built", time.time())
    return nodes


def behind(db, site: str, nodes_of_issue: list[str], now: float) -> tuple[str, bool] | None:
    """Is this problem's device behind something that has a problem, or just restarted or lost link?
    (why, True when that parent's open problem was already sent). None: nothing above it explains it."""
    nodes = load(db)
    if not nodes or not nodes_of_issue:
        return None
    above = []
    for n in nodes_of_issue:
        for a in ancestors(nodes, n):
            if a not in above:
                above.append(a)
    if not above:
        return None
    for i in db.all("SELECT * FROM issues WHERE site=? AND resolved IS NULL AND nodes!='[]'", (site,)):
        hit = next((a for a in above if a in json.loads(i["nodes"])), None)
        if hit:
            return f"behind {nodes[hit]['name']}, which has a problem: {i['name'] or i['check_key']}", bool(i["notified"])
    for e in db.all(f"SELECT * FROM events WHERE site=? AND ts>? AND kind IN ({','.join('?' * len(EVENT_KINDS))}) "
                    "ORDER BY ts DESC", (site, now - RECENT, *EVENT_KINDS)):
        en = event_node(nodes, e["module"], e["device"])
        if en and en in above:
            mins = max(0, int((now - e["ts"]) // 60))
            return f"behind {nodes[en]['name']}: {e['detail'].lower()} {mins} min ago", False
    return None


def tree(nodes: dict) -> list[dict]:
    """The map as nested lists for the page: roots first (switches, hosts, integrations), children by name."""
    kids: dict[str | None, list[str]] = {}
    for nid, n in nodes.items():
        kids.setdefault(n["parent"] if n["parent"] in nodes else None, []).append(nid)
    order = {"switch": 0, "host": 1, "port": 2, "ap": 3, "guest": 4, "device": 5, "integration": 6}

    def sub(parent, depth=0):
        ids = sorted(kids.get(parent, []), key=lambda i: (order.get(nodes[i]["kind"], 9), nodes[i]["name"].lower()))
        return [{"id": i, **nodes[i], "children": sub(i, depth + 1) if depth < MAX_DEPTH else []} for i in ids]

    roots = sub(None)
    # ports nobody is on, and lone MACs, only make the page long
    def prune(items):
        out = []
        for it in items:
            it["children"] = prune(it["children"])
            if it["kind"] == "port" and not it["children"]:
                continue
            out.append(it)
        return out
    return prune(roots)
