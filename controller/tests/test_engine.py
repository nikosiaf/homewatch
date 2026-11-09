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


def test_the_same_reading_confirms_nothing(db):
    e = Engine(db, "home")
    bad = lambda ts: CheckResult("x.a", "A", ERROR, "bad", observed_at=ts)  # noqa: E731
    assert e.apply("x", [CheckResult("x.a", "A", OK, "ok")], 1.0) == []
    assert e.apply("x", [bad(100.0)], 2.0) == []
    assert e.apply("x", [bad(100.0)], 3.0) == []  # cached: still waiting
    assert [ev.kind for ev in e.apply("x", [bad(200.0)], 4.0)] == ["open"]


def test_partial_results_keep_the_other_checks(db):
    e = Engine(db, "home")
    e.apply("x", [CheckResult("x.a", "A", OK, "ok"), CheckResult("x.b", "B", OK, "ok")], 1.0)
    e.apply("x", [CheckResult("x.a", "A", ERROR, "bad")], 2.0, partial=True)
    assert db.one("SELECT 1 FROM check_state WHERE check_key='x.b'")


def test_silences_read_once_and_answer_like_one_by_one(db):
    e = Engine(db, "home")
    db.run("INSERT INTO snoozes(site,check_key,until,reason,created_by,created) VALUES('home','x.a',500,'short','a',0)")
    db.run("INSERT INTO snoozes(site,check_key,until,reason,created_by,created) VALUES('home','x.a',900,'long','a',0)")
    db.run("INSERT INTO snoozes(site,check_key,until,reason,created_by,created) VALUES('home','x.gone',50,'over','a',0)")
    db.run("INSERT INTO maintenance(site,name,kind,start_ts,end_ts,checks,created_by,created) VALUES('home','all','once',0,1000,NULL,'a',0)")
    db.run("INSERT INTO maintenance(site,name,kind,start_ts,end_ts,checks,created_by,created) VALUES('home','later','once',800,1000,'[\"x.b\"]','a',0)")
    queries = []
    why = e.silences(100.0)
    db.conn.set_trace_callback(queries.append)
    assert why("x.a") == "snoozed: long"  # longest snooze first
    assert why("x.gone") == "maintenance: all" and why("x.other") == "maintenance: all"
    db.conn.set_trace_callback(None)
    assert queries == []  # nothing more is read per check



# ---- incidents -----------------------------------------------------------------------------
def gw(status, detail="g"):
    return CheckResult("opnsense.gateways", "Gateways", status, detail)


def both(e, line, gate, t):
    return e.apply("internet", [res(line)], t) + e.apply("opnsense", [gw(gate)], t)


def test_incident_sends_one_back_to_ok(db):
    from homewatch.notify import render

    e = Engine(db, "home")
    both(e, OK, OK, T0)
    both(e, ERROR, ERROR, T0 + 120)
    opened = both(e, ERROR, ERROR, T0 + 240)
    assert [x.kind for x in opened] == ["open", "open"]  # problems are still sent as they come
    assert db.one("SELECT COUNT(DISTINCT incident_id) n FROM issues")["n"] == 1
    assert both(e, OK, ERROR, T0 + 360) == [] and both(e, OK, ERROR, T0 + 480) == []  # line back: held
    both(e, OK, OK, T0 + 600)
    ev = both(e, OK, OK, T0 + 720) + e.flush(T0 + 720)
    assert [x.kind for x in ev] == ["close"] and len(ev[0].items) == 2 and ev[0].worst == ERROR
    assert ev[0].started == T0 + 120 and ev[0].resolved == T0 + 600
    inc = db.one("SELECT * FROM incidents")
    assert inc["ended"] == T0 + 600 and inc["worst"] == ERROR
    msg = render(ev, "Home", (0, 0), "", ZoneInfo("UTC"))
    assert "back to OK" in msg.title and "Incident over" in msg.body and "Gateways" in msg.body
    assert "Internet reachable" in msg.body


def test_incident_still_open_sends_the_rest_after_a_while(db):
    from homewatch.engine import OK_SETTLE

    e = Engine(db, "home")
    both(e, OK, OK, T0)
    both(e, ERROR, WARNING, T0 + 120)
    both(e, ERROR, WARNING, T0 + 240)
    both(e, OK, WARNING, T0 + 360)
    assert both(e, OK, WARNING, T0 + 480) + e.flush(T0 + 480) == []  # held while the gateway warning is open
    t = T0 + 360 + OK_SETTLE
    assert both(e, OK, WARNING, t) == []  # never in the middle of a run
    ev = e.flush(t)
    assert [x.kind for x in ev] == ["partial"] and ev[0].still == ["Gateways"]
    assert [i[0] for i in ev[0].items] == ["Internet reachable"]
    assert both(e, OK, WARNING, T0 + 1200) + e.flush(T0 + 1200) == []  # sent once
    both(e, OK, OK, T0 + 1320)
    ev = both(e, OK, OK, T0 + 1440)
    assert [x.kind for x in ev] == ["close"] and [i[0] for i in ev[0].items] == ["Gateways"]


def test_late_problem_starts_a_new_incident(db):
    from homewatch.engine import INCIDENT_JOIN

    e = Engine(db, "home")
    e.apply("opnsense", [gw(OK)], T0)
    e.apply("opnsense", [gw(WARNING)], T0 + 120)
    e.apply("opnsense", [gw(WARNING)], T0 + 240)  # a long-running warning
    t = T0 + 120 + INCIDENT_JOIN + 60
    run(e, OK, t - 240)
    run(e, ERROR, t)
    run(e, ERROR, t + 120)
    ids = [r["incident_id"] for r in db.all("SELECT incident_id FROM issues ORDER BY started")]
    assert len(set(ids)) == 2  # the new outage is not glued to the old warning
    run(e, OK, t + 240)
    ev = run(e, OK, t + 360)
    assert [x.kind for x in ev] == ["resolve"]  # its own incident, one check: plain back to OK at once


def test_silenced_problem_in_incident_is_not_announced(db):
    e = Engine(db, "home")
    both(e, OK, OK, T0)
    db.run("INSERT INTO snoozes(site,check_key,until,reason,created_by,created) VALUES('home','opnsense.gateways',?,'',"
           "'admin',?)", (T0 + 10_000, T0))
    both(e, ERROR, ERROR, T0 + 120)
    assert [x.key for x in both(e, ERROR, ERROR, T0 + 240)] == ["internet.reachable"]
    both(e, OK, OK, T0 + 360)
    ev = both(e, OK, OK, T0 + 480)
    assert [x.kind for x in ev] == ["resolve"] and ev[0].key == "internet.reachable"
