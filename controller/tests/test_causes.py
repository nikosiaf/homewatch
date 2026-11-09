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
    for n, (status, expect) in enumerate((("OL", []), ("OB DISCHRG", ["on_battery"]), ("OB DISCHRG", []),
                                          ("OL CHRG", ["on_mains"]))):
        monkeypatch.setattr(ups, "snap", {"name": "apc", "vars": {"ups.status": status}})
        monkeypatch.setattr(ups, "snap_ts", T0 + 120 * n)
        assert [x.kind for x in ups.device_events(None, prev)] == expect
    prev["ob_ts"] = T0 + 1000  # the watcher saw something newer: an older reading adds nothing
    monkeypatch.setattr(ups, "snap", {"name": "apc", "vars": {"ups.status": "OB"}})
    assert ups.device_events(None, prev) == []


def test_ups_watcher_catches_a_short_power_cut(cfg, monkeypatch):
    from homewatch import runtime
    from homewatch.modules.registry import MODULES
    from homewatch.modules.ups import plugin

    rt = runtime.Runtime.create(cfg)
    ups = MODULES["ups"]
    assert ups.watched(rt, False, "apc", T0) is False  # the first status only remembers
    assert ups.watched(rt, True, "apc", T0 + 10) is True and rt.wake.is_set() and ups.stale
    assert ups.watched(rt, True, "apc", T0 + 20) is False
    assert ups.watched(rt, False, "apc", T0 + 40) is True
    rows = rt.db.all("SELECT kind, ts, exact FROM events ORDER BY ts")
    assert [(r["kind"], r["ts"], r["exact"]) for r in rows] == [("on_battery", T0 + 10, 0), ("on_mains", T0 + 40, 0)]
    # a full reading from before the watcher's last look adds no second event
    monkeypatch.setattr(ups, "snap", {"name": "apc", "vars": {"ups.status": "OB"}})
    monkeypatch.setattr(ups, "snap_ts", T0 + 30)
    rt.record_device_events(ups, T0 + 50)
    assert rt.db.one("SELECT COUNT(*) n FROM events")["n"] == 2

    # the watcher asks with GET (a one-line answer) and passes the flags on
    asked = []

    async def fake_nut(host, port, commands, timeout=6):
        asked.append(commands)
        return [[["VAR", "apc", "ups.status", "OB DISCHRG"]]]

    monkeypatch.setattr(plugin, "nut", fake_nut)
    monkeypatch.setattr(ups, "load", lambda rt_: {"enabled": True, "_configured": True, "host": "nut", "ups": "apc"})
    seen = []
    monkeypatch.setattr(ups, "watched", lambda rt_, ob, name, now: seen.append((ob, name)))

    async def one_round():
        monkeypatch.setattr(plugin.asyncio, "sleep", _stop_after(2))
        try:
            await ups.watch(rt)
        except _Stop:
            pass

    asyncio.run(one_round())
    assert asked == [["GET VAR apc ups.status"]] and seen == [(True, "apc")]


class _Stop(Exception):
    pass


def _stop_after(n):
    calls = {"n": 0}

    async def sleep(_s):
        calls["n"] += 1
        if calls["n"] >= n:
            raise _Stop

    return sleep


def test_nut_get_reads_one_line():
    from homewatch.modules.ups.plugin import nut

    async def main():
        async def serve(reader, writer):
            await reader.readline()
            writer.write(b'VAR apc ups.status "OL"\n')
            await writer.drain()
            await reader.readline()  # LOGOUT
            writer.close()

        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with server:
            return await nut("127.0.0.1", port, ["GET VAR apc ups.status"], 3)

    assert asyncio.run(main()) == [[["VAR", "apc", "ups.status", "OL"]]]


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


# ---- v0.39.2: exact times, no loss after an outage, quick recovery, Grafana markers ------------------
def test_began_finds_when_the_line_went_down_and_came_back():
    from homewatch.modules.internet.checks import began

    trend = [(T0 + 10 * i, v) for i, v in enumerate([0, 0, 80, 100, 100, 100])]
    assert began(trend, True) == T0 + 30  # every target at 100 % from here
    assert began(trend, False) is None  # it isn't up now
    trend += [(T0 + 60, 0), (T0 + 70, 0)]
    assert began(trend, False) == T0 + 60
    assert began([(T0, 100), (T0 + 10, 100)], True) is None  # down for the whole window: unknown
    assert began([], True) is None


