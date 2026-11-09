"""Device events (step 2) and likely causes of incidents (step 3)."""

import asyncio
from zoneinfo import ZoneInfo

from homewatch.causes import explain
from homewatch.engine import Engine
from homewatch.modules import ERROR, OK, CheckResult

UTC = ZoneInfo("UTC")
T0 = 1_800_000_000.0  # 2027-01-15 08:00 UTC


def ev(db, ts, module, device, kind, detail="", exact=1):
    db.run("INSERT INTO events(site,ts,module,device,kind,detail,exact,seen) VALUES('home',?,?,?,?,?,?,?)",
           (ts, module, device, kind, detail, exact, ts))


def why(db, keys=("internet.reachable",), start=T0, end=T0 + 420):
    return explain(db, "home", start, end, list(keys), UTC)[0]


def test_area_power_cut(db):
    ev(db, T0 + 5, "ups", "apc", "on_battery", "UPS on battery", 0)
    ev(db, T0 + 65, "ups", "apc", "on_mains", "UPS back on mains", 0)
    ev(db, T0 + 400, "speedport", "modem", "line_resync", "DSL line re-synced")
    cause, evidence = explain(db, "home", T0, T0 + 420, ["internet.reachable", "opnsense.gateways"], UTC)
    assert cause.startswith("Power cut in the area") and "~08:00 to ~08:01" in cause and "08:06" in cause
    assert [e[2] for e in evidence] == ["apc", "apc", "modem"]


def test_power_cut_at_home_restarts_the_modem(db):
    ev(db, T0, "ups", "apc", "on_battery", exact=0)
    ev(db, T0 + 30, "speedport", "modem", "reboot", "The modem restarted")
    ev(db, T0 + 200, "speedport", "modem", "line_resync")
    assert why(db).startswith("Power cut at home") and "modem restarted" in why(db)


def test_power_cut_without_internet_trouble(db):
    ev(db, T0, "ups", "apc", "on_battery", exact=0)
    assert why(db, keys=("ups.power",)).startswith("Power cut: the UPS ran on battery")


def test_modem_restart(db):
    ev(db, T0 + 10, "speedport", "modem", "reboot", "The modem restarted")
    assert why(db).startswith("The modem restarted at 08:00")


def test_own_equipment(db):
    ev(db, T0 - 60, "opnsense", "fw", "reboot", "Firewall fw restarted")
    assert why(db).startswith("Firewall fw restarted at 07:59: a local cause")


def test_line_problem(db):
    ev(db, T0 + 300, "speedport", "modem", "line_resync", "DSL line re-synced")
    assert why(db).startswith("The DSL line dropped and re-synced at 08:05")


def test_isp_problem_needs_the_modem_watched(db):
    assert why(db) == ""  # nothing known: no cause
    db.run("INSERT INTO coverage(site,module,start,end) VALUES('home','speedport',?,?)", (T0 - 3600, T0 + 3600))
    assert why(db).startswith("The internet stopped answering while the DSL line stayed up")


def test_events_far_away_do_not_count(db):
    ev(db, T0 - 3600, "ups", "apc", "on_battery", exact=0)
    ev(db, T0 + 7200, "speedport", "modem", "reboot")
    assert why(db) == ""


# ---- the engine waits one run, then tells the cause ---------------------------------------------
def test_incident_close_waits_one_run_and_tells_the_cause(db):
    from homewatch.notify import render

    e = Engine(db, "home")
    e.explain = True
    r = lambda s, t: e.apply("internet", [CheckResult("internet.reachable", "Internet reachable", s, "d")], t)  # noqa: E731
    r(OK, T0)
    r(ERROR, T0 + 120)
    r(ERROR, T0 + 240)
    r(OK, T0 + 360)
    assert r(OK, T0 + 480) + e.flush(T0 + 480) == [] and e.just_ended  # over: the runtime reads everything again
    ev(db, T0 + 100, "ups", "apc", "on_battery", "UPS on battery", 0)
    ev(db, T0 + 420, "speedport", "modem", "line_resync", "DSL line re-synced")
    out = e.flush(T0 + 510)
    assert [x.kind for x in out] == ["resolve"] and out[0].cause.startswith("Power cut in the area")
    assert db.one("SELECT cause FROM incidents")["cause"] == out[0].cause
    msg = render(out, "Home", (0, 0), "", UTC)
    assert "Likely cause: Power cut in the area" in msg.body and "modem: DSL line re-synced" in msg.body
    assert e.flush(T0 + 600) == []


# ---- device events from the modules ------------------------------------------------------------
def test_modem_resync_and_reboot(cfg):
    from homewatch.modules.registry import MODULES
    from homewatch.runtime import Runtime

    rt = Runtime.create(cfg)
    m = MODULES["speedport"]
    st = {"last_ok": T0, "status": {"link_up": 1}, "values": {"line_up_s": 86400, "modem_uptime_s": 900000}}
    rt.db.set("speedport.state", st)
    prev = {}
    assert m.device_events(rt, prev) == []  # the first reading only remembers
    rt.db.set("speedport.state", st | {"last_ok": T0 + 900, "values": {"line_up_s": 300, "modem_uptime_s": 900900,
                                                                       "sync_down_kbps": 110000, "sync_up_kbps": 11000}})
    (e,) = m.device_events(rt, prev)
    assert (e.kind, e.ts, e.exact) == ("line_resync", T0 + 600, True) and "110/11" in e.detail
    rt.db.set("speedport.state", st | {"last_ok": T0 + 1800, "values": {"line_up_s": 200, "modem_uptime_s": 260}})
    kinds = sorted(x.kind for x in m.device_events(rt, prev))
    assert kinds == ["line_resync", "reboot"]


