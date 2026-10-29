"""Configuration versions: a snapshot of every setting after each change, and restoring one.

A snapshot holds what an admin configures (module settings, thresholds, timing, home, report schedule,
channels, maintenance windows). It never holds secrets, users, sessions, snoozes or history; restoring
leaves those as they are.
"""

from __future__ import annotations

import json
import time

from .db import DB

KEEP = 200
# settings keys that are configuration; the rest of the settings table is runtime state
CONFIG_KEYS = ("engine.timing", "home.name", "home.tz", "home.url", "report.schedule", "updates.check")
CONFIG_PREFIXES = ("module.",)
CHANNEL_COLS = ("id", "kind", "name", "enabled", "levels", "quiet_start", "quiet_end", "weekly_report", "config")
MAINT_COLS = ("id", "site", "name", "kind", "start_ts", "end_ts", "weekday", "start_min", "dur_min", "checks",
              "created_by", "created")
LABELS = {"engine.timing": "check timing", "home.name": "home", "home.tz": "home", "home.url": "home",
          "report.schedule": "weekly report", "updates.check": "update check"}


def is_config(key: str) -> bool:
    return key in CONFIG_KEYS or key.startswith(CONFIG_PREFIXES)


def snapshot(db: DB) -> dict:
    return {
        "settings": {r["key"]: json.loads(r["value"]) for r in db.all("SELECT key, value FROM settings ORDER BY key")
                     if is_config(r["key"])},
        "checks": [dict(r) for r in db.all("SELECT site, check_key, enabled, warn, error FROM checks "
                                           "ORDER BY site, check_key")],
        "channels": [dict(r) for r in db.all(f"SELECT {', '.join(CHANNEL_COLS)} FROM channels ORDER BY id")],  # noqa: S608
        "maintenance": [dict(r) for r in db.all(f"SELECT {', '.join(MAINT_COLS)} FROM maintenance ORDER BY id")],  # noqa: S608
    }


def changes(old: dict | None, new: dict) -> list[str]:
    """Short names of what differs between two snapshots, e.g. ["Home Assistant", "thresholds"]."""
    from .modules.registry import MODULES

    old = old or {"settings": {}, "checks": [], "channels": [], "maintenance": []}
    out: list[str] = []

    def add(name):
        if name not in out:
            out.append(name)

    for k in sorted(set(old["settings"]) | set(new["settings"])):
        if old["settings"].get(k) != new["settings"].get(k):
            mid = k.removeprefix("module.").split(".")[0]
            add(MODULES[mid].name if k.startswith("module.") and mid in MODULES else LABELS.get(k, k))
    if old["checks"] != new["checks"]:
        add("thresholds")
    if old["channels"] != new["channels"]:
        add("notification channels")
    if old["maintenance"] != new["maintenance"]:
        add("maintenance windows")
    return out


def latest(db: DB):
    return db.one("SELECT * FROM config_versions ORDER BY id DESC LIMIT 1")


def record(db: DB, username: str, action: str) -> int | None:
    """Store the current configuration as a new version when it differs from the last one."""
    snap = snapshot(db)
    data = json.dumps(snap, sort_keys=True)
    last = latest(db)
    if last is not None and last["data"] == data:
        return None
    what = changes(json.loads(last["data"]) if last else None, snap)
    if last is not None and not what:
        return None
    vid = db.run("INSERT INTO config_versions(ts, username, action, summary, data) VALUES(?,?,?,?,?)",
                 (time.time(), username, action, ", ".join(what) if last else "first version", data))
    db.run("DELETE FROM config_versions WHERE id <= (SELECT id FROM config_versions ORDER BY id DESC "
           "LIMIT 1 OFFSET ?)", (KEEP,))
    return vid


def restore(db: DB, vid: int) -> dict | None:
    """Put version vid's configuration back. Returns its snapshot, or None when it doesn't exist."""
    row = db.one("SELECT data FROM config_versions WHERE id=?", (vid,))
    if row is None:
        return None
    snap = json.loads(row["data"])
    with db.tx() as t:
        for r in t.all("SELECT key FROM settings"):
            if is_config(r["key"]):
                t.run("DELETE FROM settings WHERE key=?", (r["key"],))
        for k, v in snap["settings"].items():
            t.run("INSERT INTO settings(key, value) VALUES(?,?)", (k, json.dumps(v)))
        t.run("DELETE FROM checks")
        for c in snap["checks"]:
            t.run("INSERT INTO checks(site, check_key, enabled, warn, error) VALUES(?,?,?,?,?)",
                  (c["site"], c["check_key"], c["enabled"], c["warn"], c["error"]))
        t.run("DELETE FROM channels")
        for c in snap["channels"]:
            t.run(f"INSERT INTO channels({', '.join(CHANNEL_COLS)}) VALUES({', '.join('?' * len(CHANNEL_COLS))})",  # noqa: S608
                  tuple(c[k] for k in CHANNEL_COLS))
        t.run("DELETE FROM outbox WHERE channel_id NOT IN (SELECT id FROM channels)")
        t.run("DELETE FROM maintenance")
        for m in snap["maintenance"]:
            t.run(f"INSERT INTO maintenance({', '.join(MAINT_COLS)}) VALUES({', '.join('?' * len(MAINT_COLS))})",  # noqa: S608
                  tuple(m[k] for k in MAINT_COLS))
    return snap
