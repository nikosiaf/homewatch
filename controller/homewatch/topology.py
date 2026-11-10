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
import re
import time

RECENT = 15 * 60  # s: a parent's restart or link loss this recent explains a problem behind it
EVENT_KINDS = ("reboot", "port_down", "restart")
MAX_DEPTH = 8


def mac(m: str) -> str:
    return (m or "").strip().upper().replace("-", ":")


INFRA = ("modem", "switch", "ap", "host")  # (a UPS hangs off a host: drawn in the host's row)  # shown in their own frames on the Map page


def build(mikrotik: list[dict] | None, unifi: dict | None, ha: list[dict] | None, proxmox: list[dict] | None,
          manual: list[tuple[str, str]] = (), modem: dict | None = None, hosts: dict | None = None,
          addresses: list[dict] | None = None, roles: dict[str, str] | None = None,
          ups: dict | None = None) -> dict[str, dict]:
    """{node id: {name, kind, parent, via, [uplink]}}. Pure: the inputs are the modules' last readings."""
    nodes: dict[str, dict] = {}

    def node(nid, name="", kind="device", parent=None, via=""):
        n = nodes.setdefault(nid, {"name": "", "kind": kind, "parent": None, "via": ""})
        if name and not n["name"]:
            n["name"] = name
        if kind != "device" and n["kind"] in ("device", kind) or kind in INFRA:
            n["kind"] = kind
        if parent and not n["parent"]:
            n["parent"], n["via"] = parent, via
        return n

    if modem:
        node("modem:isp", modem.get("name") or "ISP modem", "modem")

    # switches, their ports and their MAC tables
    own: dict[str, str] = {}  # MAC -> switch
    seen: dict[str, list[tuple[str, str]]] = {}  # MAC -> [(switch, port)]
    count: dict[tuple[str, str], int] = {}
    comments = {}
    for d in mikrotik or []:
        sw = d["name"]
        node(f"sw:{sw}", sw, "switch")
        for p in d.get("ports", []):
            comments[(sw, p["name"])] = p.get("comment") or ""
            node(f"port:{sw}/{p['name']}", f"{sw} {p['name']}" + (f" ({p['comment']})" if p.get("comment") else ""),
                 "port", f"sw:{sw}", "MikroTik")
        for m in d.get("macs", []):
            own[mac(m)] = sw
        for m, port in d.get("hosts", []):
            m = mac(m)
            if (sw, port) not in seen.get(m, []):
                seen.setdefault(m, []).append((sw, port))
                count[(sw, port)] = count.get((sw, port), 0) + 1

    def edge_port(m, on=None):
        cands = [c for c in seen.get(m, []) if own.get(m) != c[0] and (on is None or c[0] == on)]
        return min(cands, key=lambda c: (count[c], c)) if cands else None

    # a switch hangs off the port where another switch sees its MACs: its uplink. Two switches see each other;
    # the one that sees the most MACs is the core (everything passes it on the way to the router), so the smaller
    # ones are placed first and the core's link back to them is left out
    total = {d["name"]: len(d.get("hosts", [])) for d in mikrotik or []}

    def above(sw, other):
        p, hops = nodes[f"sw:{other}"]["parent"], 0
        while p and hops < MAX_DEPTH:
            if p.startswith("port:") and p[5:].split("/")[0] == sw:
                return True
            p, hops = nodes.get(p, {}).get("parent"), hops + 1
        return False

    for m, sw in sorted(own.items(), key=lambda x: (total.get(x[1], 0), x[1], x[0])):
        c = edge_port(m)
        if c and c[0] != sw and not nodes[f"sw:{sw}"]["parent"] and not above(sw, c[0]):
            node(f"sw:{sw}", parent=f"port:{c[0]}/{c[1]}", via="uplink")
            back = next((edge_port(pm, on=sw) for pm, psw in own.items() if psw == c[0] and edge_port(pm, on=sw)), None)
            nodes[f"sw:{sw}"]["uplink"] = f"{c[0]} {c[1]} ↔ {sw} {back[1]}" if back else f"{c[0]} {c[1]}"

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

    # UniFi's history: a client that isn't connected now stays under the access point it last used, in red, for
    # KEEP_SEEN (it may be off, asleep or broken; that's what the red tells)
    now_ts = time.time()
    for c in (unifi or {}).get("history", []):
        m, ap = mac(c.get("mac")), mac(c.get("ap_mac"))
        if not m or ap not in aps or c.get("wired") or now_ts - float(c.get("last_seen") or 0) > KEEP_SEEN:
            continue
        nid = f"mac:{m}"
        if nid in nodes and nodes[nid]["parent"]:
            continue
        n = node(nid, c.get("name") or "", "device", f"mac:{ap}", "Wi-Fi")
        if nodes[nid]["parent"] == f"mac:{ap}":
            n.update(offline=True, last_seen=float(c["last_seen"]))
            if c.get("ip"):
                n.setdefault("ip", c["ip"])

    # Home Assistant: devices with a MAC get their names; hubs ("via") carry the devices that come through them;
    # Bluetooth devices hang off the Bluetooth proxy when there is just one, else they wait under "proxy unknown"
    ha_devs = [d for d in ha or [] if d.get("id")]
    by_id = {d["id"]: d for d in ha_devs}

    def ha_node(d):
        return f"mac:{mac(d['mac'])}" if d.get("mac") and not d.get("ble") else f"ha:{d['id']}"

    proxies = [ha_node(d) for d in ha_devs if d.get("proxy")]
    for d in ha_devs:
        node(ha_node(d), d.get("name") or "", "bluetooth" if d.get("ble") else "proxy" if d.get("proxy") else "device")
    for d in ha_devs:
        nid = ha_node(d)
        if d.get("ble"):
            if len(proxies) == 1:
                node(nid, parent=proxies[0], via="Bluetooth")
            else:
                nodes[nid]["via"] = "Bluetooth, proxy unknown"
        elif d.get("via") in by_id and not d.get("mac"):
            node(nid, parent=ha_node(by_id[d["via"]]), via="through its hub")
    for d in ha or []:
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

    # everything with a MAC the switches know: its port (wired, or behind an access point's port)
    for m in list(seen):
        if m in own or m in guest_mac:
            continue
        c = edge_port(m)
        nid = f"mac:{m}"
        if c and (nid not in nodes or not nodes[nid]["parent"]):
            node(nid, "", "device", f"port:{c[0]}/{c[1]}", "wired")

    for g in proxmox or []:
        node(f"pve:{g['node']}", g["node"], "host")
        gn = node(f"guest:{g['node']}/{g['vmid']}", g.get("name") or str(g["vmid"]), "guest", f"pve:{g['node']}", "Proxmox")
        if g.get("status") and g["status"] != "running":  # stopped or paused: drawn in red
            gn.update(offline=True, detail=str(g["status"]))
    for host, ports in host_ports.items():
        (sw, port), _ = max(ports.items(), key=lambda x: (x[1], x[0]))
        node(f"pve:{host}", parent=f"port:{sw}/{port}", via="its guests' MACs")
    for m, gid in guest_mac.items():  # a guest known by MAC elsewhere (UniFi, Home Assistant) is the guest
        if f"mac:{m}" in nodes and gid in nodes:
            if nodes[f"mac:{m}"]["name"] and nodes[gid]["name"] == gid.rsplit("/", 1)[1]:
                nodes[gid]["name"] = nodes[f"mac:{m}"]["name"]
            del nodes[f"mac:{m}"]

    # the Proxmox hosts' network cards: the bridge with the host's address is the LAN, on the switch port where
    # its guests are seen; another active bridge on a card whose guests no switch sees faces the modem (the WAN)
    lan_bridges = {}
    for g in proxmox or []:
        for m, br in g.get("nics") or []:
            if edge_port(mac(m)):
                lan_bridges.setdefault(g["node"], set()).add(br)
    for host, ifaces in (hosts or {}).items():
        if f"pve:{host}" not in nodes:
            continue
        bridges = [i for i in ifaces if i.get("type") == "bridge" and i.get("active") and i.get("bridge_ports")]
        nics = []
        for b in bridges:
            lan = bool(b.get("address")) or b["iface"] in lan_bridges.get(host, set())
            nics.append({"bridge": b["iface"], "port": b["bridge_ports"], "ip": b.get("address") or "",
                         "role": "lan" if lan else "wan"})
            if b.get("address"):
                nodes[f"pve:{host}"]["ip"] = b["address"]
        wans = [n for n in nics if n["role"] == "wan"]
        if len(wans) > 1:  # can't tell which one faces the modem
            for n in wans:
                n["role"] = "other"
        nodes[f"pve:{host}"]["nics"] = nics
        if wans and len(wans) == 1 and "modem:isp" in nodes:
            nodes["modem:isp"]["to"] = f"pve:{host}"  # the modem's cable goes to this card
            wans[0]["to"] = "modem:isp"

    for child, parent in manual:  # by hand: wins
        if child == parent:
            continue
        node(child)
        node(parent)
        nodes[child]["parent"], nodes[child]["via"] = parent, "by hand"
    # addresses: the firewall's ARP table and DHCP leases, UniFi, Home Assistant's device pages, the module settings
    ips = {}
    for a in addresses or []:
        ips.setdefault(mac(a.get("mac")), (a.get("ip"), a.get("name") or ""))
    for c in (unifi or {}).get("clients", []) + (unifi or {}).get("devices", []):
        if c.get("ip"):
            ips.setdefault(mac(c.get("mac")), (c["ip"], ""))
    for d in ha_devs:
        if d.get("ip"):
            nid = ha_node(d)
            if nid in nodes and not nodes[nid].get("ip"):
                nodes[nid]["ip"] = d["ip"]
    for d in mikrotik or []:
        if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", str(d.get("host", ""))):
            nodes[f"sw:{d['name']}"]["ip"] = d["host"]
    for m, (ip, name) in ips.items():
        n = nodes.get(f"mac:{m}")
        if n is not None:
            n.setdefault("ip", ip)
            if name and n["name"] in ("", m):
                n["name"] = name
    for g in proxmox or []:  # a guest's address by its MAC
        for m in g.get("macs") or []:
            if mac(m) in ips and f"guest:{g['node']}/{g['vmid']}" in nodes:
                nodes[f"guest:{g['node']}/{g['vmid']}"].setdefault("ip", ips[mac(m)][0])
    # what the modules are set up with: the device at the PBS module's address is the backup server, and so on
    for nid, n in nodes.items():
        role = (roles or {}).get(n.get("ip", ""))
        if role and n["kind"] not in INFRA:  # a host, switch or access point keeps its own name and box
            n["role"] = role
            if nid.startswith("mac:") and n["name"] in ("", nid[4:]):
                n["name"] = role
    # a device with the host's own address is the host (Home Assistant's Proxmox integration has one): one box
    for hid, h in [(i, x) for i, x in nodes.items() if x["kind"] == "host" and x.get("ip")]:
        for nid in [i for i, x in nodes.items() if i.startswith(("mac:", "ha:")) and x.get("ip") == h["ip"]]:
            for x in nodes.values():
                if x["parent"] == nid:
                    x["parent"] = hid
            del nodes[nid]
    # a device with no place that has the address of a placed one is that device (a second network card)
    by_ip = {x["ip"]: i for i, x in nodes.items() if x["parent"] and x.get("ip") and i.startswith(("mac:", "guest:"))}
    for nid in [i for i, x in nodes.items() if i.startswith(("mac:", "ha:")) and not x["parent"] and x.get("ip") in by_ip]:
        for x in nodes.values():
            if x["parent"] == nid:
                x["parent"] = by_ip[nodes[nid]["ip"]]
        del nodes[nid]
    # a Home Assistant device with no address of its own, named like exactly one placed device, is that device
    placed_names: dict[str, list[str]] = {}
    for nid, x in nodes.items():
        if x["parent"] and nid.startswith(("mac:", "guest:")):
            placed_names.setdefault(x["name"].lower(), []).append(nid)
    for nid in [i for i, x in nodes.items() if i.startswith("ha:") and not x["parent"]]:
        same = placed_names.get(nodes[nid]["name"].lower(), [])
        if len(same) == 1:
            for x in nodes.values():
                if x["parent"] == nid:
                    x["parent"] = same[0]
            del nodes[nid]
    # a wired device nobody named takes its switch port's comment (NAS, Cam1…) until a lease names it
    unnamed: dict[str, list[str]] = {}
    for nid, n in nodes.items():
        if nid.startswith("mac:") and n["name"] in ("", nid[4:]) and str(n["parent"]).startswith("port:"):
            unnamed.setdefault(n["parent"], []).append(nid)
    for port, ids in unnamed.items():
        sw, _, pname = port[5:].partition("/")
        comment = comments.get((sw, pname), "")
        if comment and comment.lower() not in ("uplink", "trunk"):
            for k, nid in enumerate(sorted(ids)):
                nodes[nid]["name"] = comment if k == 0 else f"{comment} {k + 1}"
    # the UPS hangs off the machine its NUT server runs on (the UPS module's address), usually by USB
    if ups:
        nid = f"ups:{ups['name']}"
        nodes[nid] = {"name": ups["name"], "kind": "ups", "parent": None, "via": "", "detail": ups.get("detail", "")}
        at = next((i for i, n in nodes.items() if ups.get("host") and n.get("ip") == ups["host"] and i != nid), None)
        if at:
            nodes[nid].update(parent=at, via="USB, NUT server")
    # a Synology VMM virtual machine (its MACs start 02:11:32) on the NAS's switch port runs on that NAS
    nas = {n["parent"]: i for i, n in nodes.items() if n.get("role") == "Synology NAS" and n["parent"]}
    for nid, n in nodes.items():
        if nid.startswith("mac:02:11:32:") and n["parent"] in nas and nas[n["parent"]] != nid:
            n.update(parent=nas[n["parent"]], kind="guest", via="VM on the NAS")
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
    fw = snap("opnsense")
    # a module that is on but has no reading (just started, or its device can't be read): keep the last good map
    # rather than draw one with whole parts missing
    waiting = [mid for mid in SOURCES if MODULES.get(mid) and MODULES[mid].enabled(MODULES[mid].load(rt))
               and getattr(MODULES[mid], "snap", None) is None]
    stored = load(rt.db)
    if waiting and stored:
        return stored
    sp = MODULES.get("speedport")
    modem = {"name": "ISP modem"} if sp and sp.enabled(sp.load(rt)) else None
    hosts = {k: v[1] for k, v in getattr(MODULES.get("proxmox"), "net", {}).items()} if px else None
    nodes = build((mt or {}).get("devices"), uf, (ha or {}).get("devices") if ha else None,
                  [{"node": g.get("node"), "vmid": g.get("vmid"), "name": g.get("name"), "macs": g.get("macs", []),
                    "nics": g.get("nics", []), "status": g.get("status", "")} for g in (getattr(px, "guests", None) or [])],
                  manual_edges(rt.db, rt.cfg.site), modem, hosts, (fw or {}).get("addresses"), roles(rt), ups_info(rt))
    now = time.time()
    seen = rt.db.get("topology.seen") or {}
    nodes = remember(nodes, seen, now)
    from . import newdevices
    rt.new_devices = newdevices.update(rt.db, rt.cfg.site, nodes, now,
                                       [mac(c.get("mac")) for c in (uf or {}).get("history", [])])
    rt.db.set("topology.seen", seen)
    rt.db.set("topology.map", nodes)
    rt.db.set("topology.built", now)
    return nodes


