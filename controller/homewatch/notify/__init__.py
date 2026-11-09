"""Notifications: render events per channel, queue them in the outbox, deliver with retries.

Messages are queued first and sent by a separate loop, so nothing is lost while the line is down;
when several messages wait for the same channel they go out as one.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from ..db import DB
from ..engine import Event, tz
from ..modules import ERROR, LEVEL_WORD, OK, WARNING
from ..secrets import Secrets
from . import healthchecks, mail, ntfy, telegram, webhook
from .base import RateLimited

log = logging.getLogger("homewatch.notify")

ICON = {OK: "✅", WARNING: "🟡", ERROR: "🔴"}
KINDS = {"telegram": telegram, "ntfy": ntfy, "email": mail, "webhook": webhook}  # channels that receive messages
PROVIDERS = {**KINDS, "healthchecks": healthchecks}  # everything that can be added on the Notifications page
COMING_SOON: tuple[str, ...] = ()
RETRY_MAX = 120
SEND_LIMIT = 60  # seconds one message may take
JOIN_ROOM = 300  # left free in a long message's parts for its title and "delivered late" note


@dataclass
class Message:
    level: int
    title: str
    body: str


def channel_secret(channel_id: int) -> str:
    return f"channel.{channel_id}"


def channel_private(channel_id: int) -> str:
    return f"channel.{channel_id}.private"


def private_fields(kind: str) -> tuple:
    """Config fields that work as a credential (e.g. a hard-to-guess URL): encrypted, not in versions or exports."""
    return getattr(PROVIDERS.get(kind), "PRIVATE", ())


def split_config(kind: str, config: dict) -> tuple[dict, dict]:
    """(what may be shared, what is kept encrypted)."""
    names = private_fields(kind)
    return ({k: v for k, v in config.items() if k not in names}, {k: v for k, v in config.items() if k in names})


def store_private(secrets, channel_id: int, private: dict) -> None:
    if private:
        secrets.put(channel_private(channel_id), json.dumps(private))


def full_config(secrets, ch) -> dict:
    """The channel's config with its encrypted fields, for sending; never store the result."""
    return json.loads(ch["config"]) | json.loads(secrets.get(channel_private(ch["id"])) or "{}")


def forget_channel(secrets, channel_id: int) -> None:
    secrets.delete(channel_secret(channel_id))
    secrets.delete(channel_private(channel_id))


def imported(kind: str, config: dict) -> tuple[dict, str]:
    """A channel's config from an imported file, by the rules of its form: (what to store, what is wrong)."""
    mod = PROVIDERS.get(kind)
    if mod is None:
        return config, f"unknown channel type {kind}"
    return mod.imported(config) if hasattr(mod, "imported") else (config, "")


def destination(kind: str, config: dict) -> list:
    """Where the channel's credentials go; a change means they must be entered again."""
    return [config.get(k) for k in getattr(PROVIDERS.get(kind), "DESTINATION", ())]


