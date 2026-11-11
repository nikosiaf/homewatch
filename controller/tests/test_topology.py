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
    assert "Hall AP" in page and "Manually set dependencies" in page
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


def test_map_frames_bluetooth_hubs_and_unknown(client):  # noqa: F811
    setup_admin(client)
    ha = [{"id": "esp", "name": "bedroomBT", "mac": "cc:00:00:00:00:02", "bt": "", "via": None, "ble": False, "proxy": True},
          {"id": "lamp", "name": "Lamp", "mac": "be:69:a9:01:07:19", "bt": "", "via": None, "ble": True, "proxy": False},
          {"id": "z2m", "name": "Zigbee2MQTT Bridge", "mac": "", "bt": "", "via": None, "ble": False, "proxy": False},
          {"id": "door", "name": "Rear door", "mac": "", "bt": "", "via": "z2m", "ble": False, "proxy": False},
          {"id": "gone", "name": "Old plug", "mac": "aa:bb:cc:dd:ee:ff", "bt": "", "via": None, "ble": False, "proxy": False}]
    n = topology.build([SW1, SW2], UNIFI, ha, PVE, [], {"name": "ISP modem"})
    assert n["sw:lan"]["uplink"] == "core ether3 ↔ lan ether1"
    assert n["ha:lamp"]["parent"] == "mac:CC:00:00:00:00:02" and n["ha:lamp"]["via"] == "Bluetooth"
    assert n["ha:door"]["parent"] == "ha:z2m" and n["mac:CC:00:00:00:00:02"]["kind"] == "proxy"
    sec = topology.sections(n)
    assert [f["name"] for f in sec["frames"]] == ["ISP modem", "core", "lan", "pve", "Hall AP"]
    frame = {f["name"]: f for f in sec["frames"]}
    assert frame["lan"]["where"] == "uplink core ether3 ↔ lan ether1" and frame["core"]["where"] == "top of the network"
    assert frame["ISP modem"]["where"].startswith("not linked yet")
    assert frame["Hall AP"]["wifi"] == 2
    core_ports = [c["name"] for c in frame["core"]["children"]]
    assert core_ports == ["core ether1 (uplink)", "core ether3", "core ether4"]
    on3 = frame["core"]["children"][1]["children"]
    assert on3[0]["ref"] and on3[0]["name"] == "lan"  # another switch: named, it has its own frame
    assert [p["name"] for p in sec["proxies"]] == ["zbridge"] and sec["proxies"][0]["children"][0]["name"] == "Lamp"
    assert [h["name"] for h in sec["hubs"]] == ["Zigbee2MQTT Bridge"]
    assert [u["name"] for u in sec["unknown"]] == ["Old plug"]
    client.rt.db.set("topology.map", n)
    page = client.get("/map").text
    assert 'class="nm-box k-switch ' in page and "Not connected or unknown" in page and "Old plug" in page
    assert "core ether3 ↔ lan ether1" in page and 'data-tree=' not in page


def test_two_proxies_leave_bluetooth_unplaced():
    ha = [{"id": "p1", "name": "P1", "mac": "11:00:00:00:00:01", "bt": "", "via": None, "ble": False, "proxy": True},
          {"id": "p2", "name": "P2", "mac": "11:00:00:00:00:02", "bt": "", "via": None, "ble": False, "proxy": True},
          {"id": "lamp", "name": "Lamp", "mac": "be:69:00:00:00:01", "bt": "", "via": None, "ble": True, "proxy": False}]
    sec = topology.sections(topology.build(None, None, ha, None))
    assert [x["name"] for x in sec["lost_bt"]] == ["Lamp"] and sec["lost_bt"][0]["via"] == "Bluetooth, proxy unknown"


