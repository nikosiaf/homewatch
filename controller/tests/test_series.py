"""Detailed metrics for Grafana: every module turns its latest reading into valid line protocol."""

import asyncio
import re

from homewatch.metrics import lines, num

LINE = re.compile(r"^hw_[a-z_]+(,(?:[^ \\]|\\.)+)? [a-z0-9_]+=[-0-9.e+]+(,[a-z0-9_]+=[-0-9.e+]+)* \d+$")


def valid(out):
    assert out, "no lines"
    for x in out:
        assert LINE.match(x), x
    return out


def test_lines_skip_missing_and_bad_values():
    out = lines("hw_x", [({"site": "home", "dev": "a b"}, {"v": 1, "n": None, "s": "text", "nan": float("nan")}),
                         ({"site": "home"}, {"n": None})], 10)
    assert out == ["hw_x,dev=a\\ b,site=home v=1.0 10000000000"]
    assert num("7.5") == 7.5 and num("N/A") is None and num(None) is None and num(True) == 1.0


def test_mikrotik_series():
    from homewatch.modules.mikrotik.plugin import MikroTikModule

    m = MikroTikModule()
    m.snap, m.snap_ts = {"devices": [{"host": "192.0.2.2", "name": "mt2", "model": "RB2011", "cpu": 4, "mem": 40,
                                      "temp": 41.0, "voltage": 24.1, "uptime": 100.0,
                                      "ports": [{"name": "ether1", "comment": "uplink", "running": True, "disabled": False,
                                                 "errors": 0, "rx_bytes": 1e9, "tx_bytes": 2e9, "rx_drops": 0,
                                                 "tx_drops": None}]}], "errors": {}}, 1000
    out = valid(m.series("home"))
    assert any(x.startswith("hw_mikrotik_device,") and "cpu_percent=4.0" in x for x in out)
    assert any(x.startswith("hw_mikrotik_port,") and "comment=uplink" in x and "rx_bytes=1000000000.0" in x for x in out)
    assert m.series("home") and MikroTikModule().series("home") == []  # no reading: nothing


def test_proxmox_series():
    from homewatch.modules.proxmox.client import Snapshot
    from homewatch.modules.proxmox.plugin import ProxmoxModule

    m = ProxmoxModule()
    m.snap = Snapshot(nodes=[{"node": "pve", "status": "online", "cpu": 0.05, "maxcpu": 8, "mem": 4, "maxmem": 8}],
                      guests=[{"vmid": 101, "name": "ha", "type": "qemu", "node": "pve", "status": "running", "cpu": 0.5,
                               "netin": 10, "netout": 20, "disk": 0, "maxdisk": 32}],
                      storages=[{"storage": "local", "node": "pve", "status": "available", "disk": 1, "maxdisk": 4}],
                      disks={"pve": [{"devpath": "/dev/sda", "health": "PASSED", "wearout": "N/A"}]},
                      backups={101: {"ok": 900.0, "failed": None, "error": ""}})
    m.snap_ts = 1000
    out = valid(m.series("home"))
    assert any(x.startswith("hw_proxmox_node,") and "cpu_percent=5.0" in x for x in out)
    guest = next(x for x in out if x.startswith("hw_proxmox_guest,"))
    assert "cpu_percent=50.0" in guest and "disk_used_bytes" not in guest  # 0 = not reported for VMs
    assert any(x.startswith("hw_proxmox_disk,") and "healthy=1.0" in x and "life_left" not in x for x in out)
    assert any(x.startswith("hw_proxmox_backup,") and "last_ok_timestamp=900.0" in x for x in out)


def test_ups_series_estimates_power():
    from homewatch.modules.ups.plugin import UPSModule

    m = UPSModule()
    m.snap, m.snap_ts = {"name": "apc", "vars": {"ups.status": "OL CHRG", "battery.charge": "100", "ups.load": "12",
                                                  "ups.realpower.nominal": "900"}}, 1000
    (x,) = valid(m.series("home"))
    assert "power_watts=108.0" in x and "online=1.0" in x and "on_battery=0.0" in x