def _dur(seconds: float) -> str:
    m = int(seconds // 60)
    if m < 60:
        return f"{max(m, 1)} min"
    h, m = divmod(m, 60)
    if h < 48:
        return f"{h} h {m} min" if m else f"{h} h"
    return f"{h // 24} d {h % 24} h"


SHORT = 300  # a message keeps each check's text short; the full text is on the Status page


def _short(text: str) -> str:
    text = text or ""
    return text if len(text) <= SHORT else text[:SHORT - 1].rstrip(" ;,") + "…"


def _hm(ts: float, zone: ZoneInfo) -> str:
    return datetime.fromtimestamp(ts, zone).strftime("%H:%M")


def render(events: list[Event], home: str, open_counts: tuple[int, int], link: str, zone: ZoneInfo) -> Message:
    level = max((e.level for e in events), default=OK)
    if all(e.kind in ("resolve", "selfok", "close") for e in events):
        title = f"{ICON[OK]} {home}: back to OK"
    elif all(e.kind in ("resolve", "selfok", "close", "partial") for e in events):
        title = f"{ICON[OK]} {home}: partly back to OK"
    elif all(e.kind == "remind" for e in events):
        title = f"{ICON[level]} {home}: still {LEVEL_WORD[level]} (reminder)"
    else:
        title = f"{ICON[level]} {home}: {LEVEL_WORD[level]}"
    lines = []
    for e in sorted(events, key=lambda e: (-e.level, e.name)):
        if e.kind in ("close", "partial"):
            if e.kind == "close":
                head = f"Incident over: {_hm(e.started, zone)}–{_hm(e.resolved, zone)}, {_dur(e.resolved - e.started)}"
            else:
                head = f"Incident since {_hm(e.started, zone)} still open"
            lines.append(f"{ICON[OK]} {head} · {len(e.items)} check{'s' if len(e.items) != 1 else ''} back to OK")
            for name, worst, started, resolved, detail in sorted(e.items, key=lambda i: (i[2], i[0])):
                lines.append(f"    {ICON[worst]}→{ICON[OK]} {name}: {LEVEL_WORD[worst]} for {_dur(resolved - started)} "
                             f"({_hm(started, zone)}–{_hm(resolved, zone)})")
            if e.still:
                lines.append(f"    still open: {', '.join(e.still)}")
            lines += _cause(e, zone)
            continue
        if e.kind in ("resolve", "selfok"):
            span = ""
            if e.started and e.resolved:
                span = f" after {_dur(e.resolved - e.started)} ({_hm(e.started, zone)}–{_hm(e.resolved, zone)})"
            lines.append(f"{ICON[OK]} OK · {e.name}{span}\n    {_short(e.detail)}")
            lines += _cause(e, zone)
        else:
            since = f" since {_hm(e.started, zone)}" if e.kind == "remind" and e.started else ""
            lines.append(f"{ICON[e.level]} {LEVEL_WORD[e.level]} · {e.name}{since}\n    {_short(e.detail)}")
    errors, warnings = open_counts
    footer = "All checks OK." if not (errors or warnings) else f"Open now: {errors} error, {warnings} warning"
    body = "\n".join(lines) + f"\n\n{footer}" + (f"\n{link}" if link else "")
    return Message(level, title, body)


EVIDENCE_LINES = 8  # a message lists at most this many device events; History has them all


def _cause(e: Event, zone: ZoneInfo) -> list[str]:
    """The likely cause of an incident and the device events behind it, for a message."""
    if not e.cause and not e.evidence:
        return []
    out = [f"    💡 Likely cause: {e.cause}"] if e.cause else []
    evidence = e.evidence or []
    for ts, _module, device, detail, exact in evidence[:EVIDENCE_LINES]:
        out.append(f"      {'' if exact else '~'}{datetime.fromtimestamp(ts, zone).strftime('%H:%M:%S')} {device}: {detail}")
    if len(evidence) > EVIDENCE_LINES:
        out.append(f"      … {len(evidence) - EVIDENCE_LINES} more on the History page")
    return out


def in_quiet(channel, now: float, zone: ZoneInfo) -> float | None:
    """When the channel is in quiet hours, return the timestamp they end."""
    qs, qe = channel["quiet_start"], channel["quiet_end"]
    if not qs or not qe:
        return None
    local = datetime.fromtimestamp(now, zone)
    sh, sm = map(int, qs.split(":"))
    eh, em = map(int, qe.split(":"))
    for back in (0, 1):
        day = local.date() - timedelta(days=back)
        start = datetime(day.year, day.month, day.day, sh, sm, tzinfo=zone)
        end = datetime(day.year, day.month, day.day, eh, em, tzinfo=zone)
        if end <= start:
            end += timedelta(days=1)
        if start <= local < end:
            return end.timestamp()
    return None


class Notifier:
    def __init__(self, db: DB, secrets: Secrets, http: httpx.AsyncClient | None = None):
        self.db = db
        self.secrets = secrets
        self.http = http or httpx.AsyncClient(timeout=20)

    def channels(self, enabled_only: bool = True, messages_only: bool = True):
        q = "SELECT * FROM channels WHERE 1=1"
        if enabled_only:
            q += " AND enabled=1"
        if messages_only:
            q += f" AND kind IN ({','.join(repr(k) for k in KINDS)})"
        return self.db.all(q + " ORDER BY id")

    async def heartbeat(self) -> None:
        """Ping every enabled healthchecks.io check after a successful run."""
        for ch in self.db.all("SELECT * FROM channels WHERE enabled=1 AND kind='healthchecks'"):
            try:
                await healthchecks.ping(self.http, full_config(self.secrets, ch))
                self.db.set(f"heartbeat.{ch['id']}.last_ok", time.time())
            except RuntimeError as e:
                log.warning("heartbeat %s: %s", ch["name"], e)

    def dispatch(self, events: list[Event], open_counts: tuple[int, int], now: float | None = None) -> int:
        """Queue one message per channel that wants at least one of the events."""
        if not events:
            return 0
        now = now or time.time()
        zone = tz(self.db)
        home = self.db.get("home.name") or "Home"
        link = self.db.get("home.url") or ""
        queued = 0
        for ch in self.channels():
            levels = set(json.loads(ch["levels"]))
            mine = [e for e in events if e.route_level in levels]
            if not mine:
                continue
            msg = render(mine, home, open_counts, link, zone)
            next_try = now
            if msg.level < ERROR:
                quiet_end = in_quiet(ch, now, zone)
                if quiet_end:
                    next_try = quiet_end
            self.queue(ch["id"], msg, now, next_try)
            queued += 1
        return queued

    def queue(self, channel_id: int, msg: Message, now: float, next_try: float | None = None) -> None:
        self.db.run("INSERT INTO outbox(channel_id,created,level,title,body,next_try) VALUES(?,?,?,?,?,?)",
                    (channel_id, now, msg.level, msg.title, msg.body, next_try if next_try is not None else now))

    async def send_due(self, now: float | None = None) -> int:
        """Deliver due messages, each channel on its own, so that a dead one doesn't hold up the others."""
        now = now or time.time()
        due = self.db.all("SELECT * FROM outbox WHERE next_try<=? ORDER BY channel_id, created, id", (now,))
        by_channel: dict[int, list] = {}
        for row in due:
            by_channel.setdefault(row["channel_id"], []).append(row)
        counts = await asyncio.gather(*(self._send_channel(ch_id, rows, now) for ch_id, rows in by_channel.items()))
        return sum(counts)

    async def _send_channel(self, ch_id: int, rows: list, now: float) -> int:
        """Send a channel's queue in order. What was delivered is gone from the queue at once, so a failure
        halfway retries only the rest, and nothing arrives twice."""
        ch = self.db.one("SELECT * FROM channels WHERE id=?", (ch_id,))
        if ch is None or not ch["enabled"]:
            self._forget(r["id"] for r in rows)
            return 0
        limit = getattr(KINDS.get(ch["kind"]), "MAX_TEXT", None)
        zone = tz(self.db)
        sent = 0
        for batch in batches(self._split_long(rows, limit), zone, now, limit):
            ids = [r["id"] for r in batch]
            try:
                await asyncio.wait_for(self.deliver(ch, join(batch, zone, now)), SEND_LIMIT)
            except Exception as e:  # noqa: BLE001 - any failure means "try again later"
                attempts = max(r["attempts"] for r in batch) + 1
                delay = e.after if isinstance(e, RateLimited) else min(15 * 2 ** (attempts - 1), RETRY_MAX)
                reason = str(e)[:300] or f"no answer within {SEND_LIMIT} s"
                rest = self.db.all("SELECT id FROM outbox WHERE channel_id=? AND next_try<=?", (ch_id, now))
                self.db.run(f"UPDATE outbox SET attempts=?, next_try=?, last_error=? WHERE id IN "
                            f"({','.join('?' * len(rest))})", (attempts, now + delay, reason, *[r["id"] for r in rest]))
                log.warning("channel %s: send failed (%s), retry in %ss", ch["name"], reason, delay)
                return sent
            self._forget(ids)
            self.db.set(f"channel.{ch_id}.last_ok", time.time())
            sent += 1
        return sent

    def _forget(self, ids) -> None:
        for i in ids:
            self.db.run("DELETE FROM outbox WHERE id=?", (i,))

    def _split_long(self, rows: list, limit: int | None) -> list:
        """Replace any message too long for the channel by parts in the queue, so that each is tracked by itself."""
        if not limit:
            return rows
        room = limit - JOIN_ROOM
        out = []
        for row in rows:
            if len(row["body"]) <= room:
                out.append(row)
                continue
            parts = split_text(row["body"], room)
            with self.db.tx() as t:
                t.run("DELETE FROM outbox WHERE id=?", (row["id"],))
                for n, part in enumerate(parts, 1):
                    i = t.run("INSERT INTO outbox(channel_id,created,level,title,body,attempts,next_try,last_error) "
                              "VALUES(?,?,?,?,?,?,?,?)",
                              (row["channel_id"], row["created"], row["level"], f"{row['title']} ({n}/{len(parts)})", part,
                               row["attempts"], row["next_try"], row["last_error"]))
                    out.append(t.one("SELECT * FROM outbox WHERE id=?", (i,)))
        return out

    async def deliver(self, ch, msg: Message, config: dict | None = None, secret: str | None = None) -> None:
        """config and secret, when given, are used instead of what's saved: for testing a form before saving it."""
        mod = KINDS.get(ch["kind"])
        if mod is None:
            raise RuntimeError(f"channel type {ch['kind']} is not available yet")
        config = full_config(self.secrets, ch) if config is None else config
        secret = secret or self.secrets.get(channel_secret(ch["id"])) or ""
        await mod.send(self.http, config, secret, msg.level, msg.title, msg.body)


def split_text(text: str, room: int) -> list[str]:
    """Cut at incident boundaries (a blank line) where possible, then at line ends, then anywhere."""
    parts = []
    while len(text) > room:
        cut = text.rfind("\n\n", 0, room)
        if cut <= 0:
            cut = text.rfind("\n", 0, room)
        if cut <= 0:
            cut = room
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip("\n")
    return parts + [text] if text else parts


def batches(rows: list, zone: ZoneInfo, now: float, limit: int | None) -> list[list]:
    """Group a queue into messages that each fit the channel, keeping the order."""
    out: list[list] = []
    for row in rows:
        if out and _fits(out[-1] + [row], zone, now, limit):
            out[-1].append(row)
        else:
            out.append([row])
    return out


def _fits(rows: list, zone: ZoneInfo, now: float, limit: int | None) -> bool:
    if limit is None:
        return True
    msg = join(rows, zone, now)
    return len(msg.title) + 2 + len(msg.body) <= limit


def join(rows, zone: ZoneInfo, now: float) -> Message:
    if len(rows) == 1:
        r = rows[0]
        late = now - r["created"] > 300
        suffix = f"\n\n(queued at {_hm(r['created'], zone)}, delivered late)" if late else ""
        return Message(r["level"], r["title"], r["body"] + suffix)
    level = max(r["level"] for r in rows)
    parts = [f"— {_hm(r['created'], zone)} · {r['title']}\n{r['body']}" for r in rows]
    title = f"{ICON[level]} {len(rows)} updates (queued while offline)"
    return Message(level, title, "\n\n".join(parts))
