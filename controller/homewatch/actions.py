"""Buttons in notifications (ntfy shows them under the message): one-time links that snooze a check for a day or
acknowledge a problem (no more reminders), without signing in. Each link is a random token, stored as a hash,
for one action on one check, valid for 24 hours and used once. The phone must reach homeWatch's address
(home network or VPN); homeWatch is never opened to the internet for this."""

from __future__ import annotations

import secrets as pysecrets
import time

from .auth import _token_hash

VALID = 24 * 3600
SNOOZE = 24 * 3600
KINDS = {"snooze": "Snooze 1 day", "ack": "Acknowledge"}


def make(db, kind: str, key: str, issue_id: int | None, now: float) -> str:
    token = pysecrets.token_urlsafe(24)
    db.run("INSERT INTO action_tokens(token_hash,kind,check_key,issue_id,created,expires) VALUES(?,?,?,?,?,?)",
           (_token_hash(token), kind, key, issue_id, now, now + VALID))
    db.run("DELETE FROM action_tokens WHERE expires<?", (now - 7 * 86400,))
    return token


def links(db, site: str, base: str, key: str, now: float) -> list[dict]:
    """The buttons for one open problem: [{label, url}]; none when homeWatch's address isn't set."""
    if not base.startswith(("https://", "http://")):
        return []
    issue = db.one("SELECT id FROM issues WHERE site=? AND check_key=? AND resolved IS NULL", (site, key))
    base = base.rstrip("/")
    return [{"label": label, "url": f"{base}/act/{make(db, kind, key, issue['id'] if issue else None, now)}"}
            for kind, label in KINDS.items()]


def use(db, site: str, token: str, now: float | None = None) -> tuple[bool, str]:
    """Do what the link says, once: (done, what to show)."""
    from .auth import audit

    now = now or time.time()
    row = db.one("SELECT * FROM action_tokens WHERE token_hash=?", (_token_hash(token),))
    if row is None or row["expires"] < now:
        return False, "This link has expired or is not valid."
    if row["used"]:
        return False, "This link was already used."
    db.run("UPDATE action_tokens SET used=? WHERE id=?", (now, row["id"]))
    key = row["check_key"]
    if row["kind"] == "snooze":
        db.run("INSERT INTO snoozes(site,check_key,until,reason,created_by,created) VALUES(?,?,?,?,?,?)",
               (site, key, now + SNOOZE, "from a notification", "notification", now))
        audit(db, "notification", "snooze", f"{key} for 24 h (button in a notification)")
        return True, f"Snoozed {key} for 24 hours."
    if row["kind"] == "ack":
        db.run("UPDATE issues SET acked=? WHERE site=? AND check_key=? AND resolved IS NULL AND acked IS NULL",
               (now, site, key))
        audit(db, "notification", "acknowledge", f"{key} (button in a notification)")
        return True, f"Acknowledged {key}: no more reminders while it stays open."
    return False, "Unknown action."
