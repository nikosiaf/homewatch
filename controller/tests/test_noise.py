"""Less noise (0.40): to-dos instead of issues, reboots counted from events, flapping entities, feedback,
the Ookla fallback and the speed test back-off."""

import time

from homewatch.modules import OK, WARNING, CheckResult, Thresholds, reboots
from homewatch.modules.registry import MODULES
from homewatch import todo
from tests.test_web import client  # noqa: F401 - the fixture

DAY = 86400


def res(status, detail="1 package update available"):
    return [CheckResult("pbs.updates", "Updates", status, detail, 1)]


def th(days=14):
    return {"updates": Thresholds(days, None, True)}


def test_todo_is_ok_until_it_waits_too_long(db):
    m = MODULES["pbs"]
    t = 1_800_000_000.0
    r = todo.apply(db, "home", m, th(), res(WARNING), t)[0]
    assert r.status == OK and r.detail == "To do: 1 package update available" and r.unit == "days"
    assert [x["item"] for x in todo.open_items(db, "home")] == ["1 package update available"]
    r = todo.apply(db, "home", m, th(), res(WARNING, "3 package updates available"), t + 13 * DAY)[0]
    assert r.status == OK and r.value == 13
    r = todo.apply(db, "home", m, th(), res(WARNING, "3 package updates available"), t + 15 * DAY)[0]
    assert r.status == WARNING and "(waiting 15 days)" in r.detail and r.since == t + 14 * DAY
    assert todo.open_items(db, "home")[0]["first_seen"] == t  # a newer update doesn't restart the wait
    r = todo.apply(db, "home", m, th(), res(OK, "up to date"), t + 16 * DAY)[0]
    assert r.status == OK and r.value == 0 and todo.open_items(db, "home") == []


def test_todo_without_a_limit_never_warns_and_closes_with_the_module(db):
    m = MODULES["pbs"]
    r = todo.apply(db, "home", m, th(None), res(WARNING), 1.0)[0]
    r = todo.apply(db, "home", m, th(None), res(WARNING), 400 * DAY)[0]
    assert r.status == OK
    todo.close_module(db, "home", "pbs", 401 * DAY)
    assert todo.open_items(db, "home") == []


def test_other_checks_are_left_alone(db):
    other = [CheckResult("pbs.datastores", "Datastores", WARNING, "90 % full", 90)]
    assert todo.apply(db, "home", MODULES["pbs"], th(), other, 1.0)[0].status == WARNING


def test_update_checks_are_todos_with_their_defaults():
    want = {"homeassistant.updates": 14, "pbs.updates": 14, "synology.updates": 14, "unifi.firmware": 14,
            "mikrotik.firmware": 14, "mikrotik.routeros": 7, "opnsense.firmware": 7}
    got = {f"{m.id}.{c.id}": c.warn for m in MODULES.values() for c in m.checks if c.todo}
    assert got == want


def test_reboots_warn_only_when_repeated():
    t = Thresholds(2, None)
    now = 1_800_000_000.0
    one = [{"ts": now - 600, "device": "sw1", "kind": "reboot"}, {"ts": now - 2 * DAY, "device": "sw1", "kind": "reboot"},
           {"ts": now - 60, "device": "sw1", "kind": "port_down"}]
    status, detail, n = reboots(one, [(600, "sw1"), (9 * DAY, "sw2")], t, now)
    assert status == OK and n == 1 and detail == "1 reboot in 24 h: sw1 10 min ago"
    two = one + [{"ts": now - 5 * 3600, "device": "sw2", "kind": "reboot"}]
    status, detail, n = reboots(two, [], t, now)
    assert status == WARNING and n == 2 and detail == "2 reboots in 24 h: sw2 5 h ago, sw1 10 min ago"
    assert reboots([], [(3 * DAY, "sw1")], t, now) == (OK, "no reboots in 24 h; shortest uptime sw1 3.0 days", 0)