def test_ha_devices_for_the_map():
    from homewatch.modules.homeassistant.plugin import device_row, map_devices

    reg = {"esp": device_row({"name": "bedroomBT", "connections": [["mac", "10:20:ba:32:7a:54"]]}),
           "adapter": device_row({"name": "bedroomBT (10:20:BA:32:7A:56)", "connections": [["bluetooth", "10:20:BA:32:7A:56"]],
                                  "via_device_id": "esp"}),
           "lamp": device_row({"name": "nikosBTled", "connections": [["mac", "be:69:a9:01:07:19"]],
                               "identifiers": [["elkbledom", "BE:69:A9:01:07:19"]]}),
           "z2m": device_row({"name": "Zigbee2MQTT Bridge", "identifiers": [["mqtt", "bridge"]]}),
           "door": device_row({"name": "Rear door", "identifiers": [["mqtt", "0x00"]], "via_device_id": "z2m"}),
           "cloud": device_row({"name": "Garmin"})}
    reg["hall"] = device_row({"name": "Hall", "connections": [["mac", "60:01:94:52:88:21"]],
                              "identifiers": [["fritz", "60:01:94:52:88:21"]]})
    assert not reg["hall"]["ble"]  # a router's tracker names Wi-Fi devices by MAC too
    cat = [{"id": "binary_sensor.door", "device_id": "door", "integration": "MQTT"},
           {"id": "light.lamp", "device_id": "lamp", "integration": "Elkbledom"},
           {"id": "sensor.garmin", "device_id": "cloud", "integration": "Garmin"}]
    got = {d["id"]: d for d in map_devices(reg, cat, {"binary_sensor.door"})}
    assert set(got) == {"esp", "lamp", "z2m", "door", "hall"}  # watched, with an address, the hubs above; no adapter
    assert got["esp"]["proxy"] and got["lamp"]["ble"] and not got["esp"]["ble"]
    assert got["door"]["via"] == "z2m" and got["door"]["integration"] == "MQTT"


def test_layers_ips_host_cards_and_last_seen():
    sw = {"name": "core", "host": "192.168.2.2",
          "ports": [{"name": "ether1", "comment": ""}, {"name": "ether3", "comment": "pve"}, {"name": "ether4", "comment": "ap"},
                    {"name": "ether6", "comment": "Paradox"}],
          "macs": ["AA:00:00:00:01:01"],
          "hosts": [("02:8F:04:E0:C6:66", "ether3"), ("BB:00:00:00:00:01", "ether4"), ("CC:00:00:00:00:01", "ether4"),
                    ("DD:00:00:00:00:06", "ether6")]}
    unifi = {"devices": [{"mac": "bb:00:00:00:00:01", "name": "Hall AP", "ip": "192.168.2.20"}],
             "clients": [{"mac": "cc:00:00:00:00:01", "name": "phone", "ap_mac": "bb:00:00:00:00:01", "wired": False,
                          "ip": "192.168.2.150"}]}
    pve = [{"node": "pve", "vmid": 100, "name": "OPNsense", "macs": ["02:8F:04:E0:C6:66", "02:00:00:00:00:99"],
            "nics": [["02:8F:04:E0:C6:66", "vmbr0"], ["02:00:00:00:00:99", "vmbr1"]]}]
    hosts = {"pve": [{"iface": "vmbr0", "type": "bridge", "active": 1, "bridge_ports": "nic0", "address": "192.168.2.253"},
                     {"iface": "vmbr1", "type": "bridge", "active": 1, "bridge_ports": "nic1"},
                     {"iface": "nic0", "type": "eth", "active": 1}]}
    addresses = [{"mac": "DD:00:00:00:00:06", "ip": "192.168.2.60", "name": "IP150"}]
    n = topology.build([sw], unifi, None, pve, [("ha:sp6000", "mac:DD:00:00:00:00:06")], {"name": "ISP modem"}, hosts, addresses)
    assert n["pve:pve"]["nics"] == [{"bridge": "vmbr0", "port": "nic0", "ip": "192.168.2.253", "role": "lan"},
                                    {"bridge": "vmbr1", "port": "nic1", "ip": "", "role": "wan", "to": "modem:isp"}]
    assert n["modem:isp"]["to"] == "pve:pve" and n["pve:pve"]["ip"] == "192.168.2.253"
    assert n["mac:DD:00:00:00:00:06"]["name"] == "IP150" and n["mac:DD:00:00:00:00:06"]["ip"] == "192.168.2.60"
    assert n["sw:core"]["ip"] == "192.168.2.2" and n["mac:CC:00:00:00:00:01"]["ip"] == "192.168.2.150"
    seen = {}
    n = topology.remember(n, seen, 1000.0)
    lay = topology.layers(n)
    assert lay["modem"]["name"] == "ISP modem" and lay["host"]["name"] == "pve" and lay["host"]["guests"] == ["OPNsense"]
    core = lay["core"]
    assert core is lay["switches"][0] and lay["others"] == []
    assert [(w["port"], w["name"]) for w in core["wired"]] == [("ether4", "Hall AP"), ("ether6", "IP150")]
    assert lay["host"]["lan_port"] == "core ether3"  # the host is drawn above, cabled to this port
    assert lay["host"]["wan"]["port"] == "nic1" and lay["host"]["lan"]["port"] == "nic0"
    ap = core["wired"][0]
    assert [w["name"] for w in ap["wifi"]] == ["phone"] and ap["kids"] == ap["wifi"]
    assert core["wired"][1]["through"][0]["name"] == "sp6000"
    # the phone leaves: it stays under the AP, in red, with when it was last seen
    sw_now = sw | {"hosts": [h for h in sw["hosts"] if h[0] != "CC:00:00:00:00:01"]}  # aged out of the MAC table too
    gone = topology.build([sw_now], {"devices": unifi["devices"], "clients": []}, None, pve, [], {"name": "ISP modem"}, hosts)
    gone = topology.remember(gone, seen, 2000.0)
    phone = gone["mac:CC:00:00:00:00:01"]
    assert phone["offline"] and phone["last_seen"] == 1000.0 and phone["parent"] == "mac:BB:00:00:00:00:01"
    assert phone["name"] == "phone" and phone["ip"] == "192.168.2.150"
    assert topology.remember(topology.build([sw_now], None, None, None), seen, 1000.0 + topology.KEEP_SEEN + 1) is not None
    assert "mac:CC:00:00:00:00:01" not in seen  # forgotten after 30 days


