"""Rendering, routing by level, quiet hours, the offline queue and the two channel types."""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import respx

from homewatch.engine import Event
from homewatch.modules import ERROR, OK, WARNING
from homewatch.notify import Notifier, channel_secret, in_quiet, render
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


def test_ntfy_server_and_topic_forms():
    from homewatch.notify.ntfy import normalize

    assert normalize({"server": "ntfy.sh", "topic": "home-alerts"})[0] == {"server": "https://ntfy.sh",
                                                                        "topic": "home-alerts"}
    assert normalize({"server": "", "topic": "x1"})[0]["server"] == "https://ntfy.sh"
    assert normalize({"server": "", "topic": "https://ntfy.example.org/abc"})[0] == {
        "server": "https://ntfy.example.org", "topic": "abc"}
    assert normalize({"server": "http://192.168.2.5:8080/", "topic": "t"})[0]["server"] == "http://192.168.2.5:8080"
    assert normalize({"server": "ntfy.sh", "topic": ""})[1]
    assert normalize({"server": "ftp://x", "topic": "t"})[1]