def test_mikrotik_reads_its_reboots_from_the_events():
    from homewatch.modules.mikrotik.plugin import MikroTikModule

    from tests.test_mikrotik import NOW, dev, run

    m = MikroTikModule()
    m.events_24h = ({"ts": NOW - 600, "device": "sw1", "kind": "reboot"},)
    r = run(m, {"devices": [dev("sw1", uptime=600)], "errors": {}}, ["sw1/ether1"])["reboots"]
    assert r.status == OK and r.detail == "1 reboot in 24 h: sw1 10 min ago"
    m.events_24h += ({"ts": NOW - 3600, "device": "sw1", "kind": "reboot"},)
    assert run(m, {"devices": [dev("sw1", uptime=600)], "errors": {}}, ["sw1/ether1"])["reboots"].status == WARNING


def test_resyncs_warn_from_three():
    d = next(c for c in MODULES["speedport"].checks if c.id == "resyncs")
    assert (d.warn, d.error) == (3, 5)


def test_migration_moves_stored_limits(tmp_path):
    import sqlite3

    from homewatch.db import DB, MIGRATIONS

    path = str(tmp_path / "old.db")
    c = sqlite3.connect(path)
    for sql in MIGRATIONS[:8]:
        c.executescript(sql)
    c.execute("PRAGMA user_version=8")
    c.executemany("INSERT INTO checks(site,check_key,enabled,warn,error) VALUES('home',?,1,?,?)",
                  [("speedport.resyncs", 1, 5), ("homeassistant.updates", None, None), ("opnsense.firmware", None, None),
                   ("mikrotik.uptime", 60, None), ("pbs.updates", 30, None)])
    c.commit()
    c.close()
    db = DB(path)
    got = {r["check_key"]: r["warn"] for r in db.all("SELECT check_key, warn FROM checks")}
    assert got == {"speedport.resyncs": 3, "homeassistant.updates": 14, "opnsense.firmware": 7, "pbs.updates": 30}


# ---- flapping Home Assistant entities ---------------------------------------------------------------------
def test_flaps_count_short_spells_only(db):
    from homewatch.modules.homeassistant.plugin import flaps

    t = 1_800_000_000.0
    for i in range(3):  # three times unavailable for 10 minutes, back by itself
        assert flaps(db, "home", {"sensor.phone"}, t + i * 3600) == set() or i == 2
        got = flaps(db, "home", set(), t + i * 3600 + 600)
    assert got == {"sensor.phone"}
    # a long outage (the Zigbee coordinator gone for a night) is not a flap
    flaps(db, "home", {"binary_sensor.door"}, t)
    for i in range(3):
        flaps(db, "home", {"binary_sensor.door"}, t + i * 5 * 3600)
        flaps(db, "home", set(), t + i * 5 * 3600 + 4 * 3600)
        flaps(db, "home", {"binary_sensor.door"}, t + i * 5 * 3600 + 4 * 3600 + 1)
    assert "binary_sensor.door" not in flaps(db, "home", set(), t + 20 * 3600)


def test_flapping_entities_are_named_but_not_counted():
    from tests.test_homeassistant import NOW, iso, snap, w

    from homewatch.modules.homeassistant.plugin import plugin

    ents = [w("sensor.phone", "unavailable", name="Phone pace"), w("sensor.oil", "unavailable", name="Oil")]
    un = [{"entity_id": e["entity_id"], "name": e["name"], "last_changed": iso(NOW - 3600)} for e in ents]
    thr = {d.id: Thresholds(d.warn, d.error, d.default_on) for d in plugin.checks}
    plugin._flappy = {"sensor.phone"}
    try:
        r = {x.key.split(".")[1]: x for x in plugin.check({}, thr, snap(watched=ents, unavailable=un), NOW)}["unavailable"]
        assert r.status == WARNING and r.value == 1
        assert r.detail == "1 unavailable: Oil; not counted, they keep flapping: Phone pace"
        r = {x.key.split(".")[1]: x for x in plugin.check({"flap_quiet": False}, thr, snap(watched=ents, unavailable=un),
                                                         NOW)}["unavailable"]
        assert r.value == 2
    finally:
        plugin._flappy = set()


