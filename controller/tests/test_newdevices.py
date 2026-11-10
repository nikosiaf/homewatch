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
