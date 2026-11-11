"""New devices on the network: a baseline first, then each device never seen before is pointed out once."""

import time

from homewatch import newdevices, topology
from tests.test_topology import SW1, UNIFI, built
from tests.test_web import client, setup_admin  # noqa: F401 - the fixture


def test_baseline_then_new_ones(db):
    n = built()
    assert newdevices.update(db, "home", n, 1.0, ["aa:00:00:00:00:99"]) == []  # first run: everything is known
    assert db.one("SELECT COUNT(*) n FROM known_devices WHERE acked IS NOT NULL")["n"] > 5
    unifi = UNIFI | {"clients": UNIFI["clients"] + [{"mac": "d0:ad:be:ef:00:01", "name": "guest-laptop",
                                                     "ap_mac": "bb:00:00:00:00:01", "wired": False, "ip": "192.168.2.177"},
                                                    {"mac": "6a:00:00:00:00:01", "name": "", "ap_mac": "bb:00:00:00:00:01",
                                                     "wired": False}]}
    n2 = topology.build([SW1, __import__("tests.test_topology", fromlist=["SW2"]).SW2], unifi, None, None)
    new = newdevices.update(db, "home", n2, 2.0)
    got = {d["node"]: d for d in new}
    assert set(got) == {"mac:D0:AD:BE:EF:00:01", "mac:6A:00:00:00:00:01"}
    assert got["mac:D0:AD:BE:EF:00:01"]["place"] == "Hall AP (Wi-Fi)" and not got["mac:D0:AD:BE:EF:00:01"]["private"]
    assert got["mac:6A:00:00:00:00:01"]["private"]
    assert n2["mac:D0:AD:BE:EF:00:01"]["new"]  # marked on the map until it is known
    assert newdevices.update(db, "home", n2, 3.0) == []  # once only
    ev = newdevices.events(new, 2.0)
    assert len(ev) == 2 and ev[0].level == 1 and "guest-laptop · 192.168.2.177 · D0:AD:BE:EF:00:01 · on Hall AP (Wi-Fi)" in [
        e.detail for e in ev][0] + [e.detail for e in ev][1]


def test_status_box_and_known(client):  # noqa: F811
    tok = setup_admin(client)
    rt = client.rt
    now = time.time()
    rt.db.run("INSERT INTO known_devices(site,node,first_seen,last_seen,name,ip,place) VALUES('home','mac:D0:AD:BE:EF:00:01',"
              "?,?,'guest-laptop','192.168.2.177','Hall AP (Wi-Fi)')", (now, now))
    rt.db.run("INSERT INTO known_devices(site,node,first_seen,last_seen,name,ip,place) VALUES('home','mac:6A:00:00:00:00:01',"
              "?,?,'6A:00:00:00:00:01','','Hall AP (Wi-Fi)')", (now, now))
    page = client.get("/").text
    assert "New on the network" in page and "guest-laptop" in page and "private Wi-Fi address" in page
    client.post("/devices/known", data={"csrf": tok, "node": "mac:D0:AD:BE:EF:00:01"})
    page = client.get("/").text
    assert "guest-laptop" not in page and "New on the network" in page
    client.post("/devices/known", data={"csrf": tok, "node": "*"})
    assert "New on the network" not in client.get("/").text


# ---- 1.0.1: stray MACs on a switch port wait; guests at the captive portal count at once -----------------------
def _with(hosts=(), clients=()):
    from tests.test_topology import SW2

    sw1 = SW1 | {"hosts": SW1["hosts"] + list(hosts)}
    sw2 = SW2 | {"hosts": SW2["hosts"] + [(m, "ether1") for m, _ in hosts]}  # the other switch: on its uplink
    return topology.build([sw1, sw2], UNIFI | {"clients": UNIFI["clients"] + list(clients)}, None, None)


