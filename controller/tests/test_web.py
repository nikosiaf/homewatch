"""Web flows: first-run setup, login, roles, CSRF."""

import json
import re

import pytest
from fastapi.testclient import TestClient

from homewatch.runtime import Runtime
from homewatch.web import create_app


@pytest.fixture
def client(cfg):
    rt = Runtime.create(cfg)
    app = create_app(rt, start_loops=False)
    with TestClient(app, base_url="https://testserver") as c:
        c.rt = rt
        yield c


def csrf(html: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def setup_admin(c):
    r = c.get("/")
    assert r.url.path == "/setup"
    r = c.post("/setup", data={"csrf": csrf(r.text), "username": "admin", "password": "a-long-password",
                               "password2": "a-long-password"})
    assert r.url.path == "/setup/home"
    return csrf(r.text)


def test_first_run_wizard(client):
    tok = setup_admin(client)
    r = client.post("/setup/home", data={"csrf": tok, "name": "Test Home", "tz": "Europe/Athens"})
    assert r.url.path == "/setup/internet"
    assert 'value="192.168.1.1"' in r.text  # gateway hint offered
    r = client.post("/setup/internet", data={"csrf": tok, "public.enabled": "on", "public.targets": "1.1.1.1 9.9.9.9",
                                             "gateway.enabled": "on", "gateway.target": "192.168.1.1",
                                             "dns.enabled": "on", "dns.server": "192.168.1.1",
                                             "dns.domain": "example.com", "speed.enabled": "on",
                                             "speed.engine": "cloudflare", "speed.interval_h": "3",
                                             "line.contract_down": "100", "line.contract_up": "10"})
    assert r.url.path == "/setup/notify"
    line = client.rt.internet_settings()["line"]
    assert line == {"contract_down": 100, "contract_up": 10}
    conf = open(client.rt.cfg.telegraf_dir + "/telegraf.conf").read()
    assert '"9.9.9.9"' in conf and '"example.com"' in conf
    r = client.post("/setup/notify", data={"csrf": tok, "skip": "1"})
    assert r.url.path == "/setup/healthchecks"
    r = client.post("/setup/healthchecks", data={"csrf": tok, "healthchecks.url": "https://hc-ping.com/abc"})
    assert r.url.path == "/" and "Setup done" in r.text
    hc = client.rt.db.one("SELECT * FROM channels WHERE kind='healthchecks'")
    assert json.loads(hc["config"]) == {"url": "https://hc-ping.com/abc"}
    assert client.rt.db.get("home.name") == "Test Home"


def test_csrf_required(client):
    tok = setup_admin(client)
    r = client.post("/checks", data={"csrf": "wrong"})
    assert "Form expired" in r.text
    r = client.post("/checks/snooze", data={"check": "internet.loss", "for": "1h"})
    assert "Form expired" in r.text
    assert tok


def test_viewer_is_read_only(client, cfg):
    tok = setup_admin(client)
    client.post("/users", data={"csrf": tok, "username": "family", "password": "another-long-one", "role": "viewer"})
    client.post("/logout", data={"csrf": tok})
    r = client.get("/login")
    r = client.post("/login", data={"csrf": csrf(r.text), "username": "family", "password": "another-long-one"})
    assert r.url.path == "/"
    assert client.get("/history").status_code == 200
    for path in ("/checks", "/notifications", "/system", "/modules/internet", "/maintenance", "/modules"):
        assert client.get(path).status_code == 403


def test_login_required(client):
    setup_admin(client)
    client.cookies.clear()
    r = client.get("/checks")
    assert r.url.path == "/login"


def test_thresholds_saved_and_audited(client):
    tok = setup_admin(client)
    data = {"csrf": tok, "internet.present": "1", "timing.interval": "120", "timing.confirm": "2",
            "timing.remind_h": "12", "timing.fail_alert": "5"}
    for cid in ("fresh", "gateway", "reachable", "loss", "latency", "jitter", "dns", "speed"):
        data[f"internet.{cid}.enabled"] = "on"
    data.update({"internet.loss.warn": "5", "internet.loss.error": "20", "internet.latency.warn": "80",
                 "internet.jitter.warn": "15", "internet.jitter.error": "30", "internet.dns.warn": "500",
                 "internet.speed.warn": "70", "internet.fresh.error": "180"})
    r = client.post("/checks", data=data)
    assert "Saved" in r.text
    row = client.rt.db.one("SELECT warn FROM checks WHERE check_key='internet.loss'")
    assert row["warn"] == 5
    assert client.rt.db.one("SELECT * FROM audit WHERE action='checks'")


def test_speedport_module_settings(client):
    tok = setup_admin(client)
    r = client.post("/modules/speedport", data={"csrf": tok, "enabled": "on", "host": "192.168.1.1",
                                                "username": "admin", "interval_min": "15"})
    assert "Enter the modem password" in r.text
    r = client.post("/modules/speedport", data={"csrf": tok, "enabled": "on", "host": "192.168.1.1",
                                                "username": "admin", "password": "s3cret", "interval_min": "15"})
    assert "Saved" in r.text
    assert client.rt.secrets.get("module.speedport.password") == "s3cret"
    assert "s3cret" not in client.get("/modules/speedport").text
    r = client.get("/checks")
    assert "ISP modem" in r.text and "speedport.snr.warn" in r.text
    assert "DSL sync" in client.get("/").text


def test_pages_render(client):
    setup_admin(client)
    for path in ("/", "/history", "/modules", "/modules/internet", "/modules/speedport", "/internet", "/checks", "/maintenance",
                 "/notifications", "/system", "/password", "/partials/status"):
        assert client.get(path).status_code == 200, path


def test_menu_lists_modules_with_coming_soon(client):
    setup_admin(client)
    r = client.get("/")
    assert 'href="/modules/internet"' in r.text and "Proxmox VE<small>coming soon</small>" in r.text


def test_check_now_reports_read_failure(client):
    tok = setup_admin(client)
    r = client.post("/checks/run", data={"csrf": tok})
    assert "Check run failed" in r.text  # no VictoriaMetrics in the test environment


def test_delete_history_needs_the_word(client):
    tok = setup_admin(client)
    db = client.rt.db
    db.run("INSERT INTO issues(site,check_key,level,worst,started) VALUES('home','internet.loss',1,1,1)")
    r = client.post("/system/reset", data={"csrf": tok, "mode": "history", "confirm": "nope"})
    assert "Type DELETE" in r.text and db.one("SELECT COUNT(*) n FROM issues")["n"] == 1
    client.post("/system/reset", data={"csrf": tok, "mode": "history", "confirm": "DELETE"})
    assert db.one("SELECT COUNT(*) n FROM issues")["n"] == 0
    assert db.one("SELECT COUNT(*) n FROM users")["n"] == 1


def test_reset_everything_restarts_the_wizard(client):
    tok = setup_admin(client)
    client.post("/setup/home", data={"csrf": tok, "name": "Old Home", "tz": "Europe/Athens"})
    r = client.post("/system/reset", data={"csrf": tok, "mode": "all", "confirm": "RESET"})
    assert r.url.path == "/setup" and "first-run setup" in r.text
    db = client.rt.db
    assert db.one("SELECT COUNT(*) n FROM users")["n"] == 0
    assert db.get("home.name") is None


def test_add_providers_with_prefixed_fields(client):
    tok = setup_admin(client)
    r = client.post("/notifications/add", data={"csrf": tok, "kind": "ntfy", "ntfy.url": "ntfy.sh/home-alerts-x1",
                                                "telegram.chat_id": "ignored"})
    assert "Added" in r.text
    ch = client.rt.db.one("SELECT * FROM channels WHERE kind='ntfy'")
    assert json.loads(ch["config"])["url"] == "https://ntfy.sh/home-alerts-x1"
    r = client.post("/notifications/add", data={"csrf": tok, "kind": "telegram", "telegram.chat_id": "42"})
    assert "bot token" in r.text
    r = client.post("/notifications/add", data={"csrf": tok, "kind": "healthchecks",
                                                "healthchecks.url": "https://hc-ping.com/u1"})
    assert "Added" in r.text and "https://hc-ping.com/u1" in r.text  # the ping URL is shown, not hidden
    assert len(client.rt.notifier.channels()) == 1  # the heartbeat gets no messages


def test_weekly_report_card(client):
    tok = setup_admin(client)
    client.post("/notifications/add", data={"csrf": tok, "kind": "ntfy", "ntfy.url": "https://ntfy.sh/t1"})
    cid = client.rt.db.one("SELECT id FROM channels")["id"]
    r = client.post("/notifications/report", data={"csrf": tok, "report_weekday": "6", "report_time": "19:30",
                                                   f"report.{cid}": "on"})
    assert "Weekly report saved" in r.text
    assert client.rt.db.get("report.schedule") == {"weekday": 6, "time": "19:30"}
    assert client.rt.db.one("SELECT weekly_report FROM channels")["weekly_report"] == 1


def test_old_heartbeat_secret_becomes_a_provider(client):
    client.rt.secrets.put("healthchecks.url", "https://hc-ping.com/old")
    client.rt.migrate_heartbeat()
    ch = client.rt.db.one("SELECT * FROM channels WHERE kind='healthchecks'")
    assert json.loads(ch["config"])["url"] == "https://hc-ping.com/old"
    assert client.rt.secrets.get("healthchecks.url") is None


def test_homewatch_address_filled_from_request_and_editable(client):
    tok = setup_admin(client)
    assert client.rt.db.get("home.url") == "https://testserver"
    client.post("/system/home", data={"csrf": tok, "name": "H", "tz": "UTC", "url": "https://hw.tail1234.ts.net/"})
    assert client.rt.db.get("home.url") == "https://hw.tail1234.ts.net"


def test_speedtest_button_returns_running_row_at_once(client, monkeypatch):
    tok = setup_admin(client)

    async def slow(already_marked=False):  # never finishes during the test
        import asyncio
        await asyncio.sleep(3600)

    monkeypatch.setattr(client.rt, "run_speedtest", slow)
    r = client.post("/modules/internet/speedtest", data={"csrf": tok}, headers={"HX-Request": "true"})
    assert "Running…" in r.text and 'hx-trigger="every 2s"' in r.text
    client.rt.speed_running = False
    for t in client.rt.tasks:
        t.cancel()


def test_modem_read_returns_partial(client):
    tok = setup_admin(client)
    r = client.post("/modules/speedport/read", data={"csrf": tok}, headers={"HX-Request": "true"})
    assert 'id="reading"' in r.text and "No password set" in r.text
    assert "<html" not in r.text