def test_reachable_reports_when_the_data_changed():
    from homewatch.modules.internet.checks import CHECKS, Readings, Target, evaluate
    from homewatch.modules import Thresholds

    s = {"public": {"enabled": True, "targets": ["1.1.1.1"]}, "gateway": {"enabled": False, "target": ""},
         "dns": {"enabled": False, "server": "", "domain": ""}, "speed": {"enabled": False}, "line": {}}
    th = {c.id: Thresholds(c.warn, c.error, True) for c in CHECKS}
    r = Readings(now=T0 + 200, public={"1.1.1.1": Target(100, None, None, None, T0 + 190)},
                 public_trend=[(T0, 0), (T0 + 100, 100), (T0 + 190, 100)])
    reach = next(x for x in evaluate(s, th, r) if x.key == "internet.reachable")
    assert reach.status == ERROR and reach.since == T0 + 100


def test_engine_takes_the_time_from_the_data_but_not_before_the_last_change(db):
    e = Engine(db, "home")
    cr = lambda s, since=None: CheckResult("internet.reachable", "Internet reachable", s, "d", since=since)  # noqa: E731
    e.apply("internet", [cr(OK)], T0)
    e.apply("internet", [cr(ERROR, T0 + 70)], T0 + 120)
    (ev,) = e.apply("internet", [cr(ERROR, T0 + 70)], T0 + 240)
    assert ev.kind == "open" and ev.started == T0 + 70
    e.apply("internet", [cr(OK, T0 + 300)], T0 + 360)
    e.apply("internet", [cr(OK, T0 + 300)], T0 + 480)
    assert db.one("SELECT resolved FROM issues")["resolved"] == T0 + 300
    e.apply("internet", [cr(ERROR, T0 + 10)], T0 + 600)  # a hint from before the last change is not believed
    assert db.one("SELECT pending_since FROM check_state")["pending_since"] == T0 + 600


def test_quick_run_when_a_check_starts_to_recover(cfg):
    from homewatch import runtime

    rt = runtime.Runtime.create(cfg)
    fresh = []
    rt.read_fresh = lambda ids: fresh.append(list(ids))
    rt.db.run("INSERT INTO check_state(site,check_key,status,pending,since) VALUES('home','internet.reachable',2,1,0)")

    async def main():
        rt.quick_confirm(T0)
        rt.quick_confirm(T0 + 120)  # the same recovery: once

    asyncio.run(main())
    assert len(fresh) == 1 and rt.quick_at == T0
    rt.db.run("UPDATE check_state SET status=0, pending=2")  # a new problem: normal pace
    rt.pending_seen = set()
    asyncio.run(main())
    assert len(fresh) == 1


def test_incident_and_event_markers_for_grafana(cfg, monkeypatch):
    from homewatch import runtime
    from homewatch.grafana import ANNOTATIONS, overview
    from homewatch.modules import DevEvent

    rt = runtime.Runtime.create(cfg)
    inc = rt.db.run("INSERT INTO incidents(site,started,worst) VALUES('home',?,2)", (T0,))
    rt.db.run("INSERT INTO issues(site,check_key,level,worst,started,incident_id) VALUES('home','internet.reachable',2,2,?,?)",
              (T0, inc))
    lines, marked = rt.incident_markers()
    assert len(lines) == 1 and "phase=start" in lines[0] and marked == [(inc, 1)]
    rt.db.run("UPDATE incidents SET marked=1, ended=?, cause='Power cut in the area' WHERE id=?", (T0 + 420, inc))
    rt.db.run("UPDATE issues SET resolved=?, ok_sent=0", (T0 + 420,))
    assert rt.incident_markers() == ([], [])  # its "back to OK" (and cause) isn't out yet
    rt.db.run("UPDATE issues SET ok_sent=1")
    (end,), marked = rt.incident_markers()
    assert "phase=end" in end and "Likely\\ cause:\\ Power\\ cut" in end and marked == [(inc, 3)]
    rt.queue_event_line("ups", DevEvent(T0 + 5, "on_battery", "apc", "UPS on battery", exact=False))
    assert rt.metric_queue and rt.metric_queue[0].startswith("hw_event,") and "at=~" in rt.metric_queue[0]
    assert {a["name"] for a in ANNOTATIONS} == {"Incidents", "Device events"}
    assert overview(set())["annotations"]["list"] == ANNOTATIONS


