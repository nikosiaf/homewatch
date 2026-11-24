"""A reboot that came with a firmware update (the version differs from before it) is planned: named, not counted."""

from zoneinfo import ZoneInfo

from homewatch import causes, topology
from homewatch.modules import OK, WARNING, Thresholds, booted, planned_update, reboots
from tests.test_topology import _store

T0 = 1_800_000_000.0
UTC = ZoneInfo("UTC")
FW = "MikroTik1 restarted: firmware update RouterOS 7.25.1 -> 7.25.2 (planned)"


def boot_twice(before, after):
    """One reading with the versions before, a reboot, one reading with the versions after: the events."""
    prev, out = {}, []
    booted(prev, "sw:boot", T0, "MikroTik1", "MikroTik1", out, before)
    assert out == []  # the first reading only remembers
    booted(prev, "sw:boot", T0 + 3600, "MikroTik1", "MikroTik1", out, after)
    return out


def test_version_changed_reboot_is_a_planned_firmware_update():
    (e,) = boot_twice({"RouterOS": "7.25.1", "RouterBOOT": "7.25.1"}, {"RouterOS": "7.25.2", "RouterBOOT": "7.25.1"})
    assert (e.kind, e.detail) == ("reboot", FW)
    assert planned_update(e.detail) == "firmware update RouterOS 7.25.1 -> 7.25.2"
    (e,) = boot_twice({"RouterOS": "7.25.2", "RouterBOOT": "7.25.1"}, {"RouterOS": "7.25.2", "RouterBOOT": "7.25.2"})
    assert e.detail == "MikroTik1 restarted: firmware update RouterBOOT 7.25.1 -> 7.25.2 (planned)"


def test_version_unchanged_reboot_is_an_ordinary_one():
    (e,) = boot_twice({"RouterOS": "7.25.2", "RouterBOOT": "7.25.2"}, {"RouterOS": "7.25.2", "RouterBOOT": "7.25.2"})
    assert e.detail == "MikroTik1 restarted" and planned_update(e.detail) == ""


def test_version_not_read_tells_nothing():
    (e,) = boot_twice({"RouterOS": "7.25.1"}, {"RouterOS": None, "RouterBOOT": ""})  # not read after the reboot
    assert e.detail == "MikroTik1 restarted"
    (e,) = boot_twice({"RouterOS": None}, {"RouterOS": "7.25.2"})  # not read before it
    assert e.detail == "MikroTik1 restarted"
    (e,) = boot_twice(None, {"RouterOS": "7.25.2"})  # never seen before the reboot (an older homeWatch)
    assert e.detail == "MikroTik1 restarted"


def test_a_version_not_read_keeps_the_one_seen_before():
    prev, out = {}, []
    booted(prev, "k", T0, "sw", "sw", out, {"RouterOS": "7.25.1"})
    booted(prev, "k", T0, "sw", "sw", out, {"RouterOS": None})  # a reading without it
    booted(prev, "k", T0 + 3600, "sw", "sw", out, {"RouterOS": "7.25.2"})
    assert [e.detail for e in out] == ["sw restarted: firmware update RouterOS 7.25.1 -> 7.25.2 (planned)"]


def test_planned_reboots_are_not_counted():
    t, now = Thresholds(2, None), T0
    planned = [{"ts": now - 600, "device": "MikroTik1", "kind": "reboot", "detail": FW},
               {"ts": now - 300, "device": "MikroTik2", "kind": "reboot",
                "detail": "MikroTik2 restarted: firmware update RouterOS 7.25.1 -> 7.25.2 (planned)"}]
    status, detail, n = reboots(planned, [(300, "MikroTik2")], t, now)
    assert (status, n) == (OK, 0)
    assert detail == ("no unplanned reboots in 24 h; planned: firmware update on MikroTik1 10 min ago, "
                      "MikroTik2 5 min ago")
    plain = [{"ts": now - 60, "device": "MikroTik1", "kind": "reboot", "detail": "MikroTik1 restarted"}]
    status, detail, n = reboots(planned + plain, [], t, now)
    assert (status, n) == (OK, 1) and detail.startswith("1 reboot in 24 h: MikroTik1 1 min ago; planned:")
    status, _, n = reboots(planned + plain * 2, [], t, now)
    assert (status, n) == (WARNING, 2)
    # events without a detail (an older caller) still count as before
    assert reboots([{"ts": now - 60, "device": "sw", "kind": "reboot"}], [], t, now)[2] == 1


