"""Web flows: first-run setup, login, roles, CSRF."""

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
                                             "speed.contract_down": "100", "speed.contract_up": "10"})
    assert r.url.path == "/setup/notify"
    conf = open(client.rt.cfg.telegraf_dir + "/telegraf.conf").read()
    assert '"9.9.9.9"' in conf and '"example.com"' in conf
    r = client.post("/setup/notify", data={"csrf": tok, "skip": "1"})
    assert r.url.path == "/setup/healthchecks"
    r = client.post("/setup/healthchecks", data={"csrf": tok, "hc_url": "https://hc-ping.com/abc"})
    assert r.url.path == "/" and "Setup done" in r.text
    assert client.rt.secrets.get("healthchecks.url") == "https://hc-ping.com/abc"
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
    for path in ("/checks", "/notifications", "/system", "/internet", "/maintenance"):
        assert client.get(path).status_code == 403


def test_login_required(client):
    setup_admin(client)
    client.cookies.clear()
    r = client.get("/checks")
    assert r.url.path == "/login"


def test_thresholds_saved_and_audited(client):
    tok = setup_admin(client)
    data = {"csrf": tok, "timing.interval": "120", "timing.confirm": "2", "timing.remind_h": "12",
            "timing.fail_alert": "5"}
    for cid in ("fresh", "gateway", "reachable", "loss", "latency", "jitter", "dns", "speed"):
        data[f"{cid}.enabled"] = "on"
    data.update({"loss.warn": "5", "loss.error": "20", "latency.warn": "80", "jitter.warn": "15",
                 "jitter.error": "30", "dns.warn": "500", "speed.warn": "70", "fresh.error": "180"})
    r = client.post("/checks", data=data)
    assert "Saved" in r.text
    row = client.rt.db.one("SELECT warn FROM checks WHERE check_key='internet.loss'")
    assert row["warn"] == 5
    assert client.rt.db.one("SELECT * FROM audit WHERE action='checks'")


def test_pages_render(client):
    setup_admin(client)
    for path in ("/", "/history", "/internet", "/checks", "/maintenance", "/notifications", "/system", "/password",
                 "/partials/status"):
        assert client.get(path).status_code == 200, path