SOURCES = ("mikrotik", "unifi", "homeassistant", "proxmox")  # the modules the map is built from
ROLES = {"pbs": ("url", "Proxmox Backup Server"), "synology": ("url", "Synology NAS"), "opnsense": ("url", "Firewall"),
         "unifi": ("url", "UniFi controller"), "homeassistant": ("url", "Home Assistant"), "ups": ("host", "NUT server")}


def ups_info(rt) -> dict | None:
    """The UPS for the map: its name, what it is and how it is, and the address of its NUT server."""
    from .modules.registry import MODULES

    m = MODULES.get("ups")
    if not m or not m.enabled(m.load(rt)) or not getattr(m, "snap", None):
        return None
    v = m.snap.get("vars") or {}
    status = str(v.get("ups.status", "")).split()
    state = "ON BATTERY" if "OB" in status else "on mains" if "OL" in status else " ".join(status)
    model = " ".join(x for x in (v.get("ups.mfr", ""), v.get("ups.model", v.get("device.model", ""))) if x).strip()
    charge = f", battery {v['battery.charge']} %" if v.get("battery.charge") else ""
    return {"name": m.snap.get("name") or "UPS", "host": str(m.load(rt).get("host") or ""),
            "detail": " · ".join(x for x in (model, state + charge) if x)}


