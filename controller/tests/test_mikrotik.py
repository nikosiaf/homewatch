"""MikroTik: uptime parsing, several devices, ports, new port errors, reboots, RouterBOOT."""

import time

from homewatch.modules import ERROR, OK, WARNING, Thresholds
from homewatch.modules.mikrotik.plugin import MikroTikModule, uptime_s

NOW = time.time()


def test_uptime_parse():
    assert uptime_s("1w18h13m33s") == 604800 + 18 * 3600 + 13 * 60 + 33 and uptime_s("45s") == 45


def dev(name, **over):
    d = {"host": name, "name": name, "model": "RB2011", "version": "7.24.5 (stable)", "cpu": 5, "mem": 40,
         "uptime": 5 * 86400, "temp": 30, "voltage": 24, "boot": "7.24.5", "boot_upgrade": "7.24.5",
         "ports": [{"name": "ether1", "comment": "UpLink", "running": True, "disabled": False, "errors": 0},
                   {"name": "ether2", "comment": "", "running": False, "disabled": False, "errors": 0}]}
    d.update(over)
    return d


def run(m, snap, chosen):
    s = {"enabled": True, "chosen": chosen}
    th = {d.id: Thresholds(d.warn, d.error, d.default_on) for d in m.checks}
    return {r.key.split(".")[1]: r for r in m.evaluate(s, th, (s, snap, "", NOW))}


def test_healthy_and_default_choice():
    m = MikroTikModule()
    snap = {"devices": [dev("sw1"), dev("sw2")], "errors": {}, "_summary": "2"}
    assert m.default_chosen(snap) == ["sw1/ether1", "sw2/ether1"]
    res = run(m, snap, m.default_chosen(snap))
    assert all(r.status == OK for r in res.values()), {k: r.detail for k, r in res.items()}


def test_port_down_errors_reboot_and_firmware():
    m = MikroTikModule()
    first = {"devices": [dev("sw1")], "errors": {}}
    run(m, first, ["sw1/ether1"])  # remembers the error counters
    ports = [{"name": "ether1", "comment": "UpLink", "running": False, "disabled": False, "errors": 50}]
    snap = {"devices": [dev("sw1", ports=ports, uptime=600, boot="6.49")], "errors": {"sw2": "timeout"}}
    res = run(m, snap, ["sw1/ether1"])
    assert res["ports"].status == ERROR and "UpLink" in res["ports"].detail
    assert res["errors"].status == WARNING and res["errors"].value == 50
    assert res["uptime"].status == WARNING and "rebooted 10 min ago" in res["uptime"].detail
    assert res["firmware"].status == WARNING and res["each"].status == ERROR
