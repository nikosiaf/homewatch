"""To do: what waits for the admin (updates, firmware, a reboot to finish an update) is not a problem.

A check marked todo=True reports "something to do" as a WARNING, as before. Here that becomes:
- OK with "To do: …" on the Status page, and one row in the To do list (first seen, what it is)
- a WARNING, a normal issue, only once it has waited "warn" days (the check's editable limit; empty = never)
- done when the check is OK again
"""

from __future__ import annotations

from .db import DB
from .modules import OK, WARNING, CheckResult, Module, Thresholds

DAY = 86400


def days(seconds: float) -> str:
    d = seconds / DAY
    return f"{d:.0f} day{'s' if round(d) != 1 else ''}" if d >= 1 else "less than a day"


def apply(db: DB, site: str, module: Module, th: dict[str, Thresholds], results: list[CheckResult],
          now: float) -> list[CheckResult]:
    defs = {f"{module.id}.{c.id}": c for c in module.checks if c.todo}
    for r in results:
        d = defs.get(r.key)
        if d is None or r.unknown or r.suppressed_by:
            continue
        row = db.one("SELECT * FROM todos WHERE site=? AND check_key=? AND done IS NULL", (site, r.key))
        if r.status == OK:
            if row:
                db.run("UPDATE todos SET done=? WHERE id=?", (now, row["id"]))
            r.value = 0
            continue
        if row is None:
            db.run("INSERT INTO todos(site,check_key,name,item,first_seen,last_seen) VALUES(?,?,?,?,?,?)",
                   (site, r.key, r.name, r.detail, now, now))
            first = now
        else:
            db.run("UPDATE todos SET item=?, name=?, last_seen=? WHERE id=?", (r.detail, r.name, now, row["id"]))
            first = row["first_seen"]
        limit = th[d.id].warn if d.id in th else d.warn
        age = now - first
        r.value, r.unit = round(age / DAY, 1), "days"
        if limit is not None and age >= limit * DAY:
            r.status = WARNING
            r.detail = f"{r.detail} (waiting {days(age)})"
            r.since = first + limit * DAY  # it became overdue then, not when this run saw it
        else:
            r.status = OK
            r.detail = f"To do: {r.detail}"
    return results


def close_module(db: DB, site: str, module_id: str, now: float) -> None:
    """A module switched off: its to-dos are no longer known."""
    db.run("UPDATE todos SET done=? WHERE site=? AND check_key LIKE ? AND done IS NULL", (now, site, f"{module_id}.%"))


def open_items(db: DB, site: str) -> list:
    return db.all("SELECT * FROM todos WHERE site=? AND done IS NULL ORDER BY first_seen", (site,))
