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


def run(m, snap, chosen, **opts):
    s = {"enabled": True, "chosen": chosen} | opts
    th = {d.id: Thresholds(d.warn, d.error, True if d.id == "changed" else d.default_on) for d in m.checks}
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


def test_cpu_is_the_median_of_three_readings(monkeypatch):
    import asyncio
    import itertools

    import httpx

    from homewatch.modules.mikrotik import plugin

    cpu = itertools.cycle(["100", "4", "6"])  # one-second spike on the first read (SNMP walk / login)
    calls = []

    def handler(req):
        path = req.url.path.split("/rest/")[1]
        calls.append(path)
        body = {"system/resource": lambda: {"cpu-load": next(cpu), "free-memory": "50", "total-memory": "100",
                                            "uptime": "1d", "board-name": "RB2011", "version": "7.25"},
                "system/identity": lambda: {"name": "mt2"}, "system/health": lambda: [],
                "system/routerboard": lambda: {}, "interface": lambda: [], "system/clock": lambda: {"gmt-offset": "+02:00"},
                "system/package/update": lambda: {"channel": "stable"}}[path]()
        return httpx.Response(200, json=body)

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(plugin, "CPU_GAP", 0)
    s = {"devices": "192.0.2.1", "username": "u", "https": False, "verify": False}
    snap = asyncio.run(plugin.MikroTikModule().fetch(s, "p", 0))
    assert snap["devices"][0]["cpu"] == 6
    assert calls.count("system/resource") == plugin.CPU_SAMPLES


def test_version_order():
    from homewatch.modules.mikrotik.plugin import version_key as k
    assert k("7.25.1") > k("7.25") > k("7.25rc3") > k("7.25beta2") > k("7.24.6")
    assert k("7.25") == k("7.25.0") and k("garbage") < k("7.1")


def test_routeros_updates_is_optional_and_compares_per_channel():
    m = MikroTikModule()
    snap = {"devices": [dev("sw1", version="7.25 (stable)", channel="stable"),
                        dev("sw2", version="7.24.6", channel="long-term")], "errors": {},
            "latest": {"stable": "7.25.1", "long-term": "7.24.6"}, "latest_error": {}}
    assert "routeros" not in run(m, snap, [])  # option off: no check at all
    assert "routeros" in m.hidden_checks({}) and "routeros" not in m.hidden_checks({"os_updates": True})
    res = run(m, snap, [], os_updates=True)["routeros"]
    assert res.status == WARNING and "sw1 7.25 → 7.25.1 (stable)" in res.detail and "sw2" not in res.detail
    snap["latest"]["stable"] = "7.25"
    res = run(m, snap, [], os_updates=True)["routeros"]
    assert res.status == OK and not res.unknown
    snap["latest"], snap["latest_error"] = {}, {"stable": "timeout"}
    res = run(m, snap, [], os_updates=True)["routeros"]
    assert res.status == OK and res.unknown and "timeout" in res.detail


def test_newest_version_is_read_once_a_day(monkeypatch):
    import asyncio

    import httpx

    from homewatch.modules.mikrotik import plugin

    urls, answer = [], {"code": 200}

    def handler(req):
        urls.append(str(req.url))
        return httpx.Response(answer["code"], text="7.25.1 1793623193")

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    m = plugin.MikroTikModule()
    asyncio.run(m._newest({"stable"}, 1000))
    assert m.latest["stable"][0] == "7.25.1" and urls == ["https://upgrade.mikrotik.com/routeros/NEWESTa7.stable"]
    asyncio.run(m._newest({"stable"}, 1000 + 3600))  # same day: no new lookup
    assert len(urls) == 1
    answer["code"] = 500
    asyncio.run(m._newest({"stable"}, 1000 + 86400))  # next day: looked up, failure kept, old value stays
    assert len(urls) == 2 and m.latest["stable"][0] == "7.25.1" and "stable" in m.latest_error
    asyncio.run(m._newest({"stable"}, 1000 + 86400 + 60))  # retried only after an hour
    assert len(urls) == 2
    asyncio.run(m._newest({"stable", "bad channel!"}, 1000 + 86400 + 3600))
    assert len(urls) == 3 and all("bad" not in u for u in urls)