def test_opnsense_series():
    from homewatch.modules.opnsense.plugin import OPNsenseModule

    m = OPNsenseModule()
    m.snap, m.snap_ts = {
        "info": {"name": "fw"}, "activity": {"headers": ["CPU:  0.5% user, 99.0% idle"]},
        "resources": {"memory": {"total": "8000", "used": 4000}}, "disk": {"devices": [
            {"mountpoint": "/", "used_bytes": 1, "total_bytes": 4}, {"mountpoint": "/boot", "used_bytes": 1}]},
        "gateways": [{"name": "WAN", "status_translated": "Online", "delay": "8.6 ms", "loss": "0.0 %", "stddev": ""}],
        "traffic": {"wan": {"name": "WAN", "device": "pppoe0", "bytes received": "5", "bytes transmitted": "6"}},
        "haproxy": [{"pxname": "ha", "svname": "ha", "status": "UP", "scur": "2"}, {"pxname": "ha", "svname": "BACKEND"},
                    {"pxname": "nas", "svname": "nas", "status": "DOWN"}],
        "time": {"uptime": "7 days, 21:07:59"},
        "services": [{"name": "unbound", "running": 1}]}, 1000
    out = valid(m.series("home"))
    assert any(x.startswith("hw_opnsense_system,") and "cpu_percent=1.0" in x for x in out)
    assert sum(x.startswith("hw_opnsense_disk,") for x in out) == 1
    assert any(x.startswith("hw_opnsense_gateway,") and "delay_ms=8.6" in x and "stddev" not in x for x in out)
    assert [("down=1.0" in x) for x in out if x.startswith("hw_opnsense_haproxy,")] == [False, True]
    m.skipped = {"nas/nas"}  # unticked under "HAProxy servers to watch" (the check sets it)
    assert [("watched=1.0" in x) for x in valid(m.series("home")) if x.startswith("hw_opnsense_haproxy,")] == [True, False]
    assert any("uptime_seconds=" + str(7 * 86400 + 21 * 3600 + 7 * 60 + 59.0) in x for x in out)


def test_opnsense_uptime_text():
    from homewatch.modules.opnsense.plugin import uptime_s

    assert uptime_s("1 day") == 86400 and uptime_s("1 day, 03:12") == 86400 + 3 * 3600 + 12 * 60
    assert uptime_s("21:07:59") == 21 * 3600 + 7 * 60 + 59 and uptime_s("5 mins") == 300
    assert uptime_s("") is None and uptime_s("soon") is None and uptime_s(None) is None


def test_other_modules_series():
    from homewatch.modules.docker.plugin import DockerModule
    from homewatch.modules.pbs.plugin import PBSModule
    from homewatch.modules.synology.plugin import SynologyModule
    from homewatch.modules.unifi.plugin import UniFiModule

    syn = SynologyModule()
    syn.snap, syn.snap_ts = {"info": {"model": "DS920+", "sys_temp": 45}, "crashed": False,
                             "cpu": {"user_load": 3, "system_load": 2}, "mem": 40, "update": False,
                             "net": [{"device": "total", "rx": 1, "tx": 2}, {"device": "eth0", "rx": 1, "tx": 2}],
                             "diskio": {"read_byte": 10}, "hw": {"system": 1, "cpu_fan": None, "power": 2},
                             "disks": [{"name": "Drive 1", "temp": 26, "status": "normal", "smart_status": "normal"}],
                             "volumes": [{"path": "/volume1", "used": "1", "total": "4", "status": "normal"}]}, 1000
    out = valid(syn.series("home"))
    system = out[0]
    assert "cpu_percent=5.0" in system and "system_ok=1.0" in system and "power_ok=0.0" in system
    assert "cpu_fan_ok" not in system and sum(x.startswith("hw_synology_net,") for x in out) == 1

    uni = UniFiModule()
    uni.snap, uni.snap_ts = {"devices": [{"name": "AP", "mac": "aa", "state": 1, "clients": 5, "cpu": 3.0, "radios": [
        {"radio": "na", "clients": 4.0, "channel": 36.0, "use": 12.0}]}], "health": [{"subsystem": "wlan", "status": "ok",
                                                                                      "users": 9}]}, 1000
    uni.snap["health"].append({"subsystem": "wan", "status": "unknown", "users": None})  # no UniFi gateway
    out = valid(uni.series("home"))
    assert any(x.startswith("hw_unifi_radio,") and "radio=na" in x for x in out)
    assert [x for x in out if x.startswith("hw_unifi_health,")] and not any("subsystem=wan" in x for x in out)

    pbs = PBSModule()
    pbs.snap, pbs.snap_ts = {"stores": [{"store": "s", "used": 1, "total": 4}], "groups": [
        {"store": "s", "type": "vm", "id": "101", "last": 900, "count": 7, "comment": "ha"}],
        "jobs": [{"type": "verificationjob", "end": 990, "status": "OK"}], "updates": 0}, 1000
    out = valid(pbs.series("home"))
    assert any(x.startswith("hw_pbs_group,") and "snapshots=7.0" in x for x in out)

    dock = DockerModule()
    dock.snap, dock.snap_ts = {"hosts": [{"name": "h", "containers": [
        {"project": "p", "name": "c", "image": "i:1", "state": "running", "health": "", "restarts": 2}]}]}, 1000
    (x,) = valid(dock.series("home"))
    assert "running=1.0" in x and "healthy" not in x