def roles(rt) -> dict[str, str]:
    """IP address -> what the device there is, from the addresses the modules are set up with."""
    from .modules.registry import MODULES

    out = {}
    for mid, (key, label) in ROLES.items():
        m = MODULES.get(mid)
        if not m or not m.enabled(m.load(rt)):
            continue
        host = re.sub(r"^\w+://", "", str(m.load(rt).get(key) or "")).split("/")[0].rsplit(":", 1)[0].strip("[]")
        if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", host):
            out.setdefault(host, label)
    return out
KEEP_SEEN = 30 * 86400  # a device that went away stays on the map, in red, under where it was, this long


def remember(nodes: dict, seen: dict, now: float) -> dict:
    """Note where each device is now; one that isn't seen any more (Wi-Fi client gone, MAC aged out of the switch)
    is put back where it was last seen, marked offline with the time. seen is updated in place."""
    for nid, n in nodes.items():
        if n["parent"] and n["kind"] in ("device", "proxy", "bluetooth"):
            seen[nid] = {"parent": n["parent"], "via": n["via"], "name": n["name"], "ip": n.get("ip", ""), "ts": now}
    taken = {n["ip"]: i for i, n in nodes.items() if n.get("ip")}
    for nid, s in list(seen.items()):
        if now - s["ts"] > KEEP_SEEN or (s.get("ip") and taken.get(s["ip"], nid) != nid):
            del seen[nid]  # too long ago, or its address belongs to something on the map now (merged into it)
            continue
        if s["ts"] == now or s["parent"] not in nodes:
            continue
        n = nodes.get(nid)
        if n is None:
            n = nodes[nid] = {"name": s["name"], "kind": "device", "parent": None, "via": ""}
        if not n["parent"]:
            n.update(parent=s["parent"], via=s["via"], offline=True, last_seen=s["ts"])
            if s.get("ip") and not n.get("ip"):
                n["ip"] = s["ip"]
            if n["name"] in ("", nid.split(":", 1)[1]) and s["name"]:
                n["name"] = s["name"]
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