# --- daily configuration copies ---------------------------------------------------------------------------------

EXPORT = "# 2026-11-07 23:15:03 by RouterOS 7.25\n# software id = X\n/system identity\nset name=sw1\n/ip address\nadd address=192.0.2.1/24\n"


def test_config_body_ignores_the_dated_header():
    from homewatch.modules.mikrotik.plugin import config_body
    later = EXPORT.replace("2026-11-07 23:15:03", "2026-11-08 23:15:09").replace("\n", "\r\n")
    assert config_body(EXPORT) == config_body(later) and "/ip address" in config_body(EXPORT)


def _rt(db):
    from types import SimpleNamespace
    return SimpleNamespace(db=db, cfg=SimpleNamespace(site="home"))


def test_copies_are_stored_only_when_the_configuration_changes(db):
    from homewatch.modules.mikrotik import plugin
    m, rt = MikroTikModule(), _rt(db)
    snap = lambda text, err="": {"devices": [dev("sw1", host="192.0.2.1", export=text, export_error=err)], "errors": {}}  # noqa: E731
    m.store_copies(rt, snap(EXPORT), NOW - 3 * 86400)
    m.store_copies(rt, snap(EXPORT.replace("23:15:03", "23:16:00")), NOW - 2 * 86400)  # same config, new date line
    assert db.one("SELECT COUNT(*) n FROM device_configs")["n"] == 1
    s = snap(EXPORT + "/ip dns\nset servers=192.0.2.53\n")
    m.store_copies(rt, s, NOW - 3600)
    assert db.one("SELECT COUNT(*) n FROM device_configs")["n"] == 2 and "export" not in s["devices"][0]
    st = m.backup_state(rt)["192.0.2.1"]
    assert st["versions"] == 2 and st["ok"] == NOW - 3600 and st["newest"] == NOW - 3600
    m.store_copies(rt, snap(None, "HTTP 403: not allowed"), NOW)  # a failed copy keeps the last good one
    assert m.backup_state(rt)["192.0.2.1"]["error"] == "HTTP 403: not allowed"
    for i in range(plugin.BACKUP_KEEP + 5):  # only the newest BACKUP_KEEP are kept
        m.store_copies(rt, snap(EXPORT + f"# change {i}\n/x\nset y={i}\n"), NOW + i)
    assert db.one("SELECT COUNT(*) n FROM device_configs")["n"] == plugin.BACKUP_KEEP


def test_copy_checks():
    from homewatch.modules.mikrotik import plugin
    m = MikroTikModule()
    assert {"backup", "changed"} <= m.hidden_checks({}) and not ({"backup", "changed"} & m.hidden_checks({"config_backup": True}))
    base = {"devices": [dev("sw1", host="h1"), dev("sw2", host="h2")], "errors": {}}
    assert "backup" not in run(m, base, [])  # option off
    st = {"h1": {"ok": NOW - 600, "newest": NOW - 600, "versions": 2, "error": ""},
          "h2": {"ok": NOW - 600, "newest": NOW - 5 * 86400, "versions": 3, "error": ""}}
    res = run(m, base | {"backup": st}, [], config_backup=True)
    assert res["backup"].status == OK and res["changed"].status == WARNING and "sw1 at" in res["changed"].detail
    assert "sw2" not in res["changed"].detail
    st["h1"] = {"ok": NOW - 3 * 86400, "newest": NOW - 9 * 86400, "versions": 1, "error": "timeout"}
    res = run(m, base | {"backup": st}, [], config_backup=True)
    assert res["backup"].status == WARNING and "sw1 (none for 3 days: timeout)" in res["backup"].detail
    assert res["changed"].status == OK
    res = run(m, base | {"backup": {}}, [], config_backup=True)
    assert res["backup"].status == OK and res["backup"].unknown
    first = {"h1": {"ok": NOW, "newest": NOW, "versions": 1, "error": ""}}  # the first copy is not a change
    assert run(m, base | {"backup": first}, [], config_backup=True)["changed"].status == OK
    assert plugin.BACKUP_STALE == 2 * 86400


