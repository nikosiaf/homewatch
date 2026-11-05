"""Rendering, routing by level, quiet hours, the offline queue and the two channel types."""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import respx

from homewatch.engine import Event
from homewatch.modules import ERROR, OK, WARNING
from homewatch.notify import Message, Notifier, channel_secret, in_quiet, render
from homewatch.secrets import Secrets
from tests.conftest import KEY
import base64

ZONE = ZoneInfo("Europe/Athens")
T0 = datetime(2026, 11, 2, 14, 2, tzinfo=ZONE).timestamp()


def secrets(db):
    return Secrets(db, base64.b64decode(KEY))


def add_channel(db, kind="telegram", levels=(1, 2), config=None, quiet=(None, None)):
    return db.run("INSERT INTO channels(kind,name,levels,quiet_start,quiet_end,config) VALUES(?,?,?,?,?,?)",
                  (kind, kind, json.dumps(list(levels)), quiet[0], quiet[1],
                   json.dumps(config or {"chat_id": "42", "server": "https://ntfy.example", "topic": "t"})))


def test_render_outage_summary():
    ev = Event("resolve", "internet.reachable", "Internet reachable", OK, ERROR, "2 of 2 targets answer",
               T0, T0 + 17 * 60)
    msg = render([ev], "Home", (0, 0), "https://hw", ZONE)
    assert msg.title.endswith("Home: back to OK")
    assert "after 17 min (14:02–14:19)" in msg.body
    assert "All checks OK." in msg.body and msg.body.endswith("https://hw")


def test_dispatch_routes_by_level_and_quiet_hours(db):
    n = Notifier(db, secrets(db))
    db.set("home.tz", "Europe/Athens")
    errors_only = add_channel(db, levels=(2,))
    quiet = add_channel(db, kind="ntfy", quiet=("13:00", "15:00"))
    warn = Event("open", "internet.latency", "Latency", WARNING, WARNING, "150 ms", T0)
    assert n.dispatch([warn], (0, 1), T0) == 1  # only the quiet channel wants warnings
    row = db.one("SELECT * FROM outbox")
    assert row["channel_id"] == quiet
    assert row["next_try"] == datetime(2026, 11, 2, 15, 0, tzinfo=ZONE).timestamp()  # held until quiet ends
    err = Event("open", "internet.reachable", "Internet reachable", ERROR, ERROR, "down", T0)
    n.dispatch([err], (1, 0), T0)
    rows = db.all("SELECT channel_id, next_try FROM outbox WHERE level=2")
    assert {r["channel_id"] for r in rows} == {errors_only, quiet}
    assert all(r["next_try"] == T0 for r in rows)  # ERROR ignores quiet hours


def test_in_quiet_across_midnight():
    ch = {"quiet_start": "23:00", "quiet_end": "07:00"}
    night = datetime(2026, 11, 2, 2, 0, tzinfo=ZONE).timestamp()
    assert in_quiet(ch, night, ZONE) == datetime(2026, 11, 2, 7, 0, tzinfo=ZONE).timestamp()
    assert in_quiet(ch, T0, ZONE) is None


@respx.mock
async def test_queue_while_offline_then_one_joined_message(db):
    s = secrets(db)
    n = Notifier(db, s, httpx.AsyncClient())
    cid = add_channel(db)
    s.put(channel_secret(cid), "TOKEN")
    route = respx.post("https://api.telegram.org/botTOKEN/sendMessage")
    route.side_effect = httpx.ConnectError("offline")
    n.dispatch([Event("open", "internet.reachable", "Internet reachable", ERROR, ERROR, "down", T0)], (1, 0), T0)
    assert await n.send_due(T0) == 0
    row = db.one("SELECT * FROM outbox")
    assert row["attempts"] == 1 and "unreachable" in row["last_error"] and "TOKEN" not in row["last_error"]
    n.dispatch([Event("resolve", "internet.reachable", "Internet reachable", OK, ERROR, "back", T0, T0 + 600)],
               (0, 0), T0 + 600)
    route.side_effect = None
    route.return_value = httpx.Response(200, json={"ok": True})
    assert await n.send_due(T0 + 700) == 1
    sent = json.loads(route.calls.last.request.content)
    assert "2 updates (queued while offline)" in sent["text"]
    assert db.all("SELECT * FROM outbox") == []