def test_opnsense_addresses():
    from homewatch.modules.opnsense.plugin import addresses

    arp = [{"mac": "aa:bb:cc:00:00:01", "ip": "192.168.2.10", "hostname": "nas"}, {"mac": "(incomplete)", "ip": "1.2.3.4"}]
    leases = {"rows": [{"hwaddr": "aa:bb:cc:00:00:02", "address": "192.168.10.65", "hostname": "zbridge"},
                       {"hwaddr": "aa:bb:cc:00:00:01", "address": "192.168.2.10", "hostname": "nas-lease"}]}
    got = {a["mac"]: a for a in addresses(arp, leases)}
    assert got["AA:BB:CC:00:00:01"] == {"mac": "AA:BB:CC:00:00:01", "ip": "192.168.2.10", "name": "nas"}
    assert got["AA:BB:CC:00:00:02"]["ip"] == "192.168.10.65" and len(got) == 2
    assert addresses(None, None) is None


def test_port_comments_name_wired_devices_and_the_host_is_one_box():
    sw = {"name": "core", "ports": [{"name": "ether1", "comment": "UpLink"}, {"name": "ether5", "comment": "NAS"},
                                    {"name": "ether3", "comment": "pve"}],
          "macs": ["AA:00:00:00:01:01"],
          "hosts": [("11:00:00:00:00:05", "ether5"), ("11:00:00:00:00:06", "ether5"), ("22:00:00:00:00:01", "ether1"),
                    ("02:8F:04:E0:C6:66", "ether3"), ("33:00:00:00:00:03", "ether3")]}
    ha = [{"id": "nodepve", "name": "Node pve", "mac": "33:00:00:00:00:03", "bt": "", "via": None, "ble": False,
           "proxy": False, "ip": "192.168.2.253"}]
    pve = [{"node": "pve", "vmid": 101, "name": "ha", "macs": ["02:8F:04:E0:C6:66"], "nics": [["02:8F:04:E0:C6:66", "vmbr0"]]}]
    hosts = {"pve": [{"iface": "vmbr0", "type": "bridge", "active": 1, "bridge_ports": "nic0", "address": "192.168.2.253"}]}
    n = topology.build([sw], None, ha, pve, [], None, hosts)
    assert n["mac:11:00:00:00:00:05"]["name"] == "NAS" and n["mac:11:00:00:00:00:06"]["name"] == "NAS 2"
    assert n["mac:22:00:00:00:00:01"]["name"] == "22:00:00:00:00:01"  # "UpLink" is not a device's name
    assert "mac:33:00:00:00:00:03" not in n and "ha:nodepve" not in n  # Home Assistant's "Node pve" is the host