def test_runtime_writes_each_reading_once(cfg, monkeypatch):
    from homewatch.modules.registry import MODULES
    from homewatch.modules.ups.plugin import UPSModule

    from homewatch.runtime import Runtime

    rt = Runtime.create(cfg)
    written = []

    async def write(ls):
        written.extend(ls)

    monkeypatch.setattr(rt.metrics, "write", write)
    ups = next(m for m in MODULES.values() if isinstance(m, UPSModule))

    async def readings(rt_, s, now):
        ups.snap, ups.snap_ts = {"name": "apc", "vars": {"ups.status": "OL", "battery.charge": "99"}}, 500.0
        return (s, ups.snap, "", now)

    monkeypatch.setattr(ups, "readings", readings)
    monkeypatch.setattr(ups, "load", lambda rt_: {"enabled": True, "_configured": True, "chosen": [], "skipped": []})
    asyncio.run(rt.run_checks(1000))
    asyncio.run(rt.run_checks(1100))  # same reading (500): not written again
    assert sum(x.startswith("hw_ups,") for x in written) == 1


def test_ha_entity_values():
    from homewatch.modules.homeassistant.plugin import entity_values

    assert entity_values({"entity_id": "sensor.t", "state": "21.5"}) == {"value": 21.5}
    assert entity_values({"entity_id": "binary_sensor.door", "state": "open"}) == {"value": 1.0}
    assert entity_values({"entity_id": "sensor.x", "state": "unavailable"}) == {}
    assert entity_values({"entity_id": "climate.h", "state": "heat"}) == {"value": 1.0}
    assert entity_values({"entity_id": "climate.house", "state": "heat", "attributes": {
        "current_temperature": 20.5, "temperature": 21, "hvac_action": "heating"}}) == {
        "value": 1.0, "current_temperature": 20.5, "target_temperature": 21.0, "active": 1.0}
    assert entity_values({"entity_id": "light.tv", "state": "on", "attributes": {"brightness": 255}}) == {
        "value": 1.0, "brightness_percent": 100.0}