@respx.mock
async def test_ntfy_headers(db):
    s = secrets(db)
    n = Notifier(db, s, httpx.AsyncClient())
    cid = add_channel(db, kind="ntfy")
    s.put(channel_secret(cid), "tk_abc")
    route = respx.post("https://ntfy.example/t").mock(return_value=httpx.Response(200))
    n.dispatch([Event("open", "internet.dns", "DNS", ERROR, ERROR, "failing", T0)], (1, 0), T0)
    assert await n.send_due(T0) == 1
    req = route.calls.last.request
    assert req.headers["Priority"] == "urgent" and req.headers["Authorization"] == "Bearer tk_abc"
    assert "ERROR" in req.headers["Title"]


def test_ntfy_topic_url_forms():
    from homewatch.notify.ntfy import normalize

    assert normalize({"url": "ntfy.sh/home-alerts"})[0] == {"url": "https://ntfy.sh/home-alerts",
                                                           "server": "https://ntfy.sh", "topic": "home-alerts"}
    assert normalize({"url": "http://192.168.2.5:8080/t/"})[0]["server"] == "http://192.168.2.5:8080"
    assert normalize({"server": "https://ntfy.example", "topic": "old"})[0]["url"] == "https://ntfy.example/old"
    assert normalize({"url": "https://ntfy.sh"})[1]  # no topic
    assert normalize({"url": "ftp://x/t"})[1]


def test_email_settings_checked():
    from homewatch.notify import mail
    cfg, err = mail.normalize({"server": "smtp.example.com", "username": "", "sender": "hw@example.com",
                               "to": "a@example.com; b@example.com"})
    assert not err and cfg["server"] == "smtp.example.com:587" and cfg["to"] == "a@example.com, b@example.com"
    assert "From" in mail.normalize(cfg | {"sender": "nope"})[1]
    assert "To" in mail.normalize(cfg | {"to": ""})[1]
    assert "host:port" in mail.normalize(cfg | {"server": "smtp.example.com:99999"})[1]
    msg = mail.build(cfg, "🔴 Home: ERROR", "body")
    assert msg["Subject"] == "🔴 Home: ERROR" and msg["To"] == "a@example.com, b@example.com"


class FakeSMTP:
    sent: list = []

    def __init__(self, host, port, timeout=None, context=None, starttls=True):
        self.host, self.port, self.tls, self.login_as = host, port, port == 465, None
        self._starttls = starttls

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def ehlo(self):
        pass

    def has_extn(self, name):
        return self._starttls

    def starttls(self, context=None):
        self.tls = True

    def login(self, user, pw):
        assert self.tls, "password over plain text"
        self.login_as = (user, pw)

    def send_message(self, msg):
        FakeSMTP.sent.append((self, msg))


async def test_email_send_uses_tls_and_refuses_plain_password(monkeypatch):
    import smtplib

    import pytest

    from homewatch.notify import mail
    cfg = {"server": "smtp.example.com:587", "username": "me", "sender": "hw@example.com", "to": "a@example.com"}
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    FakeSMTP.sent = []
    await mail.send(None, cfg, "pw", ERROR, "t", "b")
    conn, msg = FakeSMTP.sent[0]
    assert conn.tls and conn.login_as == ("me", "pw") and msg["Subject"] == "t"
    monkeypatch.setattr(smtplib, "SMTP", lambda *a, **k: FakeSMTP(*a, starttls=False, **k))
    with pytest.raises(RuntimeError, match="unencrypted"):
        await mail.send(None, cfg, "pw", ERROR, "t", "b")
    await mail.send(None, cfg | {"username": ""}, "", OK, "t", "b")  # a local relay without login may be plain


@respx.mock
async def test_webhook_payload_and_errors(db):
    import pytest

    from homewatch.notify import webhook
    route = respx.post("https://hooks.example/abc").mock(return_value=httpx.Response(204))
    async with httpx.AsyncClient() as http:
        await webhook.send(http, {}, "https://hooks.example/abc", WARNING, "🟡 Home: WARNING", "line")
        body = json.loads(route.calls.last.request.content)
        assert body["text"] == body["content"] == "🟡 Home: WARNING\n\nline" and body["level_name"] == "WARNING"
        respx.post("https://hooks.example/bad").mock(return_value=httpx.Response(404))
        with pytest.raises(RuntimeError, match="HTTP 404") as e:
            await webhook.send(http, {}, "https://hooks.example/bad", ERROR, "t", "b")
        assert "hooks.example" not in str(e.value)  # the URL may hold a token
    assert webhook.check_secret("hooks.example/x") and not webhook.check_secret("https://hooks.example/x")