def test_a_merged_device_is_not_brought_back():
    seen = {"mac:33:00:00:00:00:03": {"parent": "port:core/ether3", "via": "wired", "name": "Node pve",
                                      "ip": "192.168.2.253", "ts": 10.0}}
    n = {"pve:pve": {"name": "pve", "kind": "host", "parent": "port:core/ether3", "via": "", "ip": "192.168.2.253"},
         "port:core/ether3": {"name": "core ether3", "kind": "port", "parent": None, "via": ""}}
    topology.remember(n, seen, 20.0)
    assert "mac:33:00:00:00:00:03" not in n and seen == {}


def test_map_page_one_open_at_a_time(client):  # noqa: F811
    setup_admin(client)
    client.rt.db.set("topology.map", topology.build([SW1, SW2], UNIFI, None, PVE, [], {"name": "ISP modem"}))
    page = client.get("/map").text
    # line 2: the other switch left, the core right; each opens its own wired row, all closed at first
    assert page.index('data-open="p-sw-lan"') < page.index('data-open="p-sw-core"')
    assert '<div class="nm-panel" id="p-sw-core" hidden>' in page and '<div class="nm-panel" id="p-sw-lan" hidden>' in page
    assert "nm-level dir-rtl" in page and "nm-level dir-ltr" in page and 'aria-expanded="true"' not in page
    assert 'id="p-mac-BB-00-00-00-00-01" hidden' in page  # the access point's Wi-Fi clients, one level down


def test_unifi_history_puts_offline_clients_under_their_last_ap():
    import time as _t

    now = _t.time()
    unifi = {"devices": [{"mac": "bb:00:00:00:00:01", "name": "Hall AP"}],
             "clients": [{"mac": "cc:00:00:00:00:01", "name": "phone", "ap_mac": "bb:00:00:00:00:01", "wired": False}],
             "history": [{"mac": "cc:00:00:00:00:01", "name": "phone", "ap_mac": "bb:00:00:00:00:01", "last_seen": now},
                         {"mac": "fc:3c:d7:eb:f7:e3", "name": "main-door", "ap_mac": "bb:00:00:00:00:01",
                          "ip": "192.168.10.148", "last_seen": now - 3600},
                         {"mac": "dd:00:00:00:00:09", "name": "old", "ap_mac": "bb:00:00:00:00:01", "last_seen": now - 40 * 86400},
                         {"mac": "ee:00:00:00:00:01", "name": "pc", "ap_mac": "", "wired": True, "last_seen": now}]}
    n = topology.build([SW1], unifi, None, None)
    door = n["mac:FC:3C:D7:EB:F7:E3"]
    assert door["parent"] == "mac:BB:00:00:00:00:01" and door["offline"] and door["ip"] == "192.168.10.148"
    assert not n["mac:CC:00:00:00:00:01"].get("offline")  # connected now
    assert "mac:DD:00:00:00:00:09" not in n and "mac:EE:00:00:00:00:01" not in n  # too old; wired (not an AP's)
    ap = next(w for w in topology.layers(n)["core"]["wired"] if w["name"] == "Hall AP")
    assert [w["name"] for w in ap["wifi"]] == ["phone", "main-door"]  # offline ones last


def test_ha_device_named_like_a_placed_one_is_that_one():
    ha = [{"id": "nas", "name": "siafNAS", "mac": "", "bt": "", "via": None, "ble": False, "proxy": False},
          {"id": "vol", "name": "Volume 1", "mac": "", "bt": "", "via": "nas", "ble": False, "proxy": False}]
    sw = SW2 | {"hosts": SW2["hosts"]}
    n = topology.build([sw], None, ha, None, [], None, None, [{"mac": "DE:AD:00:00:00:01", "ip": "192.168.2.250",
                                                                "name": "siafnas"}])
    assert "ha:nas" not in n and n["ha:vol"]["parent"] == "mac:DE:AD:00:00:00:01"


def test_second_card_with_the_same_address_is_the_same_device():
    ha = [{"id": "nas", "name": "siafNAS", "mac": "90:09:d0:18:dc:6d", "bt": "", "via": None, "ble": False, "proxy": False,
           "ip": "192.168.2.250"}]
    n = topology.build([SW2], None, ha, None, [], None, None,
                       [{"mac": "DE:AD:00:00:00:01", "ip": "192.168.2.250", "name": "siafnas"}])
    assert "mac:90:09:D0:18:DC:6D" not in n and n["mac:DE:AD:00:00:00:01"]["name"] == "siafnas"


