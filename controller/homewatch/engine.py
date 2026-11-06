"""Check engine: turns check results into confirmed state changes, issues and events to notify.

Rules (defaults from running it in a real home):
- a new level must hold for CONFIRM runs in a row before it counts (blips are ignored)
- every open / worse / better / resolve is stored as an issue
- snoozed checks and maintenance windows record issues but hold their notifications; anything
  still open when the silence ends is sent then
- open issues are repeated every REMIND_H hours
- after FAIL_ALERT runs in a row that can't read data, homeWatch alerts about itself
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .db import DB
from .modules import ERROR, OK, WARNING, CheckDef, CheckResult, Thresholds

DEFAULT_TIMING = {"interval": 120, "confirm": 2, "remind_h": 12, "fail_alert": 5}


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

    @property
    def route_level(self) -> int:
        """Which level a channel filter sees: back-to-OK follows the level that opened it."""
        return self.worst if self.kind == "resolve" else self.level


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
                        db.run("UPDATE issues SET detail=? WHERE site=? AND check_key=? AND resolved IS NULL",
                               (res.detail, self.site, res.key))
                    continue
                if st["pending"] == level:
                    if res.observed_at is not None and res.observed_at == st["pending_obs"]:
                        continue  # the same reading again confirms nothing
                    count, since = st["pending_count"] + 1, st["pending_since"]
                else:
                    count, since = 1, now
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
        return events

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
            return Event("resolve", res.key, res.name, OK, issue["worst"], res.detail, issue["started"], since)
        if issue is None:
            db.run("INSERT INTO issues(site,check_key,level,worst,started,detail,notified) VALUES(?,?,?,?,?,?,?)",
                   (self.site, res.key, new, new, since, res.detail, 0 if silent else 1))
            return None if silent else Event("open", res.key, res.name, new, new, res.detail, since)
        worst = max(issue["worst"], new)
        db.run("UPDATE issues SET level=?, worst=?, detail=? WHERE id=?", (new, worst, res.detail, issue["id"]))
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
            if i["notified"] and now - i["started"] >= hours * 3600 and why(i["check_key"]) is None:
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