def _msg(n, body="x"):
    return Message(2, f"m{n}", body)


@respx.mock
async def test_long_message_arrives_in_parts_nothing_lost(db):
    s = secrets(db)
    n = Notifier(db, s, httpx.AsyncClient())
    cid = add_channel(db, kind="telegram")
    s.put(channel_secret(cid), "TOKEN")
    body = "\n\n".join(f"incident {i} " + "y" * 150 for i in range(60))
    n.queue(cid, Message(2, "Outage", body), 1000.0)
    route = respx.post(url__regex=r"https://api.telegram.org/.*").mock(return_value=httpx.Response(200, json={"ok": True}))
    assert await n.send_due(1000.0) > 1
    texts = [json.loads(c.request.content)["text"] for c in route.calls]
    assert all(len(t) <= 4000 for t in texts) and "shortened" not in "".join(texts)
    for i in range(60):
        assert sum(f"incident {i} " in t for t in texts) == 1  # whole, at a boundary, exactly once
    assert "(1/" in texts[0] and not db.all("SELECT 1 FROM outbox")


@respx.mock
async def test_failure_halfway_retries_only_the_rest(db):
    s = secrets(db)
    n = Notifier(db, s, httpx.AsyncClient())
    cid = add_channel(db, kind="telegram")
    s.put(channel_secret(cid), "TOKEN")
    for i in range(3):
        n.queue(cid, _msg(i, f"body{i} " + "z" * 3000), 1000.0 + i)
    ok = httpx.Response(200, json={"ok": True})
    route = respx.post(url__regex=r"https://api.telegram.org/.*").mock(side_effect=[ok, httpx.Response(500, json={})])
    assert await n.send_due(2000.0) == 1
    assert len(db.all("SELECT 1 FROM outbox")) == 2
    route.side_effect = [ok, ok]
    assert await n.send_due(2100.0) == 2
    assert "body0" not in "".join(json.loads(c.request.content)["text"] for c in route.calls[2:])


@respx.mock
async def test_rate_limit_instruction_is_respected(db):
    s = secrets(db)
    n = Notifier(db, s, httpx.AsyncClient())
    cid = add_channel(db, kind="telegram")
    s.put(channel_secret(cid), "TOKEN")
    n.queue(cid, _msg(1), 1000.0)
    respx.post(url__regex=r"https://api.telegram.org/.*").mock(
        return_value=httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 500}}))
    assert await n.send_due(1000.0) == 0
    row = db.one("SELECT next_try, last_error FROM outbox")
    assert row["next_try"] == 1500.0 and "slow down" in row["last_error"]
    assert await n.send_due(1100.0) == 0 and db.one("SELECT attempts FROM outbox")["attempts"] == 1  # not tried early


async def test_dead_channel_doesnt_hold_up_the_others(db, monkeypatch):
    import asyncio

    import homewatch.notify as nt

    monkeypatch.setattr(nt, "SEND_LIMIT", 0.05)
    s = secrets(db)
    n = Notifier(db, s, httpx.AsyncClient())
    dead, live = add_channel(db, kind="telegram"), add_channel(db, kind="ntfy")
    n.queue(dead, _msg(1), 1000.0)
    n.queue(live, _msg(2), 1000.0)
    order = []

    async def deliver(ch, msg):
        if ch["id"] == dead:
            await asyncio.sleep(5)
        order.append(ch["id"])

    monkeypatch.setattr(n, "deliver", deliver)
    assert await n.send_due(1000.0) == 1
    assert order == [live]
    row = db.one("SELECT channel_id, last_error FROM outbox")
    assert row["channel_id"] == dead and "no answer" in row["last_error"]


def test_split_text_prefers_boundaries():
    from homewatch.notify import split_text

    assert split_text("aaa\n\nbbb\nccc", 8) == ["aaa", "bbb\nccc"]
    assert split_text("x" * 25, 10) == ["x" * 10, "x" * 10, "x" * 5]


def test_a_long_check_text_is_shortened_in_messages_only():
    long = "; ".join(f"sensor {i} unavailable" for i in range(60))
    ev = Event("open", "homeassistant.rules", "Entity rules", WARNING, WARNING, long, T0)
    msg = render([ev], "Home", (0, 1), "", ZONE)
    line = [x for x in msg.body.splitlines() if "sensor 0" in x][0].strip()
    assert len(line) <= 300 and line.endswith("…") and "sensor 59" not in msg.body