def test_scan_now_reads_the_map_sources(client, monkeypatch):  # noqa: F811
    tok = setup_admin(client)
    rt = client.rt
    calls = []

    async def fake_run(now=None, only=None):
        calls.append(only)
        return []

    monkeypatch.setattr(rt, "run_checks", fake_run)
    r = client.post("/map/scan", data={"csrf": tok})
    assert "Scanned:" in r.text and calls == [set()]  # no map module set up here: nothing to read, map rebuilt
    assert "less than a minute ago" in client.post("/map/scan", data={"csrf": tok}).text
    page = client.get("/map").text
    assert "Network Map" in page and "Manually set dependencies" in page and "<h1>Map</h1>" not in page
    assert page.index("Network Map") < page.index("Manually set dependencies")


def test_map_is_not_replaced_while_a_module_has_no_reading(cfg):
    from homewatch.modules.registry import MODULES
    from homewatch.runtime import Runtime

    rt = Runtime.create(cfg)
    good = built()
    rt.db.set("topology.map", good)
    mt = MODULES["mikrotik"]
    rt.db.set(mt.key, {"enabled": True, "devices": "192.168.2.2", "username": "u", "chosen": []})
    old = mt.snap
    try:
        mt.snap = None  # just started: nothing read yet
        assert topology.refresh(rt) == good and rt.db.get("topology.map") == good
    finally:
        mt.snap = old


def test_module_roles_and_a_vm_on_the_nas():
    sw = {"name": "core", "ports": [{"name": "ether5", "comment": "NAS"}], "macs": ["AA:00:00:00:01:01"],
          "hosts": [("90:09:D0:18:DC:6C", "ether5"), ("02:11:32:2B:20:ED", "ether5")]}
    addrs = [{"mac": "90:09:D0:18:DC:6C", "ip": "192.168.2.250", "name": "siafnas"},
             {"mac": "02:11:32:2B:20:ED", "ip": "192.168.2.243", "name": ""}]
    roles = {"192.168.2.250": "Synology NAS", "192.168.2.243": "Proxmox Backup Server"}
    n = topology.build([sw], None, None, None, [], None, None, addrs, roles)
    nas, pbs = n["mac:90:09:D0:18:DC:6C"], n["mac:02:11:32:2B:20:ED"]
    assert nas["name"] == "siafnas" and nas["role"] == "Synology NAS"
    assert pbs["name"] == "Proxmox Backup Server" and pbs["parent"] == "mac:90:09:D0:18:DC:6C"
    assert pbs["kind"] == "guest" and pbs["via"] == "VM on the NAS"
    core = topology.layers(n)["core"]
    assert [w["name"] for w in core["wired"]] == ["siafnas"] and core["wired"][0]["kids"][0]["name"] == "Proxmox Backup Server"


def test_roles_leave_hosts_alone():
    pve = [{"node": "pve", "vmid": 101, "name": "ha", "macs": [], "nics": []}]
    hosts = {"pve": [{"iface": "vmbr0", "type": "bridge", "active": 1, "bridge_ports": "nic0", "address": "192.168.2.253"}]}
    n = topology.build(None, None, None, pve, [], None, hosts, None, {"192.168.2.253": "NUT server"})
    assert n["pve:pve"]["name"] == "pve" and "role" not in n["pve:pve"]


def test_a_device_linked_to_the_switch_itself_is_drawn(client):  # noqa: F811
    tok = setup_admin(client)
    n = topology.build([SW1], None, None, None, [("mac:A8:23:FE:D4:5A:8B", "sw:core")])
    core = topology.layers(n)["core"]
    assert [(w["port"], w["name"]) for w in core["wired"]][0] == ("by hand", "A8:23:FE:D4:5A:8B")
    client.rt.db.set("topology.map", built())
    page = client.get("/map").text
    assert '<option value="port:core/ether4">' in page  # ports can be picked for a link by hand