def test_ups_on_battery_and_back(cfg, monkeypatch):
    from homewatch.modules.registry import MODULES

    ups = MODULES["ups"]
    prev = {}
    for status, expect in (("OL", []), ("OB DISCHRG", ["on_battery"]), ("OB DISCHRG", []), ("OL CHRG", ["on_mains"])):
        monkeypatch.setattr(ups, "snap", {"name": "apc", "vars": {"ups.status": status}})
        monkeypatch.setattr(ups, "snap_ts", T0)
        assert [x.kind for x in ups.device_events(None, prev)] == expect


def test_mikrotik_port_down_with_router_time(monkeypatch):
    from homewatch.modules.mikrotik.plugin import gmt_offset, router_ts
    from homewatch.modules.registry import MODULES

    assert gmt_offset("+02:00") == 7200 and gmt_offset("-03:30") == -12600 and gmt_offset("") is None
    assert router_ts("2027-01-15 10:00:00", 7200) == T0 == router_ts("jan/15/2027 10:00:00", 7200)
    mt = MODULES["mikrotik"]
    port = {"name": "ether7", "comment": "TV", "last_down": None, "last_up": None}
    dev = {"host": "10.0.0.2", "name": "sw", "uptime": 3600.0, "ports": [port]}
    monkeypatch.setattr(mt, "snap", {"devices": [dev]})
    monkeypatch.setattr(mt, "snap_ts", T0)
    prev = {}
    assert mt.device_events(None, prev) == []
    port.update(last_down=T0 + 10, last_up=T0 + 18)
    (e,) = mt.device_events(None, prev)
    assert (e.kind, e.ts, e.device) == ("port_down", T0 + 10, "sw ether7 (TV)") and "back after 8 s" in e.detail
    assert mt.device_events(None, prev) == []
    dev["uptime"] = 60.0
    monkeypatch.setattr(mt, "snap_ts", T0 + 7200)
    assert [x.kind for x in mt.device_events(None, prev)] == ["reboot"]


def test_opnsense_gateway_and_reboot(monkeypatch):
    from homewatch.modules.registry import MODULES

    fw = MODULES["opnsense"]
    prev = {}
    snap = {"info": {"name": "fw"}, "time": {"uptime": "7 days, 01:00:00"},
            "gateways": [{"name": "WAN", "status_translated": "Online"}]}
    monkeypatch.setattr(fw, "snap", snap)
    monkeypatch.setattr(fw, "snap_ts", T0)
    assert fw.device_events(None, prev) == []
    snap["gateways"][0]["status_translated"] = "Offline"
    (e,) = fw.device_events(None, prev)
    assert e.kind == "gateway_down" and not e.exact
    snap["time"]["uptime"] = "00:03"
    monkeypatch.setattr(fw, "snap_ts", T0 + 600)
    assert [x.kind for x in fw.device_events(None, prev)] == ["reboot"]


def test_synology_uptime():
    from homewatch.modules.synology.plugin import hms_s

    assert hms_s("442:22:37") == 442 * 3600 + 22 * 60 + 37 and hms_s("") is None and hms_s("1 day") is None


def test_runtime_records_device_events_once_per_reading(cfg, monkeypatch):
    from homewatch import runtime
    from homewatch.modules.registry import MODULES

    rt = runtime.Runtime.create(cfg)

    async def write(ls):
        pass

    monkeypatch.setattr(rt.metrics, "write", write)
    monkeypatch.setattr(MODULES["internet"], "enabled", lambda s: False)
    ups = MODULES["ups"]
    status = {"v": "OL"}

    async def readings(rt_, s, now):
        if ups.snap is None or ups.snap["vars"]["ups.status"] != status["v"]:
            ups.snap, ups.snap_ts = {"name": "apc", "vars": {"ups.status": status["v"], "battery.charge": "99"}}, now
        return (s, ups.snap, "", now)

    monkeypatch.setattr(ups, "readings", readings)
    monkeypatch.setattr(ups, "load", lambda rt_: {"enabled": True, "_configured": True, "chosen": [], "skipped": []})
    monkeypatch.setattr(ups, "snap", None)
    asyncio.run(rt.run_checks(T0))
    status["v"] = "OB DISCHRG"
    asyncio.run(rt.run_checks(T0 + 120))
    asyncio.run(rt.run_checks(T0 + 240))  # same reading: not again
    rows = rt.db.all("SELECT kind, ts, exact FROM events")
    assert [(r["kind"], r["ts"], r["exact"]) for r in rows] == [("on_battery", T0 + 120, 0)]
