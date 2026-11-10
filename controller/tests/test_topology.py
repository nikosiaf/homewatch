"""0.43: the map (what hangs off what), problems behind a parent, and the "parent restarted" cause."""

import json
import time

from homewatch import topology
from homewatch.modules import ERROR, OK, WARNING, CheckResult
from tests.test_web import client, setup_admin  # noqa: F401 - the fixture

T0 = 1_800_000_000.0

SW1 = {"name": "core", "ports": [{"name": f"ether{i}", "comment": "uplink" if i == 1 else ""} for i in range(1, 6)],
       "macs": ["AA:00:00:00:01:01"],
       # ether1 is the uplink to the firewall side (many MACs); ether3: switch "lan" behind it; ether4: the AP
       "hosts": [("AA:00:00:00:02:01", "ether3"), ("DE:AD:00:00:00:01", "ether3"), ("DE:AD:00:00:00:02", "ether3"),
                 ("BB:00:00:00:00:01", "ether4"), ("CC:00:00:00:00:01", "ether4"), ("CC:00:00:00:00:02", "ether4"),
                 ("FF:00:00:00:00:01", "ether1"), ("FF:00:00:00:00:02", "ether1"), ("FF:00:00:00:00:03", "ether1"),
                 ("FF:00:00:00:00:04", "ether1")]}
SW2 = {"name": "lan", "ports": [{"name": "ether1", "comment": ""}, {"name": "ether2", "comment": "nas"}],
       "macs": ["AA:00:00:00:02:01"],
       "hosts": [("AA:00:00:00:01:01", "ether1"), ("BB:00:00:00:00:01", "ether1"), ("DE:AD:00:00:00:01", "ether2"),
                 ("DE:AD:00:00:00:02", "ether2"), ("FF:00:00:00:00:01", "ether1")]}
UNIFI = {"devices": [{"mac": "bb:00:00:00:00:01", "name": "Hall AP"}],
         "clients": [{"mac": "cc:00:00:00:00:01", "name": "phone", "ap_mac": "bb:00:00:00:00:01", "wired": False},
                     {"mac": "cc:00:00:00:00:02", "name": "zbridge", "ap_mac": "bb:00:00:00:00:01", "wired": False}]}
HA = [{"mac": "cc:00:00:00:00:02", "name": "ZBridge", "integration": "Tasmota"},
      {"mac": "", "name": "", "integration": "Zigbee2MQTT"}]
PVE = [{"node": "pve", "vmid": 101, "name": "homeassistant"}]


def built(manual=()):
    return topology.build([SW1, SW2], UNIFI, HA, PVE, list(manual))


def test_build_places_switches_ports_aps_and_clients():
    n = built()
    assert n["sw:lan"]["parent"] == "port:core/ether3"  # its MACs are seen there
    assert n["sw:core"]["parent"] is None  # nobody sees core's own MAC: it's the top
    assert n["mac:DE:AD:00:00:00:01"]["parent"] == "port:lan/ether2"  # the port with the fewest MACs wins
    assert n["mac:BB:00:00:00:00:01"]["parent"] == "port:core/ether4" and n["mac:BB:00:00:00:00:01"]["kind"] == "ap"
    assert n["mac:CC:00:00:00:00:02"]["parent"] == "mac:BB:00:00:00:00:01"  # Wi-Fi client: its access point
    assert n["mac:CC:00:00:00:00:02"]["name"] == "zbridge"
    assert n["guest:pve/101"]["parent"] == "pve:pve" and n["integ:Zigbee2MQTT"]["kind"] == "integration"
    assert topology.ancestors(n, "mac:CC:00:00:00:00:02") == ["mac:BB:00:00:00:00:01", "port:core/ether4", "sw:core"]


def test_by_hand_wins_and_loops_are_refused():
    n = built([("integ:Zigbee2MQTT", "mac:CC:00:00:00:00:02"), ("sw:core", "sw:lan")])
    assert n["integ:Zigbee2MQTT"]["parent"] == "mac:CC:00:00:00:00:02" and n["integ:Zigbee2MQTT"]["via"] == "by hand"
    chain = topology.ancestors(n, "integ:Zigbee2MQTT")
    assert chain[:3] == ["mac:CC:00:00:00:00:02", "mac:BB:00:00:00:00:01", "port:core/ether4"]
    assert len(chain) == len(set(chain))  # core → lan → core… was cut


def test_event_nodes():
    n = built()
    assert topology.event_node(n, "mikrotik", "core") == "sw:core"
    assert topology.event_node(n, "mikrotik", "core ether4 (uplink)") == "port:core/ether4"
    assert topology.event_node(n, "unifi", "Hall AP") == "mac:BB:00:00:00:00:01"
    assert topology.event_node(n, "proxmox", "pve") == "pve:pve"
    assert topology.event_node(n, "unifi", "nobody") is None


def test_tree_leaves_out_empty_ports():
    tree = topology.tree(built())
    core = next(t for t in tree if t["id"] == "sw:core")
    # ether2 and ether5 have nobody on them; ether1 has the four MACs that are only known by their address
    assert [c["id"] for c in core["children"]] == ["port:core/ether1", "port:core/ether3", "port:core/ether4"]


def _store(db, manual=()):
    db.set("topology.map", built(manual))