def test_ups_hangs_off_its_nut_host_and_the_host_opens(client):  # noqa: F811
    setup_admin(client)
    pve = [{"node": "pve", "vmid": 101, "name": "homeassistant", "macs": [], "nics": []},
           {"node": "pve", "vmid": 100, "name": "OPNsense", "macs": [], "nics": []}]
    hosts = {"pve": [{"iface": "vmbr0", "type": "bridge", "active": 1, "bridge_ports": "nic0", "address": "192.168.2.253"}]}
    ups = {"name": "apc", "host": "192.168.2.253", "detail": "APC Back-UPS · on mains, battery 100 %"}
    n = topology.build([SW1], None, None, pve, [], {"name": "ISP modem"}, hosts, None, None, ups)
    assert n["ups:apc"]["parent"] == "pve:pve" and n["ups:apc"]["via"] == "USB, NUT server"
    host = topology.layers(n)["host"]
    assert [k["name"] for k in host["kids"]] == ["apc", "homeassistant", "OPNsense"]  # the UPS first, on the left
    client.rt.db.set("topology.map", n)
    page = client.get("/map").text
    assert 'data-open="p-pve-pve"' in page and '<div class="nm-panel" id="p-pve-pve" hidden>' in page
    assert "APC Back-UPS · on mains, battery 100 %" in page


def test_stopped_guests_are_red_and_last():
    pve = [{"node": "pve", "vmid": 101, "name": "homeassistant", "macs": [], "nics": [], "status": "running"},
           {"node": "pve", "vmid": 120, "name": "Win11", "macs": [], "nics": [], "status": "stopped"},
           {"node": "pve", "vmid": 110, "name": "kali", "macs": [], "nics": [], "status": "stopped"}]
    n = topology.build(None, None, None, pve)
    assert n["guest:pve/120"]["offline"] and n["guest:pve/120"]["detail"] == "stopped"
    assert not n["guest:pve/101"].get("offline")
    host = topology.layers(n)["host"]
    assert [k["name"] for k in host["kids"]] == ["homeassistant", "kali", "Win11"]  # running first


# ---- 1.0.1: stray MACs on an access point's port ---------------------------------------------------------------
def test_a_bare_mac_on_an_ap_port_is_left_off_the_map():
    sw1 = SW1 | {"ports": [p | {"comment": "Hall AP"} if p["name"] == "ether4" else p for p in SW1["ports"]],
                 "hosts": SW1["hosts"] + [("AA:AA:03:00:00:00", "ether4"), ("DE:AD:00:00:00:07", "ether4"),
                                          ("DE:AD:00:00:00:08", "ether2")]}
    sw2 = SW2 | {"hosts": SW2["hosts"] + [("AA:AA:03:00:00:00", "ether1"), ("DE:AD:00:00:00:07", "ether1"),
                                          ("DE:AD:00:00:00:08", "ether1")]}  # the other switch: on its uplink
    n = topology.build([sw1, sw2], UNIFI, HA, PVE, addresses=[{"mac": "de:ad:00:00:00:07", "ip": "192.168.2.70"}])
    assert "mac:AA:AA:03:00:00:00" not in n  # no name, no address, not a Wi-Fi client: a garbled frame
    assert n["mac:DE:AD:00:00:00:07"]["parent"] == "port:core/ether4"  # it has an address: a real device
    assert n["mac:DE:AD:00:00:00:07"]["name"] != "Hall AP"  # the AP port's comment names the AP, not others
    assert n["mac:DE:AD:00:00:00:08"]["parent"] == "port:core/ether2"  # other ports: unchanged
    assert n["mac:CC:00:00:00:00:01"]["parent"] == "mac:BB:00:00:00:00:01"


def test_stray_macs_remembered_before_are_forgotten():
    n = built()
    seen = {"mac:AA:AA:03:00:00:00": {"parent": "port:core/ether4", "via": "wired", "name": "Hall AP", "ip": "",
                                      "ts": 900.0},
            "mac:DE:AD:00:00:00:09": {"parent": "port:lan/ether2", "via": "wired", "name": "", "ip": "", "ts": 900.0}}
    n = topology.remember(n, seen, 1000.0)
    assert "mac:AA:AA:03:00:00:00" not in seen and "mac:AA:AA:03:00:00:00" not in n
    assert n["mac:DE:AD:00:00:00:09"]["offline"]  # a wired device elsewhere stays, in red