ORDER = {"modem": 0, "switch": 1, "host": 2, "ap": 3, "port": 4, "proxy": 5, "guest": 6, "device": 7, "bluetooth": 8,
         "integration": 9}


def _kids(nodes: dict) -> dict:
    kids: dict[str | None, list[str]] = {}
    for nid, n in nodes.items():
        kids.setdefault(n["parent"] if n["parent"] in nodes else None, []).append(nid)
    for v in kids.values():
        v.sort(key=lambda i: (ORDER.get(nodes[i]["kind"], 9), nodes[i]["name"].lower()))
    return kids


def _sub(nodes: dict, kids: dict, nid: str, depth: int = 0) -> dict:
    """A node with what hangs below it; an infrastructure device below another one is only named (it has its own
    frame); ports nobody is on are left out. count = devices below (ports not counted)."""
    n = nodes[nid]
    item = {"id": nid, **n, "children": [], "ref": False}
    if n["kind"] in INFRA and depth > 0:
        return item | {"ref": True, "count": 0}
    if depth < MAX_DEPTH:
        for k in kids.get(nid, []):
            c = _sub(nodes, kids, k, depth + 1)
            if c["kind"] == "port" and not c["children"]:
                continue
            item["children"].append(c)
    item["count"] = sum(c["count"] + (c["kind"] != "port") for c in item["children"])
    return item