def test_mikrotik_records_the_firmware_update(monkeypatch):
    from homewatch.modules.registry import MODULES

    mt = MODULES["mikrotik"]
    dev = {"host": "10.0.0.2", "name": "MikroTik1", "uptime": 86400.0, "version": "7.25.1 (stable)", "boot": "7.25.1",
           "ports": []}
    monkeypatch.setattr(mt, "snap", {"devices": [dev]})
    monkeypatch.setattr(mt, "snap_ts", T0)
    prev = {}
    assert mt.device_events(None, prev) == []
    dev.update(uptime=60.0, version="7.25.2 (stable)")
    monkeypatch.setattr(mt, "snap_ts", T0 + 7200)
    (e,) = mt.device_events(None, prev)
    assert e.detail == FW
    dev.update(uptime=30.0, version=None, boot=None)  # rebooted again, versions not read: ordinary
    monkeypatch.setattr(mt, "snap_ts", T0 + 9000)
    (e,) = mt.device_events(None, prev)
    assert e.detail == "MikroTik1 restarted"


def test_mikrotik_reboots_check_is_ok_after_a_firmware_update():
    from homewatch.modules.mikrotik.plugin import MikroTikModule

    from tests.test_mikrotik import NOW, dev, run

    m = MikroTikModule()
    fw = "restarted: firmware update RouterOS 7.25.1 -> 7.25.2 (planned)"
    m.events_24h = tuple({"ts": NOW - 600, "device": n, "kind": "reboot", "detail": f"{n} {fw}"} for n in ("sw1", "sw2"))
    r = run(m, {"devices": [dev("sw1", uptime=600), dev("sw2", uptime=600)], "errors": {}}, [])["reboots"]
    assert r.status == OK and r.value == 0 and "planned: firmware update on sw1" in r.detail


def ev(db, ts, module, device, detail):
    db.run("INSERT INTO events(site,ts,module,device,kind,detail,exact,seen) VALUES('home',?,?,?,'reboot',?,1,?)",
           (ts, module, device, detail, ts))


def test_incident_cause_is_the_planned_update(db):
    ev(db, T0 - 60, "mikrotik", "MikroTik1", FW)
    cause, side, _ = causes.classify(db, "home", T0, T0 + 300, ["homeassistant.addons"], UTC)
    assert cause == "Caused by planned firmware update RouterOS 7.25.1 -> 7.25.2 on MikroTik1 at 07:59"
    assert side == causes.HOME


def test_firewall_planned_update_cause(db):
    ev(db, T0 - 60, "opnsense", "fw", "Firewall fw restarted: firmware update OPNsense 25.7.1 -> 25.7.2 (planned)")
    cause, _, _ = causes.classify(db, "home", T0, T0 + 300, ["internet.reachable"], UTC)
    assert cause == "Caused by planned firmware update OPNsense 25.7.1 -> 25.7.2 on fw at 07:59"


def test_parent_planned_update_cause_and_issue_detail(db):
    _store(db)
    ev(db, T0 - 60, "mikrotik", "core", "core restarted: firmware update RouterOS 7.25.1 -> 7.25.2 (planned)")
    cause, side, _ = causes.classify(db, "home", T0, T0 + 600, ["homeassistant.unavailable"], UTC,
                                     ["mac:CC:00:00:00:00:02", "mac:DE:AD:00:00:00:01"])
    assert cause == ("Caused by planned firmware update RouterOS 7.25.1 -> 7.25.2 on core at 07:59, "
                     "and 2 devices behind it went with it")
    assert side == causes.HOME
    why, held = topology.behind(db, "home", ["mac:DE:AD:00:00:00:01"], T0 + 120)
    assert why == "behind core: caused by planned firmware update RouterOS 7.25.1 -> 7.25.2 3 min ago" and not held


def test_unifi_and_opnsense_pass_their_versions(monkeypatch):
    from homewatch.modules.registry import MODULES

    ap = MODULES["unifi"]
    d = {"mac": "aa", "name": "Hall AP", "state": 1, "uptime": 86400, "version": "6.6.77"}
    monkeypatch.setattr(ap, "snap", {"devices": [d]})
    monkeypatch.setattr(ap, "snap_ts", T0)
    prev = {}
    assert ap.device_events(None, prev) == []
    d.update(uptime=60, version="6.7.10")
    monkeypatch.setattr(ap, "snap_ts", T0 + 3600)
    assert [e.detail for e in ap.device_events(None, prev)] == [
        "Hall AP restarted: firmware update UniFi 6.6.77 -> 6.7.10 (planned)"]

    fw = MODULES["opnsense"]
    snap = {"info": {"name": "fw", "versions": ["OPNsense 25.7.1-amd64", "FreeBSD 14.3"]},
            "time": {"uptime": "7 days, 01:00:00"}, "gateways": []}
    monkeypatch.setattr(fw, "snap", snap)
    monkeypatch.setattr(fw, "snap_ts", T0)
    prev = {}
    assert fw.device_events(None, prev) == []
    snap.update(info={"name": "fw", "versions": ["OPNsense 25.7.2-amd64"]}, time={"uptime": "00:03"})
    monkeypatch.setattr(fw, "snap_ts", T0 + 600)
    assert [e.detail for e in fw.device_events(None, prev) if e.kind == "reboot"] == [
        "Firewall fw restarted: firmware update OPNsense 25.7.1-amd64 -> 25.7.2-amd64 (planned)"]
