"""Confirmation, issues, snooze, maintenance windows, reminders, read failures."""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from homewatch.engine import Engine, window_active
from homewatch.modules import ERROR, OK, WARNING, CheckResult

T0 = 1_800_000_000.0


def res(status, detail="d", key="internet.reachable", suppressed=None):
    return CheckResult(key, "Internet reachable", status, detail, suppressed_by=suppressed)


def run(e, status, t, **kw):
    return e.apply("internet", [res(status, **kw)], t)


def test_blip_of_one_run_sends_nothing(db):
    e = Engine(db, "home")
    assert run(e, OK, T0) == []
    assert run(e, ERROR, T0 + 120) == []
    assert run(e, OK, T0 + 240) == []
    assert db.all("SELECT * FROM issues") == []


def test_confirmed_open_and_resolve(db):
    e = Engine(db, "home")
    run(e, OK, T0)
    assert run(e, ERROR, T0 + 120) == []
    ev = run(e, ERROR, T0 + 240)
    assert [(x.kind, x.level) for x in ev] == [("open", ERROR)]
    assert ev[0].started == T0 + 120  # first observation, not the confirmation
    assert run(e, OK, T0 + 360) == []
    ev = run(e, OK, T0 + 480)
    assert [(x.kind, x.worst) for x in ev] == [("resolve", ERROR)]
    assert ev[0].resolved == T0 + 360
    issue = db.one("SELECT * FROM issues")
    assert issue["resolved"] == T0 + 360 and issue["worst"] == ERROR


def test_worse_and_better(db):
    e = Engine(db, "home")
    for i, s in enumerate([OK, WARNING, WARNING]):
        ev = run(e, s, T0 + i * 120)
    assert ev[0].kind == "open"
    run(e, ERROR, T0 + 360)
    assert run(e, ERROR, T0 + 480)[0].kind == "worse"
    run(e, WARNING, T0 + 600)
    ev = run(e, WARNING, T0 + 720)
    assert ev[0].kind == "better" and ev[0].worst == ERROR


def test_suppressed_counts_as_ok(db):
    e = Engine(db, "home")
    run(e, ERROR, T0, suppressed="internet.gateway")
    assert run(e, ERROR, T0 + 120, suppressed="internet.gateway") == []
    assert db.all("SELECT * FROM issues") == []


def test_snooze_holds_then_releases(db):
    e = Engine(db, "home")
    db.run("INSERT INTO snoozes(site,check_key,until,reason,created_by,created) VALUES(?,?,?,?,?,?)",
           ("home", "internet.reachable", T0 + 1000, "router work", "admin", T0))
    run(e, ERROR, T0)
    assert run(e, ERROR, T0 + 120) == []  # recorded, not sent
    assert db.one("SELECT notified FROM issues")["notified"] == 0
    assert e.released({}, T0 + 500) == []
    rel = e.released({"internet.reachable": "Internet reachable"}, T0 + 1100)
    assert [x.kind for x in rel] == ["open"]
    run(e, OK, T0 + 1200)
    assert run(e, OK, T0 + 1320)[0].kind == "resolve"


def test_problem_fixed_inside_snooze_is_never_sent(db):
    e = Engine(db, "home")
    db.run("INSERT INTO snoozes(site,check_key,until,reason,created_by,created) VALUES(?,?,?,?,?,?)",
           ("home", "internet.reachable", T0 + 10000, "", "admin", T0))
    for i, s in enumerate([ERROR, ERROR, OK, OK]):
        assert run(e, s, T0 + i * 120) == []
    assert db.one("SELECT resolved FROM issues")["resolved"] is not None


def test_maintenance_window_once(db):
    e = Engine(db, "home")
    db.run("INSERT INTO maintenance(site,name,kind,start_ts,end_ts,checks,created_by,created) VALUES(?,?,?,?,?,?,?,?)",
           ("home", "fw update", "once", T0 - 10, T0 + 600, json.dumps(["internet.reachable"]), "admin", T0))
    assert e.silenced("internet.reachable", T0) == "maintenance: fw update"
    assert e.silenced("internet.loss", T0) is None
    assert e.silenced("internet.reachable", T0 + 700) is None


def test_weekly_window_crossing_midnight():
    zone = ZoneInfo("Europe/Athens")
    m = {"kind": "weekly", "weekday": 5, "start_min": 23 * 60, "dur_min": 120}  # Saturday 23:00 for 2 h
    sat = datetime(2026, 10, 31, 23, 30, tzinfo=zone).timestamp()
    sun = datetime(2026, 11, 1, 0, 30, tzinfo=zone).timestamp()
    later = datetime(2026, 11, 1, 1, 30, tzinfo=zone).timestamp()
    assert window_active(m, sat, zone) and window_active(m, sun, zone)
    assert not window_active(m, later, zone)


def test_reminders(db):
    e = Engine(db, "home")
    for i in range(3):
        run(e, ERROR, T0 + i * 120)
    names = {"internet.reachable": "Internet reachable"}
    assert e.reminders(names, T0 + 3600) == []
    rem = e.reminders(names, T0 + 13 * 3600)
    assert [x.kind for x in rem] == ["remind"]
    assert e.reminders(names, T0 + 14 * 3600) == []  # once per period


def test_read_failures_alert_once_and_recover(db):
    e = Engine(db, "home")
    evs = [e.read_failed("down") for _ in range(6)]
    assert [x.kind for x in evs if x] == ["selffail"]
    assert e.read_ok().kind == "selfok"
    assert e.read_ok() is None


def test_switched_off_check_closes_silently(db):
    e = Engine(db, "home")
    for i in range(2):
        run(e, ERROR, T0 + i * 120)
    assert e.apply("internet", [], T0 + 240) == []
    assert db.one("SELECT resolved_detail FROM issues")["resolved_detail"] == "check switched off"