def test_a_stray_mac_on_a_switch_port_must_stay_a_while(db):
    newdevices.update(db, "home", built(), 1.0)  # baseline
    stray = [("00:00:01:10:00:00", "ether2")]
    assert newdevices.update(db, "home", _with(stray), 100.0) == []  # once: not yet
    assert newdevices.update(db, "home", _with(), 300.0) == []  # gone again
    assert newdevices.update(db, "home", _with(stray), 400.0) == []  # 2 readings, 5 minutes
    assert newdevices.update(db, "home", _with(stray), 500.0) == []  # 3 readings but only 6.7 minutes
    (d,) = newdevices.update(db, "home", _with(stray), 700.0)  # 4 readings over 10 minutes: a real device
    assert d["node"] == "mac:00:00:01:10:00:00"
    assert db.one("SELECT first_seen FROM known_devices WHERE node=?", (d["node"],))["first_seen"] == 100.0
    assert db.get(newdevices.WAITING) == {}


def test_a_waiting_mac_is_forgotten_and_junk_never_counts(db):
    newdevices.update(db, "home", built(), 1.0)
    newdevices.update(db, "home", _with([("64:00:11:04:00:00", "ether2")]), 100.0)
    assert "mac:64:00:11:04:00:00" in db.get(newdevices.WAITING)
    newdevices.update(db, "home", _with(), 100.0 + newdevices.FORGET_S + 1)
    assert db.get(newdevices.WAITING) == {}
    junk = [("01:00:5E:00:00:FB", "ether2"), ("33:33:00:00:00:01", "ether2"), ("00:00:00:00:00:00", "ether2")]
    for t in (2000.0, 2400.0, 2800.0, 3200.0):
        assert newdevices.update(db, "home", _with(junk), t) == []
    assert db.get(newdevices.WAITING) == {}


def test_a_switch_mac_with_an_address_counts_at_once(db):
    newdevices.update(db, "home", built(), 1.0)
    n = topology.build([SW1 | {"hosts": SW1["hosts"] + [("DE:AD:00:00:00:09", "ether2")]}], UNIFI, None, None,
                       addresses=[{"mac": "de:ad:00:00:00:09", "ip": "192.168.2.150"}])
    (d,) = newdevices.update(db, "home", n, 50.0)
    assert d["ip"] == "192.168.2.150"


def test_guests_at_the_captive_portal(db):
    newdevices.update(db, "home", built(), 1.0)
    guest = {"mac": "5e:8d:80:16:65:5d", "name": "Galaxy-A16", "ap_mac": "bb:00:00:00:00:01", "wired": False,
             "ip": "192.168.11.225", "guest": True, "authorized": False}
    n = _with(clients=[guest])
    (d,) = newdevices.update(db, "home", n, 50.0)  # at once, even before the voucher
    assert d["guest_wifi"] == "waiting at the portal" and d["private"]
    (ev,) = newdevices.events([d], 50.0)
    assert "guest Wi-Fi, waiting at the portal" in ev.detail
    db.set("topology.map", _with(clients=[guest | {"authorized": True}]))
    assert newdevices.pending(db, "home")[0]["guest_wifi"] == "passed the portal"
    assert newdevices.update(db, "home", _with(clients=[guest | {"authorized": True}]), 60.0) == []  # still once only


def test_a_guest_without_portal_state_is_just_a_guest(db):
    newdevices.update(db, "home", built(), 1.0)
    g = {"mac": "d0:ad:be:ef:00:07", "name": "visitor", "ap_mac": "bb:00:00:00:00:01", "wired": False,
         "ip": "192.168.11.30", "guest": True, "authorized": None}
    (d,) = newdevices.update(db, "home", _with(clients=[g]), 50.0)
    (ev,) = newdevices.events([d], 50.0)
    assert "guest Wi-Fi" in ev.detail and "portal" not in ev.detail


def test_a_bare_mac_on_an_access_points_port_never_counts(db):
    newdevices.update(db, "home", built(), 1.0)
    stray = [("AA:AA:03:00:00:00", "ether4")]  # ether4: the AP's port; UniFi doesn't know it, no address
    for t in (100.0, 400.0, 700.0, 1000.0, 7000.0):
        assert newdevices.update(db, "home", _with(stray), t) == []
    assert db.get(newdevices.WAITING) == {}
