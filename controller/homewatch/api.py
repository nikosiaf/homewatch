"""Read-only JSON API, /api/v1/…, for scripts, Home Assistant and other tools. Sign in with a token made on the
System page (Authorization: Bearer <token>); a token can only read. Times are Unix seconds, levels are
0 OK, 1 WARNING, 2 ERROR (level_word has the name)."""

from __future__ import annotations

import json
import secrets as pysecrets
import time

from .auth import _token_hash
from .modules import LEVEL_WORD
from .modules.registry import MODULES

TOKEN_PREFIX = "hw_"
MAX_DAYS = 400
LIMIT = 1000


def new_token(db, name: str, by: str) -> str:
    token = TOKEN_PREFIX + pysecrets.token_urlsafe(32)
    db.run("INSERT INTO api_tokens(name,token_hash,created,created_by) VALUES(?,?,?,?)",
           (name, _token_hash(token), time.time(), by))
    return token


def token_ok(db, header: str) -> str | None:
    """The token's name when the Authorization header carries a valid one, else None."""
    if not header.startswith("Bearer "):
        return None
    row = db.one("SELECT id, name, last_used FROM api_tokens WHERE token_hash=?", (_token_hash(header[7:].strip()),))
    if row is None:
        return None
    now = time.time()
    if not row["last_used"] or now - row["last_used"] > 60:  # not a write per request
        db.run("UPDATE api_tokens SET last_used=? WHERE id=?", (now, row["id"]))
    return row["name"]


def _lv(n: int | None) -> dict:
    return {"level": n or 0, "level_word": LEVEL_WORD.get(n or 0, "OK")}


def _module(key: str) -> str:
    return key.split(".", 1)[0]


def status(rt) -> dict:
    now = time.time()
    states = {r["check_key"]: r for r in rt.db.all("SELECT * FROM check_state WHERE site=?", (rt.cfg.site,))}
    mods = []
    for m in MODULES.values():
        mine = [r for r in rt.results if _module(r.key) == m.id]
        if not mine:
            continue
        worst = max((states[r.key]["status"] if r.key in states else 0) for r in mine)
        last = m.reading_ts(rt) or rt.engine.last_observed(m.id)
        mods.append({"id": m.id, "name": m.name, **_lv(worst), "checks": len(mine), "last_reading": last})
    overall = max((x["level"] for x in mods), default=0)
    errors, warnings = rt.engine.counts()
    from . import __version__

    return {"home": rt.db.get("home.name") or "Home", "version": __version__, "now": now, "last_run": rt.last_run,
            **_lv(overall), "errors": errors, "warnings": warnings,
            "open_issues": len(rt.engine.open_issues()),
            "todos": rt.db.one("SELECT COUNT(*) n FROM todos WHERE site=? AND done IS NULL", (rt.cfg.site,))["n"],
            "modules": mods}


def checks(rt) -> list[dict]:
    states = {r["check_key"]: r for r in rt.db.all("SELECT * FROM check_state WHERE site=?", (rt.cfg.site,))}
    why = rt.engine.silences(time.time())
    out = []
    for r in rt.results:
        st = states.get(r.key)
        out.append({"key": r.key, "module": _module(r.key), "name": r.name, **_lv(st["status"] if st else 0),
                    "detail": r.detail, "value": r.value, "unit": r.unit, "since": st["since"] if st else None,
                    "pending": st["pending"] if st else None, "silenced": why(r.key), "suppressed_by": r.suppressed_by})
    return out


def _days(q: dict, default: float) -> float:
    try:
        return max(0.01, min(float(q.get("days", default)), MAX_DAYS))
    except ValueError:
        return default


def issues(rt, q: dict) -> list[dict]:
    since = time.time() - _days(q, 7) * 86400
    sql = "SELECT * FROM issues WHERE site=? AND (started>? OR resolved>? OR resolved IS NULL)"
    args: list = [rt.cfg.site, since, since]
    if q.get("open") in ("1", "true"):
        sql += " AND resolved IS NULL"
    if q.get("module") in MODULES:
        sql += " AND check_key LIKE ?"
        args.append(q["module"] + ".%")
    names = rt.names()
    return [{"id": r["id"], "key": r["check_key"], "module": _module(r["check_key"]),
             "name": names.get(r["check_key"]) or r["name"] or r["check_key"], **_lv(r["level"]),
             "worst": r["worst"], "started": r["started"], "resolved": r["resolved"], "detail": r["detail"],
             "resolved_detail": r["resolved_detail"], "incident": r["incident_id"], "feedback": r["feedback"]}
            for r in rt.db.all(sql + f" ORDER BY started DESC LIMIT {LIMIT}", tuple(args))]


def incidents(rt, q: dict) -> list[dict]:
    from . import causes

    since = time.time() - _days(q, 30) * 86400
    out = []
    for inc in rt.db.all("SELECT * FROM incidents WHERE site=? AND (started>? OR ended>? OR ended IS NULL) "
                         f"ORDER BY started DESC LIMIT {LIMIT}", (rt.cfg.site, since, since)):
        rows = rt.db.all("SELECT id, check_key FROM issues WHERE incident_id=? ORDER BY started", (inc["id"],))
        out.append({"id": inc["id"], "started": inc["started"], "ended": inc["ended"], **_lv(inc["worst"]),
                    "cause": inc["cause"], "issues": [r["id"] for r in rows], "checks": [r["check_key"] for r in rows],
                    "evidence": [{"ts": ts, "module": m, "device": d, "detail": det, "exact": bool(x)}
                                 for ts, m, d, det, x in causes.loads(inc["evidence"])]})
    return out


def events(rt, q: dict) -> list[dict]:
    since = time.time() - _days(q, 7) * 86400
    return [{"ts": r["ts"], "module": r["module"], "device": r["device"], "kind": r["kind"], "detail": r["detail"],
             "exact": bool(r["exact"])}
            for r in rt.db.all(f"SELECT * FROM events WHERE site=? AND ts>? ORDER BY ts DESC LIMIT {LIMIT}",
                               (rt.cfg.site, since))]


def todos(rt) -> list[dict]:
    return [{"key": r["check_key"], "module": _module(r["check_key"]), "name": r["name"], "item": r["item"],
             "first_seen": r["first_seen"]}
            for r in rt.db.all("SELECT * FROM todos WHERE site=? AND done IS NULL ORDER BY first_seen", (rt.cfg.site,))]


def dumps(data) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))
