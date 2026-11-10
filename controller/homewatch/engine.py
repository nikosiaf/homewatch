"""Check engine: turns check results into confirmed state changes, issues and events to notify.

Rules (defaults from running it in a real home):
- a new level must hold for CONFIRM runs in a row before it counts (blips are ignored)
- every open / worse / better / resolve is stored as an issue
- snoozed checks and maintenance windows record issues but hold their notifications; anything
  still open when the silence ends is sent then
- open issues are repeated every REMIND_H hours
- after FAIL_ALERT runs in a row that can't read data, homeWatch alerts about itself
- issues that start together form one incident: a new issue joins the newest incident that is still open
  and started at most INCIDENT_JOIN ago. "Back to OK" is held and sent as one message when the whole
  incident is over, or after OK_SETTLE when something in it stays open (so nothing waits behind it)
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .db import DB
from .modules import ERROR, OK, WARNING, CheckDef, CheckResult, Thresholds

DEFAULT_TIMING = {"interval": 120, "confirm": 2, "remind_h": 12, "fail_alert": 5}
INCIDENT_JOIN = 30 * 60  # s: a new issue joins an open incident that started at most this long ago
OK_SETTLE = 5 * 60  # s: "back to OK" of an incident that is still open waits at most this long


@dataclass
class Event:
    kind: str  # open | worse | better | resolve | remind | selffail | selfok
    key: str
    name: str
    level: int
    worst: int
    detail: str
    started: float | None = None
    resolved: float | None = None
    items: list | None = None  # close / partial: the checks back to OK, [(name, worst, started, resolved, detail)]
    still: list | None = None  # partial: names of the incident's checks still open
    cause: str = ""  # close / resolve of an incident: its likely cause (causes.explain)
    evidence: list | None = None  # and the device events behind it [(ts, module, device, detail, exact)]

    @property
    def route_level(self) -> int:
        """Which level a channel filter sees: back-to-OK follows the level that opened it."""
        return self.worst if self.kind in ("resolve", "close", "partial") else self.level


def timing(db: DB) -> dict:
    return {**DEFAULT_TIMING, **(db.get("engine.timing") or {})}


def thresholds(db: DB, site: str, module: str, defs: list[CheckDef]) -> dict[str, Thresholds]:
    rows = {r["check_key"]: r for r in db.all("SELECT * FROM checks WHERE site=? AND check_key LIKE ?",
                                              (site, f"{module}.%"))}
    out = {}
    for d in defs:
        r = rows.get(f"{module}.{d.id}")
        if r is None:
            out[d.id] = Thresholds(d.warn, d.error, d.default_on)
        else:
            out[d.id] = Thresholds(r["warn"] if "warn" in d.editable else d.warn,
                                   r["error"] if "error" in d.editable else d.error,
                                   bool(r["enabled"]))
    return out


def tz(db: DB) -> ZoneInfo:
    try:
        return ZoneInfo(db.get("home.tz") or "UTC")
    except Exception:
        return ZoneInfo("UTC")


class Engine:
    def __init__(self, db: DB, site: str):
        self.db = db
        self.site = site
        self.explain = False  # look for a likely cause when an incident ends (the runtime turns it on)
        self.just_ended = False  # an incident ended in this run: the runtime reads every device again

    # ---- silences -------------------------------------------------------------------------
    def silences(self, now: float):
        """Read what is silenced right now once; returns key -> why it is silenced, or None."""
        snoozed: dict[str, str] = {}
        for s in self.db.all("SELECT check_key, reason FROM snoozes WHERE site=? AND until>? ORDER BY until",
                             (self.site, now)):  # the snooze that lasts longest wins
            snoozed[s["check_key"]] = f"snoozed{': ' + s['reason'] if s['reason'] else ''}"
        zone = tz(self.db)
        active = [(m["name"], json.loads(m["checks"]) if m["checks"] else None)
                  for m in self.db.all("SELECT * FROM maintenance WHERE site=?", (self.site,)) if window_active(m, now, zone)]

        def why(key: str) -> str | None:
            if key in snoozed:
                return snoozed[key]
            return next((f"maintenance: {name}" for name, keys in active if keys is None or key in keys), None)

        return why

    def silenced(self, key: str, now: float) -> str | None:
        """Return why a check is silenced right now (snooze / maintenance), or None."""
        return self.silences(now)(key)

    # ---- results → events ------------------------------------------------------------------
    def apply(self, module: str, results: list[CheckResult], now: float, partial: bool = False) -> list[Event]:
        """partial: the results are only part of the module's checks, so the others didn't disappear."""
        confirm = int(timing(self.db)["confirm"])
        events: list[Event] = []
        seen = set()
        with self.db.tx() as db:
            for res in results:
                seen.add(res.key)
                level = res.effective
                st = db.one("SELECT * FROM check_state WHERE site=? AND check_key=?", (self.site, res.key))
                if st is None:
                    # first sight: OK is taken as is; a problem still has to be confirmed
                    db.run("INSERT INTO check_state(site,check_key,status,since,detail,value) VALUES(?,?,?,?,?,?)",
                           (self.site, res.key, OK, now, res.detail, res.value))
                    st = db.one("SELECT * FROM check_state WHERE site=? AND check_key=?", (self.site, res.key))
                status = st["status"]
                if level == status:
                    db.run("UPDATE check_state SET pending=NULL,pending_count=0,pending_since=NULL,detail=?,value=? "
                           "WHERE site=? AND check_key=?", (res.detail, res.value, self.site, res.key))
                    if level != OK:
                        db.run("UPDATE issues SET detail=?, nodes=? WHERE site=? AND check_key=? AND resolved IS NULL",
                               (res.detail, json.dumps(res.extra.get("nodes") or []), self.site, res.key))
                    continue
                if st["pending"] == level:
                    if res.observed_at is not None and res.observed_at == st["pending_obs"]:
                        continue  # the same reading again confirms nothing
                    count, since = st["pending_count"] + 1, st["pending_since"]
                else:
                    # the data may show when it really began (not before the last confirmed change, not in the future)
                    hint = res.since
                    count, since = 1, hint if hint is not None and (st["since"] or 0) <= hint <= now else now
                if count < confirm:
                    db.run("UPDATE check_state SET pending=?,pending_count=?,pending_since=?,pending_obs=?,detail=?,value=? "
                           "WHERE site=? AND check_key=?", (level, count, since, res.observed_at, res.detail, res.value,
                                                            self.site, res.key))
                    continue
                db.run("UPDATE check_state SET status=?,pending=NULL,pending_count=0,pending_since=NULL,since=?,"
                       "detail=?,value=? WHERE site=? AND check_key=?",
                       (level, since, res.detail, res.value, self.site, res.key))
                ev = self._transition(db, res, status, level, since, now)
                if ev:
                    events.append(ev)
            # checks that disappeared (probe switched off): close silently
            for st in db.all("SELECT check_key FROM check_state WHERE site=? AND check_key LIKE ?",
                             (self.site, f"{module}.%")):
                if st["check_key"] not in seen and not partial:
                    db.run("UPDATE issues SET resolved=?, resolved_detail='check switched off', notified=1 "
                           "WHERE site=? AND check_key=? AND resolved IS NULL", (now, self.site, st["check_key"]))
                    db.run("DELETE FROM check_state WHERE site=? AND check_key=?", (self.site, st["check_key"]))
            events += self._flush(db, now, settle=False)
        return events

    # ---- incidents ------------------------------------------------------------------------
    def _incident_for(self, db, since: float, level: int) -> int:
        row = db.one("SELECT * FROM incidents WHERE site=? AND ended IS NULL AND started>=? ORDER BY started DESC LIMIT 1",
                     (self.site, since - INCIDENT_JOIN))
        if row:
            db.run("UPDATE incidents SET worst=MAX(worst, ?) WHERE id=?", (level, row["id"]))
            return row["id"]
        return db.run("INSERT INTO incidents(site,started,worst) VALUES(?,?,?)", (self.site, since, level))

    def flush(self, now: float) -> list[Event]:
        """At the end of a run, after every module: also send what waited OK_SETTLE in an incident still open."""
        with self.db.tx() as db:
            return self._flush(db, now, settle=True)

    def _flush(self, db, now: float, settle: bool) -> list[Event]:
        """Close incidents with nothing left open, and send their held "back to OK": as it was when the
        incident had one check, else as one message for all the checks it had. settle: also what has waited
        OK_SETTLE in an incident that is still open (only at the end of a run, when every module has spoken)."""
        db.run("UPDATE incidents SET ended=(SELECT MAX(resolved) FROM issues i WHERE i.incident_id=incidents.id) "
               "WHERE site=? AND ended IS NULL AND NOT EXISTS (SELECT 1 FROM issues i WHERE i.incident_id=incidents.id "
               "AND i.resolved IS NULL)", (self.site,))
        held: dict = {}
        for i in db.all("SELECT * FROM issues WHERE site=? AND ok_sent=0 ORDER BY resolved, id", (self.site,)):
            held.setdefault(i["incident_id"] or -i["id"], []).append(i)
        out = []
        for inc_id, rows in held.items():
            inc = db.one("SELECT * FROM incidents WHERE id=?", (inc_id,)) if inc_id > 0 else None
            still = [r["name"] or r["check_key"] for r in db.all(
                "SELECT name, check_key FROM issues WHERE incident_id=? AND resolved IS NULL AND notified=1 ORDER BY started",
                (inc_id,))] if inc else []
            if still and (not settle or now - rows[0]["resolved"] < OK_SETTLE):
                continue
            cause, evidence = "", None
            if inc and not still and self.explain:
                if inc["closing"] is None:  # over: one more quick run, with every device read again, then the cause
                    db.run("UPDATE incidents SET closing=? WHERE id=?", (now, inc_id))
                    self.just_ended = True
                    continue
                if now <= inc["closing"]:
                    continue
                cause, evidence = self._explain(db, inc)
            db.run(f"UPDATE issues SET ok_sent=1 WHERE id IN ({','.join('?' * len(rows))})", tuple(r["id"] for r in rows))
            told = db.one("SELECT COUNT(*) n FROM issues WHERE incident_id=? AND notified=1", (inc_id,))["n"] if inc else 1
            if len(rows) == 1 and not still and told == 1:
                r = rows[0]
                out.append(Event("resolve", r["check_key"], r["name"] or r["check_key"], OK, r["worst"],
                                 r["resolved_detail"], r["started"], r["resolved"], cause=cause, evidence=evidence))
                continue
            items = [(r["name"] or r["check_key"], r["worst"], r["started"], r["resolved"], r["resolved_detail"])
                     for r in rows]
            start = inc["started"] if inc else rows[0]["started"]
            end = (inc["ended"] if inc and inc["ended"] else max(r["resolved"] for r in rows))
            out.append(Event("partial" if still else "close", f"incident.{inc_id}", "Incident", OK,
                             max(r["worst"] for r in rows), "", start, None if still else end, items, still,
                             cause, evidence))
        return out

    def _explain(self, db, inc) -> tuple[str, list]:
        from . import causes

        rows = db.all("SELECT check_key, nodes FROM issues WHERE incident_id=?", (inc["id"],))
        keys = [r["check_key"] for r in rows]
        nodes = sorted({n for r in rows for n in json.loads(r["nodes"] or "[]")})
        cause, evidence = causes.explain(db, self.site, inc["started"], inc["ended"], keys, tz(self.db), nodes)
        db.run("UPDATE incidents SET cause=?, evidence=? WHERE id=?", (cause, causes.dumps(evidence), inc["id"]))
        return cause, evidence

    def _transition(self, db, res: CheckResult, old: int, new: int, since: float, now: float) -> Event | None:
        silent = self.silenced(res.key, now) is not None
        issue = db.one("SELECT * FROM issues WHERE site=? AND check_key=? AND resolved IS NULL", (self.site, res.key))
        if new == OK:
            if issue is None:
                return None
            db.run("UPDATE issues SET resolved=?, resolved_detail=? WHERE id=?", (since, res.detail, issue["id"]))
            if not issue["notified"]:
                # never announced (it happened inside a silence): nothing to take back
                db.run("UPDATE issues SET notified=1 WHERE id=?", (issue["id"],))
                return None
            # "back to OK" is sent by _flush: at once when its incident is over, else with the rest of it
            db.run("UPDATE issues SET ok_sent=0, name=? WHERE id=?", (res.name, issue["id"]))
            return None
        if issue is None:
            from . import topology

            nodes = res.extra.get("nodes") or []
            # a device behind something that has a problem, or just restarted or lost link: say so; and when that
            # parent's problem was already sent, this one goes with it (listed when the incident is over)
            above = topology.behind(db, self.site, nodes, now) if nodes else None
            detail = f"{res.detail} ({above[0]})" if above else res.detail
            incident = self._incident_for(db, since, new)
            db.run("INSERT INTO issues(site,check_key,level,worst,started,detail,notified,incident_id,name,nodes) "
                   "VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (self.site, res.key, new, new, since, detail, 0 if silent else 1, incident, res.name, json.dumps(nodes)))
            if silent or (above and above[1]):
                return None
            return Event("open", res.key, res.name, new, new, detail, since)
        worst = max(issue["worst"], new)
        db.run("UPDATE issues SET level=?, worst=?, detail=? WHERE id=?", (new, worst, res.detail, issue["id"]))
        if issue["incident_id"]:
            db.run("UPDATE incidents SET worst=MAX(worst, ?) WHERE id=?", (worst, issue["incident_id"]))
        if silent or not issue["notified"]:
            return None
        return Event("worse" if new > old else "better", res.key, res.name, new, worst, res.detail, issue["started"])

    def released(self, names: dict[str, str], now: float) -> list[Event]:
        """Open issues that were held by a silence that has now ended."""
        out = []
        why = self.silences(now)
        for i in self.db.all("SELECT * FROM issues WHERE site=? AND resolved IS NULL AND notified=0", (self.site,)):
            if why(i["check_key"]) is None:
                self.db.run("UPDATE issues SET notified=1 WHERE id=?", (i["id"],))
                out.append(Event("open", i["check_key"], names.get(i["check_key"], i["check_key"]),
                                 i["level"], i["worst"], i["detail"], i["started"]))
        return out

    def reminders(self, names: dict[str, str], now: float) -> list[Event]:
        hours = float(timing(self.db)["remind_h"])
        last = self.db.get("engine.last_remind", 0)
        if hours <= 0 or now - last < hours * 3600:
            return []
        out = []
        why = self.silences(now)
        for i in self.open_issues():
            if i["notified"] and not i["acked"] and now - i["started"] >= hours * 3600 and why(i["check_key"]) is None:
                out.append(Event("remind", i["check_key"], names.get(i["check_key"], i["check_key"]),
                                 i["level"], i["worst"], i["detail"], i["started"]))
        if out:
            self.db.set("engine.last_remind", now)
        return out

    # ---- read failures ---------------------------------------------------------------------
    # ---- coverage -------------------------------------------------------------------------
    def observe(self, module: str, now: float, gap: float) -> None:
        """Extend the module's observed span, or start a new one after a gap."""
        with self.db.tx() as db:
            last = db.one("SELECT id, end FROM coverage WHERE site=? AND module=? ORDER BY end DESC LIMIT 1",
                          (self.site, module))
            if last and now - last["end"] <= gap:
                db.run("UPDATE coverage SET end=? WHERE id=?", (now, last["id"]))
            else:
                db.run("INSERT INTO coverage(site,module,start,end) VALUES(?,?,?,?)", (self.site, module, now, now))
                db.run("DELETE FROM coverage WHERE end<?", (now - 400 * 86400,))

    def last_observed(self, module: str) -> float | None:
        row = self.db.one("SELECT MAX(end) e FROM coverage WHERE site=? AND module=?", (self.site, module))
        return row["e"] if row else None

    def read_failed(self, error: str) -> Event | None:
        fails = self.db.get("engine.fails", 0) + 1
        self.db.set("engine.fails", fails)
        if fails == int(timing(self.db)["fail_alert"]):
            return Event("selffail", "homewatch.read", "homewatch data", ERROR, ERROR,
                         f"Can't read monitoring data for {fails} runs: {error}")
        return None

    def read_ok(self) -> Event | None:
        fails = self.db.get("engine.fails", 0)
        self.db.set("engine.fails", 0)
        if fails >= int(timing(self.db)["fail_alert"]):
            return Event("selfok", "homewatch.read", "homewatch data", OK, ERROR, "Monitoring data readable again")
        return None

    # ---- queries for the UI / messages -----------------------------------------------------
    def open_issues(self):
        return self.db.all("SELECT * FROM issues WHERE site=? AND resolved IS NULL ORDER BY level DESC, started",
                           (self.site,))

    def counts(self) -> tuple[int, int]:
        rows = self.db.all("SELECT status, COUNT(*) n FROM check_state WHERE site=? GROUP BY status", (self.site,))
        c = {r["status"]: r["n"] for r in rows}
        return c.get(ERROR, 0), c.get(WARNING, 0)


def window_active(m, now: float, zone: ZoneInfo) -> bool:
    if m["kind"] == "once":
        return (m["start_ts"] or 0) <= now < (m["end_ts"] or 0)
    # weekly: weekday 0=Monday, start minute of day, duration in minutes (may cross midnight)
    local = datetime.fromtimestamp(now, zone)
    for back in (0, 1, 2, 3, 4, 5, 6, 7):
        day = local.date() - timedelta(days=back)
        if day.weekday() != m["weekday"]:
            continue
        start = datetime(day.year, day.month, day.day, tzinfo=zone) + timedelta(minutes=m["start_min"])
        if start <= local < start + timedelta(minutes=m["dur_min"]):
            return True
    return False