def test_ups_watcher_draws_the_power_cut(cfg):
    from homewatch import runtime
    from homewatch.modules.registry import MODULES

    rt = runtime.Runtime.create(cfg)
    ups = MODULES["ups"]
    ups.watched(rt, False, "apc", T0)
    ups.watched(rt, True, "apc", T0 + 10)
    assert [x for x in rt.metric_queue if x.startswith("hw_ups,")] == [
        f"hw_ups,site=home,ups=apc on_battery=1.0,online=0.0 {int((T0 + 10) * 1e9)}"]


# ---- OPNsense logs: exact gateway and PPPoE times -----------------------------------------------
def test_opnsense_log_times():
    from homewatch.modules.opnsense.plugin import log_ts, tz_offset

    now = T0  # 2027-01-15 08:00 UTC = 10:00 local at +02:00
    assert tz_offset("Fri Jan 15 10:00:03 EET 2027", now) == 7200
    assert tz_offset("", now) is None and tz_offset("garbage text here", now) is None
    assert log_ts("2027-01-15T10:00:00", 7200) == T0 and log_ts("2027-01-15T10:00:00", None) is None


def test_opnsense_log_events_once_each():
    from homewatch.modules.opnsense.plugin import log_events

    L = lambda t, line: {"ts": f"2027-01-15T{t}", "line": line}  # noqa: E731
    logs = {
        "monitor": [L("10:00:41", "MONITOR: WAN_PPPOE (Addr: 1.1.1.1 Alarm: none -> loss RTT: 8.7 ms RTTd: 0.4 ms Loss: 12.0 %)"),
                    L("10:00:46", "MONITOR: WAN_PPPOE (Addr: 1.1.1.1 Alarm: loss -> down RTT: 8.7 ms RTTd: 0.4 ms Loss: 21.0 %)"),
                    L("10:07:54", "MONITOR: WAN_PPPOE (Addr: 1.1.1.1 Alarm: down -> none RTT: 9.0 ms RTTd: 0.0 ms Loss: 0.0 %)")],
        "noecho": [L("10:01:38", "[wan_link0] LCP: peer not responding to echo requests")],
        "linkdown": [L("10:01:38", "ppp-linkdown: executing on pppoe0 for inet")],
        "linkup": [L("10:07:53", "ppp-linkup: executing on pppoe0 for inet")],
        "newip": [L("10:07:54", "/usr/local/etc/rc.newwanip: IP renewal starting (new: 203.0.113.9, old: 203.0.113.4, "
                                "interface: wan, device: pppoe0, force: yes)")],
    }
    prev = {}
    evs = log_events(logs, 7200, "fw", prev, T0 + 600)
    assert [(e.kind, e.ts - T0) for e in evs] == [("gateway_loss", 41), ("gateway_down", 46), ("ppp_down", 98),
                                                 ("ppp_up", 473), ("gateway_up", 474)]
    assert all(e.exact for e in evs)
    assert "ISP stopped answering" in evs[2].detail and "new public IP 203.0.113.9" in evs[3].detail
    assert log_events(logs, 7200, "fw", prev, T0 + 900) == []  # each line once
    assert log_events({"monitor": None, "linkdown": None}, 7200, "fw", {}, T0) == []  # no privilege: nothing


def test_opnsense_no_guessed_gateway_times_when_the_log_is_read(monkeypatch):
    from homewatch.modules.registry import MODULES

    fw = MODULES["opnsense"]
    snap = {"info": {"name": "fw"}, "time": {"uptime": "7 days, 01:00:00"}, "tz_offset": 7200,
            "gateways": [{"name": "WAN", "status_translated": "Online"}], "logs": {"monitor": []}}
    monkeypatch.setattr(fw, "snap", snap)
    monkeypatch.setattr(fw, "snap_ts", T0)
    prev = {}
    fw.device_events(None, prev)
    snap["gateways"][0]["status_translated"] = "Offline"
    assert fw.device_events(None, prev) == []  # the log will tell the exact time instead