def test_export_is_read_only_when_due(monkeypatch):
    import asyncio

    import httpx

    from homewatch.modules.mikrotik import plugin

    calls = []

    def handler(req):
        path = req.url.path.split("/rest/")[1]
        calls.append((req.method, path))
        if path == "execute":
            assert req.method == "POST" and json_body(req) == {"script": "/export", "as-string": ""}
            return httpx.Response(200, json={"ret": EXPORT})
        body = {"system/resource": {"cpu-load": "1", "free-memory": "50", "total-memory": "100", "uptime": "1d",
                                    "board-name": "RB2011", "version": "7.25"},
                "system/identity": {"name": "sw1"}, "system/health": [], "system/routerboard": {}, "interface": [], "system/clock": {"gmt-offset": "+02:00"},
                "system/package/update": {"channel": "stable"}}[path]
        return httpx.Response(200, json=body)

    def json_body(req):
        import json
        return json.loads(req.content)

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(plugin, "CPU_GAP", 0)
    m = plugin.MikroTikModule()
    s = {"devices": "192.0.2.1", "username": "u", "https": False, "verify": False, "config_backup": True}
    snap = asyncio.run(m.fetch(s, "p", 0))
    assert snap["devices"][0]["export"] is None and ("POST", "execute") not in calls  # not due: no export
    m.backup_due = {"192.0.2.1"}
    snap = asyncio.run(m.fetch(s, "p", 0))
    assert snap["devices"][0]["export"] == EXPORT and calls.count(("POST", "execute")) == 1
    assert all(meth == "GET" for meth, p in calls if p != "execute")


def test_copies_pages(cfg):
    from fastapi.testclient import TestClient

    from homewatch.runtime import Runtime
    from homewatch.web import create_app
    from tests.test_web import setup_admin

    rt = Runtime.create(cfg)
    with TestClient(create_app(rt, start_loops=False), base_url="https://testserver") as c:
        setup_admin(c)
        m = MikroTikModule()
        snap = lambda text: {"devices": [dev("sw 1", host="192.0.2.1", export=text, export_error="")], "errors": {}}  # noqa: E731
        m.store_copies(rt, snap(EXPORT), NOW - 86400)
        m.store_copies(rt, snap(EXPORT.replace("add address=192.0.2.1/24", "add address=192.0.2.9/24")), NOW)
        ids = [r["id"] for r in rt.db.all("SELECT id FROM device_configs ORDER BY ts")]
        r = c.get(f"/modules/mikrotik/configs/{ids[1]}")
        assert r.status_code == 200 and "-add address=192.0.2.1/24" in r.text and "+add address=192.0.2.9/24" in r.text
        r = c.get(f"/modules/mikrotik/configs/{ids[0]}")
        assert "The first copy kept" in r.text
        r = c.get(f"/modules/mikrotik/configs/{ids[1]}/download")
        assert r.text.startswith("# 2026-11-07") and 'filename="sw-1-' in r.headers["content-disposition"]
        r = c.get("/modules/mikrotik/configs/999", follow_redirects=False)
        assert r.status_code == 303


def test_port_tables_share_column_widths():
    m = MikroTikModule()
    snap = {"devices": [dev("sw1"), dev("sw2", ports=[{"name": "ether1", "comment": "a much longer comment here",
                                                        "running": True, "disabled": False, "errors": 0}])], "errors": {}}
    ports = [t for t in m.tables(snap, {}) if t.title.startswith("Ports on")]
    assert len(ports) == 2 and ports[0].widths == ports[1].widths and len(ports[0].widths) == len(ports[0].head)


def test_worst_value_lists_every_device():
    m = MikroTikModule()
    snap = {"devices": [dev("core", mem=44), dev("lan", mem=45), dev("sw3", mem=10, temp=None)], "errors": {}}
    res = run(m, snap, [])
    assert res["mem"].detail == "lan 45 % | core 44 % | sw3 10 %" and res["mem"].value == 45
    assert res["temp"].detail == "core 30 °C | lan 30 °C"