def test_runtime_writes_confirmed_status(cfg, monkeypatch):
    """One bad reading that the next one takes back is not a problem in Grafana either (as on the Status page)."""
    from homewatch.modules.registry import MODULES
    from homewatch.modules.ups.plugin import UPSModule

    from homewatch.runtime import Runtime

    rt = Runtime.create(cfg)
    written = []

    async def write(ls):
        written.extend(ls)

    monkeypatch.setattr(rt.metrics, "write", write)
    ups = next(m for m in MODULES.values() if isinstance(m, UPSModule))
    status = {"v": "OB"}

    async def readings(rt_, s, now):
        ups.snap, ups.snap_ts = {"name": "apc", "vars": {"ups.status": status["v"], "battery.charge": "99"}}, now
        return (s, ups.snap, "", now)

    monkeypatch.setattr(ups, "readings", readings)
    monkeypatch.setattr(ups, "load", lambda rt_: {"enabled": True, "_configured": True, "chosen": [], "skipped": []})

    def levels():
        out = [x for x in written if x.startswith("hw_check_status,") and "check=ups.power" in x]
        written.clear()
        return out

    rt.db.run("INSERT INTO check_state(site,check_key,status,since) VALUES(?,?,0,0)", (cfg.site, "ups.power"))
    asyncio.run(rt.run_checks(1000))  # on battery once: not confirmed yet
    (x,) = levels()
    assert "value=0" in x
    asyncio.run(rt.run_checks(1100))  # twice: confirmed
    (x,) = levels()
    assert "value=0" not in x


def _check_now_rt(cfg, monkeypatch, statuses):
    """A runtime with only the UPS module on; its readings give the next status of `statuses` each time."""
    from homewatch import runtime
    from homewatch.modules.registry import MODULES
    from homewatch.modules.ups.plugin import UPSModule

    rt = runtime.Runtime.create(cfg)

    async def write(ls):
        pass

    monkeypatch.setattr(rt.metrics, "write", write)
    monkeypatch.setattr(runtime, "CHECK_NOW_CONFIRM", 0)
    monkeypatch.setattr(MODULES["internet"], "enabled", lambda s: False)
    ups = next(m for m in MODULES.values() if isinstance(m, UPSModule))
    reads = []

    async def readings(rt_, s, now):
        reads.append(ups.stale)
        ups.stale = False
        ups.snap = {"name": "apc", "vars": {"ups.status": statuses.pop(0), "battery.charge": "99"}}
        ups.snap_ts = now + len(reads)  # every read is a new reading
        return (s, ups.snap, "", now)

    monkeypatch.setattr(ups, "readings", readings)
    monkeypatch.setattr(ups, "load", lambda rt_: {"enabled": True, "_configured": True, "chosen": [], "skipped": []})
    rt.db.run("INSERT INTO check_state(site,check_key,status,since) VALUES(?,?,0,0)", (cfg.site, "ups.power"))
    return rt, reads


def _ups_power(rt):
    return rt.db.one("SELECT status, pending FROM check_state WHERE site=? AND check_key='ups.power'", (rt.cfg.site,))


def test_check_now_confirms_a_change_in_one_click(cfg, monkeypatch):
    rt, reads = _check_now_rt(cfg, monkeypatch, ["OB", "OB"])
    assert asyncio.run(rt.check_now()) is True
    assert reads == [True, True]  # read fresh, then once more for the change
    st = _ups_power(rt)
    assert st["status"] != 0 and st["pending"] is None  # confirmed: shown on the Status page now
    assert rt.db.one("SELECT 1 FROM issues WHERE check_key='ups.power' AND resolved IS NULL")


def test_check_now_one_bad_reading_is_taken_back(cfg, monkeypatch):
    rt, reads = _check_now_rt(cfg, monkeypatch, ["OB", "OL"])
    asyncio.run(rt.check_now())
    assert len(reads) == 2
    st = _ups_power(rt)
    assert st["status"] == 0 and st["pending"] is None
    assert not rt.db.one("SELECT 1 FROM issues WHERE check_key='ups.power'")


def test_check_now_no_change_reads_once(cfg, monkeypatch):
    rt, reads = _check_now_rt(cfg, monkeypatch, ["OL"])
    asyncio.run(rt.check_now())
    assert reads == [True]


def test_check_now_again_within_the_gap_is_an_ordinary_run(cfg, monkeypatch):
    rt, reads = _check_now_rt(cfg, monkeypatch, ["OL", "OL"])
    assert asyncio.run(rt.check_now()) is True
    assert asyncio.run(rt.check_now()) is False
    assert reads == [True, False]  # the second click didn't force a device read