def test_behind_an_open_problem_or_a_fresh_restart(db):
    _store(db, [("integ:Zigbee2MQTT", "mac:CC:00:00:00:00:02")])
    child = ["integ:Zigbee2MQTT"]
    assert topology.behind(db, "home", child, T0) is None
    db.run("INSERT INTO events(site,ts,module,device,kind,detail,exact,seen) VALUES('home',?,'unifi','Hall AP','reboot',"
           "'Hall AP restarted',1,?)", (T0 - 120, T0))
    why, held = topology.behind(db, "home", child, T0)
    assert why == "behind Hall AP: hall ap restarted 2 min ago" and not held
    assert topology.behind(db, "home", child, T0 + 3600) is None  # too long ago
    db.run("INSERT INTO issues(site,check_key,level,worst,started,notified,name,nodes) VALUES('home','unifi.devices',2,2,?,1,"
           "'Devices online',?)", (T0, json.dumps(["mac:BB:00:00:00:00:01"])))
    why, held = topology.behind(db, "home", child, T0 + 3600)
    assert why == "behind Hall AP, which has a problem: Devices online" and held


def test_engine_holds_a_child_behind_a_sent_problem(db):
    from homewatch.engine import Engine

    _store(db, [("integ:Zigbee2MQTT", "mac:CC:00:00:00:00:02")])
    e = Engine(db, "home")
    ap = CheckResult("unifi.devices", "Devices online", ERROR, "Hall AP disconnected", extra={"nodes": ["mac:BB:00:00:00:00:01"]})
    for t in (T0, T0 + 120):
        evs = e.apply("unifi", [ap], t)
    assert [x.kind for x in evs] == ["open"]
    door = CheckResult("homeassistant.unavailable", "Unavailable entities", WARNING, "1 unavailable: Door",
                       extra={"nodes": ["integ:Zigbee2MQTT"]})
    for t in (T0 + 240, T0 + 360):
        evs = e.apply("homeassistant", [door], t)
    assert evs == []  # held: the AP's problem was already sent
    row = db.one("SELECT detail, notified, nodes, incident_id FROM issues WHERE check_key='homeassistant.unavailable'")
    assert "behind Hall AP, which has a problem" in row["detail"] and row["notified"] == 1
    assert json.loads(row["nodes"]) == ["integ:Zigbee2MQTT"]
    assert row["incident_id"] == db.one("SELECT incident_id FROM issues WHERE check_key='unifi.devices'")["incident_id"]


def test_cause_names_the_parent_that_restarted(db):
    from zoneinfo import ZoneInfo

    from homewatch import causes

    _store(db)
    db.run("INSERT INTO events(site,ts,module,device,kind,detail,exact,seen) VALUES('home',?,'mikrotik','core','reboot',"
           "'core restarted',1,?)", (T0 - 60, T0))
    cause, _ = causes.explain(db, "home", T0, T0 + 600, ["homeassistant.unavailable"], ZoneInfo("UTC"),
                              ["mac:CC:00:00:00:00:02", "mac:DE:AD:00:00:00:01"])
    assert cause == "core: core restarted at 07:59, and 2 devices behind it went with it"


def test_map_page_and_edges(client):  # noqa: F811
    tok = setup_admin(client)
    rt = client.rt
    _store(rt.db)
    page = client.get("/map").text
    assert "Hall AP" in page and "Depends on, by hand" in page
    r = client.post("/map/edges", data={"csrf": tok, "child": "integ:Zigbee2MQTT", "parent": "mac:CC:00:00:00:00:02"})
    assert r.url.path == "/map" and "now hangs off" in r.text
    assert rt.db.one("SELECT parent FROM topology_edges")["parent"] == "mac:CC:00:00:00:00:02"
    _store(rt.db)  # adding an edge rebuilt the map from the modules' readings, which this test doesn't have
    assert "Pick both from the list" in client.post("/map/edges", data={"csrf": tok, "child": "nope", "parent": "sw:core"}).text
    assert "would make a loop" in client.post("/map/edges", data={"csrf": tok, "child": "sw:core",
                                                                 "parent": "mac:DE:AD:00:00:00:01"}).text
    assert rt.db.one("SELECT COUNT(*) n FROM topology_edges")["n"] == 1
    eid = rt.db.one("SELECT id FROM topology_edges")["id"]
    client.post(f"/map/edges/{eid}/delete", data={"csrf": tok})
    assert rt.db.one("SELECT COUNT(*) n FROM topology_edges")["n"] == 0


def test_proxmox_host_sits_where_its_guests_are_seen():
    sw = {"name": "core", "ports": [{"name": "ether1", "comment": ""}, {"name": "ether7", "comment": "pve"}],
          "macs": ["AA:00:00:00:01:01"],
          "hosts": [("02:8F:04:E0:C6:66", "ether7"), ("BC:24:11:00:00:01", "ether7"), ("BC:24:11:00:00:01", "ether1"),
                    ("FF:00:00:00:00:01", "ether1"), ("FF:00:00:00:00:02", "ether1")]}
    pve = [{"node": "pve", "vmid": 101, "name": "homeassistant", "macs": ["02:8f:04:e0:c6:66"]},
           {"node": "pve", "vmid": 105, "name": "frigate", "macs": ["BC:24:11:00:00:01"]}]
    n = topology.build([sw], None, None, pve)
    assert n["pve:pve"]["parent"] == "port:core/ether7" and n["pve:pve"]["via"] == "its guests' MACs"
    assert "mac:02:8F:04:E0:C6:66" not in n  # the MAC is the guest, not a device of its own
    assert topology.ancestors(n, "guest:pve/101") == ["pve:pve", "port:core/ether7", "sw:core"]


def test_guest_macs_are_read_from_their_config():
    from homewatch.modules.proxmox.plugin import MAC_RE

    assert MAC_RE.search("virtio=02:8F:04:E0:C6:66,bridge=vmbr0").group(1) == "02:8F:04:E0:C6:66"
    assert MAC_RE.search("name=eth0,bridge=vmbr0,hwaddr=BC:24:11:AB:CD:EF,ip=dhcp").group(1) == "BC:24:11:AB:CD:EF"
