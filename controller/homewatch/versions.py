"""Configuration versions: a snapshot of every setting after each change, and restoring one.

A snapshot holds what an admin configures (module settings, thresholds, timing, home, report schedule,
channels, maintenance windows). It never holds secrets, users, sessions, snoozes or history; restoring
leaves those as they are, except credentials whose destination the restored settings change.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone

import yaml

from .db import DB

KEEP = 200
# settings keys that are configuration; the rest of the settings table is runtime state
CONFIG_KEYS = ("engine.timing", "home.name", "home.tz", "home.url", "report.schedule", "updates.check")
CONFIG_PREFIXES = ("module.",)
CHECK_COLS = ("site", "check_key", "enabled", "warn", "error")
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
        "checks": [dict(r) for r in db.all(f"SELECT {', '.join(CHECK_COLS)} FROM checks ORDER BY site, check_key")],  # noqa: S608
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


def load(db: DB, vid: int) -> dict | None:
    """Version vid's snapshot, without secret fields older versions may hold."""
    row = db.one("SELECT data FROM config_versions WHERE id=?", (vid,))
    if row is None:
        return None
    snap = json.loads(row["data"])
    strip_secret_fields(snap)
    return snap


def restore(db: DB, vid: int, before=None) -> dict | None:
    """Put version vid's configuration back. Returns its snapshot, or None when it doesn't exist.

    before(snapshot), when given, runs first, may change the snapshot, and returns what to run in its transaction.
    """
    snap = load(db, vid)
    if snap is None:
        return None
    apply(db, snap, before(snap) if before else None)
    return snap


def _secret_fields() -> dict[str, list[str]]:
    """Settings key -> fields of that module which only ever live in the encrypted secrets."""
    from .modules.registry import MODULES

    return {f"module.{m.id}": [f.key for f in getattr(m, "fields", []) if getattr(f, "secret", False)]
            for m in MODULES.values()}


def strip_secret_fields(snap: dict) -> bool:
    """Drop secret fields from a snapshot's settings; True when any was there."""
    from .notify import split_config

    found = False
    for key, names in _secret_fields().items():
        for name in names:
            if name in snap.get("settings", {}).get(key, {}):
                del snap["settings"][key][name]
                found = True
    for c in snap.get("channels", []):
        try:
            config = json.loads(c["config"])
            public, private = split_config(c["kind"], config)
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        if private:
            c["config"] = json.dumps(public)
            found = True
    return found


def move_secret_fields(db: DB, secrets) -> bool:
    """Older versions kept these fields in plain settings: encrypt them and clean every stored copy."""
    from .notify import split_config, store_private

    moved = False
    for row in db.all("SELECT id, kind, config FROM channels"):
        public, private = split_config(row["kind"], json.loads(row["config"]))
        if private:
            store_private(secrets, row["id"], private)
            db.run("UPDATE channels SET config=? WHERE id=?", (json.dumps(public), row["id"]))
            moved = True
    for key, names in _secret_fields().items():
        saved = db.get(key)
        if isinstance(saved, dict) and any(n in saved for n in names):
            for n in names:
                value = saved.pop(n, "")
                if value:
                    secrets.put(f"{key}.{n}", value)
            db.set(key, saved)
            moved = True
    for row in db.all("SELECT id, data FROM config_versions"):
        snap = json.loads(row["data"])
        if strip_secret_fields(snap):
            db.run("UPDATE config_versions SET data=? WHERE id=?", (json.dumps(snap, sort_keys=True), row["id"]))
            moved = True
    return moved


def scrub_exports(path_dir: str) -> None:
    """Remove secret fields from export files written by older versions."""
    names = [n for ns in _secret_fields().values() for n in ns]
    if not names or not os.path.isdir(path_dir):
        return
    line = re.compile(rf"^\s+({'|'.join(names)}):.*\n", re.M)
    for name in os.listdir(path_dir):
        if _EXPORT_NAME.match(name):
            path = os.path.join(path_dir, name)
            with open(path) as f:
                text = f.read()
            clean = line.sub("", text)
            try:
                doc = yaml.safe_load(clean)
                if isinstance(doc, dict) and strip_secret_fields({"channels": doc.get("channels") or []}):
                    clean = _HEADER + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)
            except yaml.YAMLError:
                pass
            if clean != text:
                with open(path, "w") as f:
                    f.write(clean)


def apply(db: DB, snap: dict, within=None) -> None:
    """Replace the configuration with a (validated) snapshot; secrets, users and history stay.

    within(), when given, runs in the same transaction: both happen, or neither.
    """
    strip_secret_fields(snap)  # an export from an older version may still hold them
    with db.tx() as t:
        for r in t.all("SELECT key FROM settings"):
            if is_config(r["key"]):
                t.run("DELETE FROM settings WHERE key=?", (r["key"],))
        for k, v in snap["settings"].items():
            t.run("INSERT INTO settings(key, value) VALUES(?,?)", (k, json.dumps(v)))
        t.run("DELETE FROM checks")
        for c in snap["checks"]:
            t.run("INSERT INTO checks(site, check_key, enabled, warn, error) VALUES(?,?,?,?,?)",
                  tuple(c[k] for k in CHECK_COLS))
        t.run("DELETE FROM channels")
        for c in snap["channels"]:
            t.run(f"INSERT INTO channels({', '.join(CHANNEL_COLS)}) VALUES({', '.join('?' * len(CHANNEL_COLS))})",  # noqa: S608
                  tuple(c[k] for k in CHANNEL_COLS))
        t.run("DELETE FROM outbox WHERE channel_id NOT IN (SELECT id FROM channels)")
        t.run("DELETE FROM maintenance")
        for m in snap["maintenance"]:
            t.run(f"INSERT INTO maintenance({', '.join(MAINT_COLS)}) VALUES({', '.join('?' * len(MAINT_COLS))})",  # noqa: S608
                  tuple(m[k] for k in MAINT_COLS))
        if within:
            within()


