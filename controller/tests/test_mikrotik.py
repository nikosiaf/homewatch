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


def test_port_errors_count_the_last_hour():
    from homewatch.modules.mikrotik.plugin import MikroTikModule
    m = MikroTikModule()
    th = {d.id: Thresholds(d.warn, d.error, d.default_on) for d in m.checks}
    th["errors"] = Thresholds(1, None)

    def at(t, n):
        ports = [{"name": "ether1", "comment": "", "running": True, "disabled": False, "errors": n}]
        snap = {"devices": [dev("sw1", ports=ports)], "errors": {}}
        return {r.key.split(".")[1]: r for r in m.check({"chosen": []}, th, snap, t)}["errors"]

    assert at(0, 100).status == OK  # first reading: the baseline
    assert at(1200, 103).value == 3 and at(1200, 103).status == WARNING
    assert at(3700, 103).value == 3  # the baseline is still the reading at or before the hour's start
    assert at(5000, 103).value == 0  # an hour without new errors
    assert at(5100, 2).value == 0  # counters restarted (reboot)


def _client_kwargs(monkeypatch):
    import httpx

    seen = {}
    real = httpx.AsyncClient

    def make(**kw):
        seen.update(kw)
        return real(transport=httpx.MockTransport(lambda r: httpx.Response(401)), **kw)

    monkeypatch.setattr(httpx, "AsyncClient", make)
    return seen


def test_https_with_certificate_check_is_the_default(monkeypatch):
    import asyncio

    from homewatch.modules.apibase import ApiError
    from homewatch.modules.mikrotik.plugin import MikroTikModule

    m = MikroTikModule()
    defaults = {f.key: f.default for f in m.fields}
    assert defaults["https"] is True and defaults["verify"] is True
    seen = _client_kwargs(monkeypatch)
    s = {"devices": "192.0.2.1", "username": "u", "https": True, "verify": True}
    for over, checked in (({}, True), ({"verify": False}, False)):
        try:
            asyncio.run(m.fetch(s | over, "p", 0))
        except ApiError:
            pass
        assert (seen["verify"] is True) is checked
        assert seen["verify"] is not False  # never an unchecked connection: unticked means pinned
    assert "unencrypted" in m.notice({"https": False}) and m.notice({"https": True}) == ""


def test_every_module_checks_certificates_by_default():
    from homewatch.modules.proxmox.plugin import DEFAULTS
    from homewatch.modules.registry import MODULES

    for m in MODULES.values():
        for f in getattr(m, "fields", []):
            if f.key == "verify":
                assert f.default is True, m.id
    assert DEFAULTS["verify"] is True


def test_rejected_certificate_is_explained():
    from homewatch.modules.apibase import why

    assert "isn't trusted" in why(Exception("[SSL: CERTIFICATE_VERIFY_FAILED] self-signed certificate"))
    assert why(ValueError("x")) == "ValueError"


def test_ports_are_listed_per_device():
    m = MikroTikModule()
    snap = {"devices": [dev("sw1"), dev("sw2")], "errors": {}, "_summary": "2"}
    cs = m.choices(snap)
    assert {c.group for c in cs} == {"sw1", "sw2"}
    assert all(not c.label.startswith("sw") for c in cs)  # the device is the group, the label is the port