def tree(nodes: dict) -> list[dict]:
    """Everything that has no parent, with what hangs below it (kept for the tests and the API)."""
    kids = _kids(nodes)
    out = []
    for nid in kids.get(None, []):
        it = _sub(nodes, kids, nid)
        if it["kind"] == "port" and not it["children"]:
            continue
        out.append(it)
    return out


def where(nodes: dict, nid: str) -> str:
    """Where an infrastructure device is connected, in words."""
    n = nodes[nid]
    if n.get("uplink"):
        return f"uplink {n['uplink']}"
    if n["parent"] in nodes:
        return f"on {nodes[n['parent']]['name']}" + (f" ({n['via']})" if n["via"] and n["via"] not in ("wired", "MikroTik") else "")
    if n["kind"] == "switch":
        return "top of the network"
    return "not linked yet: add where its cable goes by hand"


def sections(nodes: dict) -> dict:
    """The Map page: a frame per infrastructure device (modem, switches, access points, hosts) with what is on
    it; Bluetooth devices by proxy; hubs that aren't placed; and what isn't connected or isn't known."""
    kids = _kids(nodes)
    infra = sorted((i for i, n in nodes.items() if n["kind"] in INFRA),
                   key=lambda i: (ORDER[nodes[i]["kind"]], nodes[i]["parent"] is not None, nodes[i]["name"].lower()))
    frames = []
    for i in infra:
        it = _sub(nodes, kids, i)
        wifi = sum(1 for c in it["children"] if c["via"] == "Wi-Fi")
        frames.append(it | {"where": where(nodes, i), "wifi": wifi})
    proxies = [_sub(nodes, kids, i) for i, n in nodes.items() if n["kind"] == "proxy"]
    lost_bt = [{"id": i, **n, "children": [], "count": 0} for i, n in nodes.items()
               if n["kind"] == "bluetooth" and not n["parent"]]
    roots = [i for i in kids.get(None, []) if nodes[i]["kind"] not in INFRA]
    hubs = [_sub(nodes, kids, i) for i in roots if kids.get(i) and nodes[i]["kind"] not in ("integration", "port")]
    unknown = [{"id": i, **nodes[i], "children": [], "count": 0} for i in roots
               if not kids.get(i) and nodes[i]["kind"] in ("device", "guest", "proxy")]
    integrations = [{"id": i, **nodes[i]} for i in roots if nodes[i]["kind"] == "integration" and not kids.get(i)]
    return {"frames": frames, "proxies": proxies, "lost_bt": lost_bt, "hubs": hubs, "unknown": unknown,
            "integrations": integrations}