# ---- YAML export / import --------------------------------------------------------------------
FORMAT = 1
MAX_IMPORT = 1_000_000  # bytes
EXPORTS_KEPT = 14
_EXPORT_NAME = re.compile(r"^homewatch-config-\d{4}-\d{2}-\d{2}\.yaml$")
_HEADER = ("# homewatch configuration export. It holds no passwords, tokens, private notification addresses or users:\n"
           "# after importing it elsewhere, enter those again on the module and notification pages.\n")


def to_yaml(db: DB) -> str:
    from . import __version__

    body = {"format": FORMAT, "homewatch": __version__, "exported": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            **snapshot(db)}
    return _HEADER + yaml.safe_dump(body, sort_keys=False, allow_unicode=True)


def from_yaml(text: str) -> tuple[dict | None, str]:
    """Parse and check an export. Returns (snapshot, "") or (None, what is wrong)."""
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as e:
        return None, f"not valid YAML ({str(e).splitlines()[0]})"
    if not isinstance(doc, dict) or doc.get("format") != FORMAT:
        return None, "not a homewatch configuration export (format 1)"
    settings = doc.get("settings") or {}
    if not isinstance(settings, dict):
        return None, "settings: must be a mapping"
    unknown = [k for k in settings if not is_config(str(k))]
    if unknown:
        return None, f"settings: unknown keys {', '.join(map(str, unknown[:5]))}"
    snap = {"settings": settings}
    for part, cols in (("checks", CHECK_COLS), ("channels", CHANNEL_COLS), ("maintenance", MAINT_COLS)):
        rows = doc.get(part) or []
        if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
            return None, f"{part}: must be a list of entries"
        for i, r in enumerate(rows, 1):
            missing = [c for c in cols if c not in r]
            if missing:
                return None, f"{part} entry {i}: missing {', '.join(missing)}"
            bad = [c for c in cols if r[c] is not None and not isinstance(r[c], (str, int, float))]
            if bad:
                return None, f"{part} entry {i}: {', '.join(bad)} must be a plain value"
        snap[part] = [{c: r[c] for c in cols} for r in rows]
    return snap, ""


def clean_import(snap: dict) -> str:
    """Hold an imported snapshot to the rules of the module and channel forms and put the normalized values
    in it, so that what is checked is what gets stored. Returns what is wrong."""
    from .modules.registry import MODULES
    from .notify import imported as channel_imported

    for m in MODULES.values():
        settings = snap["settings"].get(f"module.{m.id}")
        if settings is None:
            continue
        if not isinstance(settings, dict):
            return f"{m.name}: settings must be a mapping"
        if not isinstance(settings.get("enabled", True), bool):
            return f"{m.name}: enabled must be true or false"
        settings, err = m.imported(settings)
        if err:
            return f"{m.name}: {err}"
        snap["settings"][f"module.{m.id}"] = settings
    for c in snap["channels"]:
        try:
            config = json.loads(c["config"])
        except (TypeError, ValueError):
            config = None
        if not isinstance(config, dict):
            return f"channel {c['name']}: config is not valid"
        if isinstance(c["id"], bool) or not isinstance(c["id"], int) or c["enabled"] not in (0, 1):
            return f"channel {c['name']}: id or enabled is not valid"
        try:
            levels = json.loads(c["levels"])
        except (TypeError, ValueError):
            levels = None
        if not isinstance(levels, list) or not all(lv in (1, 2) for lv in levels):
            return f"channel {c['name']}: levels are not valid"
        config, err = channel_imported(str(c["kind"]), config)
        if err:
            return f"channel {c['name']}: {err}"
        c["config"] = json.dumps(config)
    return ""


def destination_change(module, old: dict | None, new: dict | None) -> str:
    """Why the module's retained credentials would no longer go to the same place as safely; '' when they would."""
    if old is None or new is None:
        return "" if old is new else ("removed" if new is None else "newly added")
    for k in module.destination_keys:
        if old.get(k) != new.get(k):
            return f"{k} changes from {old.get(k)} to {new.get(k)}"
    for k in module.transport_keys:
        if old.get(k) and not new.get(k):
            return f"{k} is turned off"
    return ""


def write_export(db: DB, folder: str, today: str) -> str:
    """The nightly export: one file per day, the last EXPORTS_KEPT kept. Returns its path."""
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"homewatch-config-{today}.yaml")
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        f.write(to_yaml(db))
    os.replace(path + ".tmp", path)
    for old in list_exports(folder)[EXPORTS_KEPT:]:
        os.remove(os.path.join(folder, old))
    return path


def list_exports(folder: str) -> list[str]:
    """Export file names, newest first."""
    try:
        return sorted((n for n in os.listdir(folder) if _EXPORT_NAME.match(n)), reverse=True)
    except FileNotFoundError:
        return []


def export_name_ok(name: str) -> bool:
    return bool(_EXPORT_NAME.match(name))