# ---- speed tests ------------------------------------------------------------------------------------------
def test_failed_speed_tests_back_off(cfg):
    from homewatch.runtime import Runtime

    rt = Runtime.create(cfg)
    t = 1_800_000_000.0
    for i in range(3):
        rt.db.run("INSERT INTO speedtests(site,ts,engine,ok,error) VALUES('home',?,'cloudflare',0,'502')", (t + i,))
    last = t + 2
    assert not rt.speed_due(3, last + 59 * 60) and rt.speed_due(3, last + 60 * 60)  # 3rd failure in a row: 60 min
    for i in range(5):
        rt.db.run("INSERT INTO speedtests(site,ts,engine,ok,error) VALUES('home',?,'cloudflare',0,'502')", (t + 10 + i,))
    assert rt.speed_due(3, t + 14 + 3 * 3600)  # never longer than the usual interval


async def test_cloudflare_failure_falls_back_to_ookla(cfg, monkeypatch):
    from homewatch.modules.internet import speedtest
    from homewatch.runtime import Runtime

    rt = Runtime.create(cfg)
    s = rt.db.get("module.internet") or {}
    from homewatch.modules.internet import load

    full = load(rt.db)
    full["speed"].update(engine="cloudflare", ookla_accepted=True)
    rt.db.set("module.internet", full | s)

    async def cf(*a, **k):
        return speedtest.Result(False, error="HTTPStatusError: Server error '502 Bad Gateway'\nmore")

    async def ok(*a, **k):
        return speedtest.Result(True, 95.0, 9.0, 12.0)

    monkeypatch.setattr(speedtest, "cloudflare", cf)
    monkeypatch.setattr(speedtest, "ookla", ok)
    monkeypatch.setattr("homewatch.runtime.os.access", lambda *a: True)
    r = await rt.run_speedtest()
    row = rt.db.one("SELECT engine, ok, down FROM speedtests")
    assert r.ok and (row["engine"], row["ok"], row["down"]) == ("ookla", 1, 95.0)

    async def bad(*a, **k):
        return speedtest.Result(False, error="Ookla CLI timed out")

    monkeypatch.setattr(speedtest, "ookla", bad)
    await rt.run_speedtest()
    row = rt.db.one("SELECT error FROM speedtests ORDER BY id DESC LIMIT 1")
    assert row["error"] == "Cloudflare: HTTPStatusError: Server error '502 Bad Gateway'; Ookla: Ookla CLI timed out"


# ---- pages and the weekly report --------------------------------------------------------------------------
def test_todo_box_feedback_and_report(client):
    import asyncio

    from homewatch import report
    from tests.test_web import csrf, setup_admin

    tok = setup_admin(client)
    rt = client.rt
    now = time.time()
    rt.db.run("INSERT INTO todos(site,check_key,name,item,first_seen,last_seen) VALUES('home','pbs.updates','Updates',"
              "'9 package updates available',?,?)", (now - 3 * DAY, now))
    page = client.get("/").text
    assert "To do" in page and "9 package updates available" in page and "14 days" in page
    iid = rt.db.run("INSERT INTO issues(site,check_key,level,worst,started,resolved,detail,notified) "
                    "VALUES('home','internet.loss',1,1,?,?,'packet loss 12 %',1)", (now - 600, now - 300))
    page = client.get("/history?days=7").text
    assert f'action="/issues/{iid}/feedback"' in page
    r = client.post(f"/issues/{iid}/feedback", data={"csrf": csrf(page), "v": "noise", "days": "7"})
    assert r.url.path == "/history" and "days=7" in str(r.url)
    assert rt.db.one("SELECT feedback FROM issues WHERE id=?", (iid,))["feedback"] == -1
    msg = asyncio.run(report.build(rt.db, rt.metrics, "home", now))
    assert "To do:" in msg.body and "9 package updates available (waiting 3 days)" in msg.body
    assert "internet.loss: 1 issue, 1 marked as noise" in msg.body
    client.post(f"/issues/{iid}/feedback", data={"csrf": tok, "v": "noise"})  # the same again takes it back
    assert rt.db.one("SELECT feedback FROM issues WHERE id=?", (iid,))["feedback"] is None