def _natural(name: str):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", name)]


def _item(nodes: dict, kids: dict, nid: str, port: str = "") -> dict:
    """A device box: its address, what comes through it (a hub's devices, a proxy's Bluetooth devices) and, for an
    access point, its Wi-Fi clients (level 5); offline ones last."""
    n = nodes[nid]
    under = [k for k in kids.get(nid, []) if nodes[k]["kind"] not in INFRA]
    wifi = [k for k in under if nodes[k]["via"] == "Wi-Fi"]
    rest = [k for k in under if k not in wifi]
    order = lambda i: (bool(nodes[i].get("offline")), nodes[i]["name"].lower())  # noqa: E731
    item = {"id": nid, **n, "port": port, "mac": nid[4:] if nid.startswith("mac:") else "",
            "wifi": [_item(nodes, kids, k) for k in sorted(wifi, key=order)],
            "through": [_item(nodes, kids, k) for k in sorted(rest, key=order)]}
    item["kids"] = item["wifi"] + item["through"]  # what opens below it on the Map
    return item


def layers(nodes: dict) -> dict:
    """The Map's drawing, top down: 1 the ISP modem, 2 the host whose WAN card it is cabled to (both its cards),
    3 the switches side by side with their uplink, 4 what is wired to each switch port, 5 the Wi-Fi clients of each
    access point. Parts a home doesn't have are left out; what isn't placed is listed apart."""
    kids = _kids(nodes)
    modem = nodes.get("modem:isp")
    host_id = (modem or {}).get("to") or next((i for i, n in nodes.items() if n["kind"] == "host"), None)
    host = None
    if host_id in nodes:
        under = sorted(kids.get(host_id, []), key=lambda k: (nodes[k]["kind"] != "ups", bool(nodes[k].get("offline")),
                                                             nodes[k]["name"].lower()))
        host = {"id": host_id, **nodes[host_id], "port": "", "wifi": [], "through": [],
                "kids": [_item(nodes, kids, k) for k in under],  # the UPS first (left), then its VMs and containers
                "guests": [nodes[k]["name"] for k in under if nodes[k]["kind"] == "guest"]}
    switches = sorted((i for i, n in nodes.items() if n["kind"] == "switch"),
                      key=lambda i: (nodes[i]["parent"] is not None, nodes[i]["name"].lower()))
    lan_port = ""
    rows = []
    for sw in switches:
        name = nodes[sw]["name"]
        wired = []
        for k in kids.get(sw, []):  # linked by hand to the switch itself, not to a port
            if nodes[k]["kind"] not in ("port", "switch", "host"):
                wired.append(_item(nodes, kids, k, "by hand"))
        for port in sorted((k for k in kids.get(sw, []) if nodes[k]["kind"] == "port"), key=lambda k: _natural(k)):
            pname = port.split("/", 1)[1]
            for k in kids.get(port, []):
                kind = nodes[k]["kind"]
                if kind == "switch":
                    continue  # in the switch row, with its uplink
                if kind == "host":
                    lan_port = lan_port or f"{name} {pname}"
                    continue  # drawn above, cabled to this port
                wired.append(_item(nodes, kids, k, pname))
        rows.append({"id": sw, **nodes[sw], "wired": wired, "kids": wired, "wifi": [], "through": [], "port": ""})
    placed = {i for r in rows for w in r["wired"] for i in [w["id"]]}
    proxies = [i for i, n in nodes.items() if n["kind"] == "proxy" and not n["parent"]]
    hubs = [_item(nodes, kids, i) for i in kids.get(None, []) if kids.get(i)
            and nodes[i]["kind"] not in INFRA + ("integration", "port")] + [_item(nodes, kids, i) for i in proxies]
    lost = [{"id": i, **nodes[i]} for i in kids.get(None, []) if not kids.get(i)
            and nodes[i]["kind"] in ("device", "guest", "proxy", "bluetooth") and i not in placed]
    aps_unplaced = [_item(nodes, kids, i) for i, n in nodes.items() if n["kind"] == "ap" and not n["parent"]]
    if host:
        host["lan_port"] = lan_port
        host["wan"] = next((c for c in host.get("nics") or [] if c["role"] == "wan"), None)
        host["lan"] = next((c for c in host.get("nics") or [] if c["role"] == "lan"), None)
    # the core switch (the one the host's LAN is cabled to, else the top one) goes right, under the host
    core = next((r for r in rows if lan_port.startswith(r["name"] + " ")), rows[0] if rows else None)
    return {"modem": modem, "host": host, "switches": rows, "core": core, "others": [r for r in rows if r is not core], "hubs": hubs + aps_unplaced, "lost": lost,
            "integrations": [nodes[i]["name"] for i in kids.get(None, []) if nodes[i]["kind"] == "integration"
                             and not kids.get(i)]}
