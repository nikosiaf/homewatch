"""Telegraf config, secrets, Ookla parsing, auth, the weekly report schedule."""

import base64
import json

import pytest

from homewatch.auth import Auth, AuthError
from homewatch.modules.internet import DEFAULTS, valid_host
from homewatch.modules.internet.speedtest import parse_ookla
from homewatch.modules.internet.telegraf import render
from homewatch.report import due
from homewatch.secrets import Secrets
from tests.conftest import KEY


def test_telegraf_render_and_injection_guard():
    s = json.loads(json.dumps(DEFAULTS))
    s["gateway"]["target"] = "192.168.1.1"
    s["dns"]["server"] = "192.168.1.1"
    text = render(s, "home")
    assert 'urls = ["1.1.1.1", "8.8.8.8"]' in text and 'role = "gateway"' in text and "dns_query" in text
    s["public"]["targets"] = ['1.1.1.1"]\n[[outputs.file]]']
    assert "outputs.file" not in render(s, "home")
    assert not valid_host('x"; rm -rf /')


def test_telegraf_without_probes_still_valid():
    s = json.loads(json.dumps(DEFAULTS))
    s["public"]["enabled"] = False
    text = render(s, "home")
    assert "inputs.ping" not in text and "inputs.internal" in text


def test_secrets_roundtrip_and_binding(db):
    s = Secrets(db, base64.b64decode(KEY))
    s.put("a", "hunter2")
    assert s.get("a") == "hunter2" and s.has("a")
    raw = db.one("SELECT value FROM secrets WHERE name='a'")["value"]
    assert b"hunter2" not in bytes(raw)
    db.run("INSERT INTO secrets(name,value) VALUES('b',?)", (raw,))
    with pytest.raises(Exception):
        s.get("b")  # ciphertext is bound to its name


def test_parse_ookla():
    r = parse_ookla(json.dumps({"download": {"bandwidth": 12_500_000}, "upload": {"bandwidth": 1_250_000},
                                "ping": {"latency": 9.5}}))
    assert (r.down, r.up, r.latency) == (100.0, 10.0, 9.5)


def test_auth_lockout(db):
    a = Auth(db)
    a.create_user("admin", "correct horse battery", "admin")
    for _ in range(5):
        with pytest.raises(AuthError):
            a.login("admin", "wrong", "10.0.0.1")
    with pytest.raises(AuthError, match="Too many"):
        a.login("admin", "correct horse battery", "10.0.0.1")
    assert a.login("admin", "correct horse battery", "10.0.0.2")


def test_short_password_rejected(db):
    with pytest.raises(AuthError):
        Auth(db).create_user("bob", "short", "viewer")


def test_report_due_once_a_week(db):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    db.set("home.tz", "Europe/Athens")
    z = ZoneInfo("Europe/Athens")
    mon_7 = datetime(2026, 11, 2, 7, 59, tzinfo=z).timestamp()
    mon_8 = datetime(2026, 11, 2, 8, 1, tzinfo=z).timestamp()
    db.set("report.last_sent", datetime(2026, 10, 26, 8, 1, tzinfo=z).timestamp())
    assert not due(db, mon_7)
    assert due(db, mon_8)
    db.set("report.last_sent", mon_8)
    assert not due(db, mon_8 + 3600)


def test_failed_speed_test_retries_sooner(cfg):
    from homewatch.runtime import Runtime

    rt = Runtime.create(cfg)
    t = 1_800_000_000.0
    assert rt.speed_due(3, t)
    rt.db.run("INSERT INTO speedtests(site,ts,engine,ok,error) VALUES('home',?,'cloudflare',0,'WriteTimeout')", (t,))
    assert not rt.speed_due(3, t + 600)
    assert rt.speed_due(3, t + 16 * 60)
    rt.db.run("INSERT INTO speedtests(site,ts,engine,down,up,ok) VALUES('home',?,'cloudflare',98,9,1)", (t + 1000,))
    assert not rt.speed_due(3, t + 1000 + 3600)
    assert rt.speed_due(3, t + 1000 + 3 * 3600)
