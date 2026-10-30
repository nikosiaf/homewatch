"""Web flows: first-run setup, login, roles, CSRF."""

import json
import os
import re
import time

import pytest
from fastapi.testclient import TestClient

from homewatch.modules.registry import MODULES
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
    r = client.post("/modules/speedport", data={"csrf": tok, "enabled": "on", "model": "speedport-plus-2",
                                                "host": "192.168.1.1", "username": "admin", "interval_min": "15"})
    assert "Enter the modem password" in r.text
    r = client.post("/modules/speedport", data={"csrf": tok, "enabled": "on", "model": "speedport-plus-2",
                                                "host": "192.168.1.1", "username": "admin", "password": "s3cret",
                                                "interval_min": "15"})
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


def test_modules_link_and_page(client):
    setup_admin(client)
    r = client.get("/")
    assert '<a href="/modules"' in r.text and "<details" not in r.text
    r = client.get("/modules")
    assert 'href="/modules/internet"' in r.text and "ISP modem" in r.text and "UPS (NUT)" in r.text
    # modules not set up are grey and say Off; none of them claims to be OK
    assert 'class="tile off" href="/modules/ups"' in r.text and 'class="tile off" href="/modules/pbs"' in r.text
    assert "checks OK" not in r.text


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


def test_message_lands_in_its_box(client):
    tok = setup_admin(client)
    r = client.post("/system/home", data={"csrf": tok, "_box": "home", "name": "H", "tz": "UTC"},
                    follow_redirects=False)
    assert r.headers["location"].endswith("&box=home#home") or "box=home" in r.headers["location"]
    page = client.get(r.headers["location"].split("#")[0]).text
    home_card = page[page.index('id="home"'):page.index('id="users"')]
    assert "Saved" in home_card


def test_weekly_report_ignores_disabled_channel(client):
    tok = setup_admin(client)
    client.post("/notifications/add", data={"csrf": tok, "kind": "ntfy", "ntfy.url": "https://ntfy.sh/t1"})
    cid = client.rt.db.one("SELECT id FROM channels")["id"]
    client.post("/notifications/report", data={"csrf": tok, "report_weekday": "0", "report_time": "08:00",
                                               f"report.{cid}": "on"})
    client.post(f"/notifications/{cid}", data={"csrf": tok, "name": "n", "url": "https://ntfy.sh/t1"})  # disable
    page = client.get("/notifications").text
    assert "(disabled)" in page
    r = client.post("/notifications/report", data={"csrf": tok, "report_weekday": "0", "report_time": "08:00",
                                                   "send_now": "1"})
    assert "No enabled channel is ticked" in r.text


def test_add_modem_flow(client):
    tok = setup_admin(client)
    page = client.get("/modules/speedport").text
    assert "Add modem" in page and "Before you switch it on" in page and "Last reading" not in page
    r = client.post("/modules/speedport", data={"csrf": tok, "enabled": "on", "model": "",
                                                "host": "192.168.1.1", "username": "admin"})
    assert "Choose your modem first" in r.text
    client.post("/modules/speedport", data={"csrf": tok, "enabled": "on", "model": "speedport-plus-2",
                                            "host": "192.168.1.1", "username": "admin", "password": "pw",
                                            "interval_min": "15"})
    page = client.get("/modules/speedport").text
    assert "Add modem" not in page and "Last reading" in page and "data-enable-toggle" in page
    # switched off with greyed (unsent) fields: settings are kept, reading hidden
    client.post("/modules/speedport", data={"csrf": tok})
    s = client.rt.db.get("module.speedport")
    assert s["enabled"] is False and s["host"] == "192.168.1.1" and s["interval_min"] == 15
    assert "Last reading" not in client.get("/modules/speedport").text


def test_no_macro_repr_on_internet_page(client):
    tok = setup_admin(client)
    r = client.post("/modules/internet", data={"csrf": tok, "_box": "settings", "public.enabled": "on",
                                              "public.targets": "1.1.1.1", "speed.engine": "cloudflare",
                                              "speed.interval_h": "3"})
    assert "Saved" in r.text and "<macro" not in r.text and "&lt;macro" not in r.text


def test_ookla_needs_eula_and_install_failure_saves_nothing(client, monkeypatch):
    tok = setup_admin(client)
    base = {"csrf": tok, "public.enabled": "on", "public.targets": "1.1.1.1", "speed.enabled": "on",
            "speed.engine": "ookla", "speed.interval_h": "3"}
    r = client.post("/modules/internet", data=base)
    assert "accept Ookla" in r.text
    assert client.rt.internet_settings()["speed"]["engine"] == "cloudflare"

    async def fail(_dir, http=None):
        return "Download from Ookla failed (ConnectError)"

    monkeypatch.setattr("homewatch.modules.internet.speedtest.install_ookla", fail)
    r = client.post("/modules/internet", data={**base, "speed.ookla_accepted": "on"})
    assert "Nothing saved" in r.text
    assert client.rt.internet_settings()["speed"]["engine"] == "cloudflare"


def test_status_page_has_one_line_per_module(client):
    setup_admin(client)
    from homewatch.modules import CheckResult

    client.rt.results = [CheckResult("internet.loss", "Packet loss", 1, "packet loss 12 %"),
                         CheckResult("internet.latency", "Latency", 0, "21 ms")]
    client.rt.db.run("INSERT INTO check_state(site,check_key,status) VALUES('home','internet.loss',1)")
    page = client.get("/").text
    assert page.count('<details class="module') == 1
    assert "Packet loss: packet loss 12 %" in page and 'data-module="internet"' in page


def test_history_shows_back_to_ok_events(client):
    setup_admin(client)
    db = client.rt.db
    t = time.time() - 3600
    db.run("INSERT INTO issues(site,check_key,level,worst,started,resolved,detail,resolved_detail,notified) "
           "VALUES('home','internet.loss',1,1,?,?,'packet loss 12 %','packet loss 0.0 %',1)", (t, t + 300))
    db.run("INSERT INTO issues(site,check_key,level,worst,started,detail,notified) "
           "VALUES('home','internet.dns',2,2,?,'DNS failing',1)", (t + 600,))
    page = client.get("/history").text
    assert "Back to OK: packet loss 0.0 %" in page and "after 5 min" in page
    assert "still open" in page and "DNS failing" in page
    assert page.index("DNS failing") < page.index("Back to OK") < page.index("packet loss 12 %")  # newest first


def test_modem_model_none_removes_it(client):
    tok = setup_admin(client)
    client.post("/modules/speedport", data={"csrf": tok, "enabled": "on", "model": "speedport-plus-2",
                                            "host": "192.168.1.1", "username": "admin", "password": "pw",
                                            "interval_min": "15"})
    r = client.post("/modules/speedport", data={"csrf": tok, "enabled": "on", "model": ""})
    assert "Modem removed" in r.text and "Add modem" in r.text
    assert client.rt.db.get("module.speedport") is None
    assert client.rt.secrets.get("module.speedport.password") is None


def test_checks_page_order(client):
    setup_admin(client)
    page = client.get("/checks").text
    assert page.index("<h2>Snooze</h2>") < page.index("<h2>Timing</h2>") < page.index("<h2>Thresholds</h2>")


def test_system_health_lists_probes_and_ookla(client):
    setup_admin(client)
    from homewatch.modules import CheckResult

    client.rt.results = [CheckResult("internet.fresh", "Probe data fresh", 0, "Last probe data 20 s ago", 20)]
    page = client.get("/system").text
    assert "Telegraf (ping and DNS probes)" in page and "last data 20 s ago" in page
    assert "Ookla Speedtest CLI" in page and "not installed" in page


def test_add_proxmox_tests_before_saving(client, monkeypatch):
    tok = setup_admin(client)
    from homewatch.modules.proxmox.plugin import plugin
    from tests.test_proxmox import snap

    async def bad(*a, **k):
        return None, "Proxmox rejected the API token"

    monkeypatch.setattr(plugin, "test", bad)
    r = client.post("/modules/proxmox", data={"csrf": tok, "url": "192.168.1.10", "token_id": "homewatch@pve!monitor",
                                              "secret": "x"})
    assert "Nothing saved: Proxmox rejected the API token" in r.text
    assert client.rt.db.get("module.proxmox") is None

    async def good(*a, **k):
        return snap(), ""

    monkeypatch.setattr(plugin, "test", good)
    r = client.post("/modules/proxmox", data={"csrf": tok, "url": "192.168.1.10", "token_id": "homewatch@pve!monitor",
                                              "secret": "x"})
    s = client.rt.db.get("module.proxmox")
    assert s["url"] == "https://192.168.1.10:8006" and s["watched"] == []  # nothing pre-selected
    assert "No guests chosen yet" in r.text and client.rt.secrets.get("module.proxmox.token") == "x"
    r = client.post("/modules/proxmox", data={"csrf": tok, "enabled": "on", "url": "https://192.168.1.10:8006",
                                              "token_id": "homewatch@pve!monitor", "watch.100": "on"})
    assert client.rt.db.get("module.proxmox")["watched"] == [100]
    r = client.post("/modules/proxmox", data={"csrf": tok, "remove": "1"})
    assert "Removed" in r.text and client.rt.db.get("module.proxmox") is None
    plugin.snap = None


def test_api_module_connect_choose_switch_off_remove(client, monkeypatch):
    tok = setup_admin(client)
    from homewatch.modules.apibase import ApiError
    from homewatch.modules.opnsense.plugin import plugin
    from tests.test_opnsense import snap

    async def bad(*a, **k):
        raise ApiError("OPNsense rejected the API key")

    monkeypatch.setattr(plugin, "fetch", bad)
    page = client.get("/modules/opnsense").text
    assert "Add OPNsense" in page and "Lobby: Dashboard" in page
    r = client.post("/modules/opnsense", data={"csrf": tok, "url": "192.168.1.1", "api_key": "k", "secret": "s"})
    assert "Nothing saved: OPNsense rejected the API key" in r.text and client.rt.db.get("module.opnsense") is None

    async def good(*a, **k):
        return snap()

    monkeypatch.setattr(plugin, "fetch", good)
    r = client.post("/modules/opnsense", data={"csrf": tok, "url": "192.168.1.1", "api_key": "k", "secret": "s"})
    s = client.rt.db.get("module.opnsense")
    assert "Connected" in r.text and s["url"] == "https://192.168.1.1" and sorted(s["chosen"]) == ["haproxy", "unbound"]
    assert client.rt.secrets.get("module.opnsense.secret") == "s" and "Services that must run" in r.text
    client.post("/modules/opnsense", data={"csrf": tok, "enabled": "on", "url": "https://192.168.1.1", "api_key": "k",
                                           "choose.unbound": "on"})
    assert client.rt.db.get("module.opnsense")["chosen"] == ["unbound"]
    client.post("/modules/opnsense", data={"csrf": tok})  # switched off, fields greyed out
    s = client.rt.db.get("module.opnsense")
    assert s["enabled"] is False and s["url"] == "https://192.168.1.1" and s["chosen"] == ["unbound"]
    from homewatch.modules.registry import MODULES
    assert not MODULES["opnsense"].enabled(MODULES["opnsense"].load(client.rt))
    r = client.post("/modules/opnsense", data={"csrf": tok, "remove": "1"})
    assert "Removed" in r.text and client.rt.db.get("module.opnsense") is None
    assert client.rt.secrets.get("module.opnsense.secret") is None
    plugin.snap = None


def test_grouped_choices_watch_list_save_and_refresh(client, monkeypatch):
    tok = setup_admin(client)
    from homewatch.modules.homeassistant.plugin import plugin
    from tests.test_homeassistant import snap

    reads = []

    async def good(*a, **k):
        reads.append(1)
        return snap(unavailable=[{"entity_id": "light.old", "name": "Old light"}, {"entity_id": "sensor.x", "name": ""}],
                    all=[["light.old", "Old light"], ["light.new", ""], ["sensor.x", ""], ["sensor.y", ""]])

    monkeypatch.setattr(plugin, "fetch", good)
    client.post("/modules/homeassistant", data={"csrf": tok, "url": "http://ha:8123", "secret": "t"})
    s = client.rt.db.get("module.homeassistant")
    assert s["chosen"] == ["light.old", "sensor.x"] and s["skipped"] == []
    page = client.get("/modules/homeassistant").text
    assert "Entities to watch" in page and "data-group-all" in page and "Read now" in page and "read every 2 min" in page
    # untick all sensors and one light; something skipped earlier but not listed now keeps its setting
    client.rt.db.set("module.homeassistant", s | {"skipped": ["camera.door"]})
    shown = '["light.old", "light.new", "sensor.x", "sensor.y"]'
    client.post("/modules/homeassistant", data={"csrf": tok, "enabled": "on", "url": "http://ha:8123",
                                                "_filters_shown": shown, "keep.light.old": "on",
                                                "choose.light.old": "on"})
    s = client.rt.db.get("module.homeassistant")
    assert s["skipped"] == ["camera.door", "group:sensor", "light.new", "sensor.x", "sensor.y"]
    assert s["chosen"] == ["light.old"]
    # ticking one sensor again drops the domain-wide skip
    client.post("/modules/homeassistant", data={"csrf": tok, "enabled": "on", "url": "http://ha:8123",
                                                "_filters_shown": shown, "keep.light.old": "on", "keep.sensor.y": "on"})
    assert client.rt.db.get("module.homeassistant")["skipped"] == ["camera.door", "light.new", "sensor.x"]
    n = len(reads)
    r = client.post("/modules/homeassistant/refresh", data={"csrf": tok})
    assert "Read again" in r.text and len(reads) == n + 1
    plugin.snap = None


def test_add_email_and_webhook_channels(client):
    tok = setup_admin(client)
    page = client.get("/notifications").text
    assert "coming soon" not in page and ">Email<" in page and ">Webhook<" in page
    r = client.post("/notifications/add", data={"csrf": tok, "kind": "webhook"})
    assert "enter the webhook url" in r.text
    r = client.post("/notifications/add", data={"csrf": tok, "kind": "webhook", "webhook.secret": "not-a-url"})
    assert "full address" in r.text
    client.post("/notifications/add", data={"csrf": tok, "kind": "webhook", "webhook.secret": "https://hooks.example/t"})
    ch = client.rt.db.one("SELECT * FROM channels WHERE kind='webhook'")
    assert json.loads(ch["config"]) == {}  # the URL is a secret: not in exports or versions
    assert client.rt.secrets.get(f"channel.{ch['id']}") == "https://hooks.example/t"
    client.post("/notifications/add", data={"csrf": tok, "kind": "email", "email.server": "smtp.example.com:465",
                                            "email.sender": "hw@example.com", "email.to": "me@example.com",
                                            "email.username": "", "email.secret": ""})
    ch = client.rt.db.one("SELECT * FROM channels WHERE kind='email'")
    assert json.loads(ch["config"])["server"] == "smtp.example.com:465"


def test_every_module_page_renders(client):
    setup_admin(client)
    from homewatch.modules import CATALOG
    from homewatch.modules.registry import MODULES

    assert {m for m, _, ready in CATALOG if ready} == set(MODULES)
    for mid in MODULES:
        r = client.get(f"/modules/{mid}")
        assert r.status_code == 200 and "Traceback" not in r.text, mid


def test_maintenance_checks_grouped_by_module(client):
    tok = setup_admin(client)
    r = client.get("/maintenance")
    assert "data-choice-group" in r.text and "<b>Internet line</b>" in r.text
    assert 'name="check.internet.loss"' in r.text
    assert 'name="check.opnsense.haproxy"' not in r.text  # modules not in use are not offered
    r = client.post("/maintenance", data={"csrf": tok, "name": "Modem swap", "kind": "weekly", "weekday": "2",
                                          "start_time": "03:00", "dur_min": "30", "scope": "some",
                                          "check.internet.loss": "on"})
    assert "Modem swap" in r.text and "Packet loss" in r.text


def test_internet_module_switch(client):
    tok = setup_admin(client)
    rt = client.rt
    assert rt.internet_settings()["enabled"] is True
    # turning it off posts only the switch: the rest of the form is greyed out, and its settings stay
    r = client.post("/modules/internet", data={"csrf": tok, "_box": "settings", "_switch": "1"})
    assert "Saved" in r.text
    s = rt.internet_settings()
    assert s["enabled"] is False and s["public"]["targets"]
    assert "inputs.ping" not in open(os.path.join(rt.cfg.telegraf_dir, "telegraf.conf")).read()
    assert not MODULES["internet"].enabled(s)
    r = client.post("/modules/internet", data={"csrf": tok, "_box": "settings", "_switch": "1", "enabled": "on",
                                              "public.enabled": "on", "public.targets": "1.1.1.1",
                                              "speed.engine": "cloudflare", "speed.interval_h": "6"})
    assert rt.internet_settings()["enabled"] is True
    assert "inputs.ping" in open(os.path.join(rt.cfg.telegraf_dir, "telegraf.conf")).read()


def test_internet_saved_before_the_switch_is_off_only_when_all_probes_are_off(client):
    setup_admin(client)
    rt = client.rt
    s = rt.internet_settings()
    saved = {k: s[k] for k in ("public", "gateway", "dns", "speed", "line")}
    rt.db.set("module.internet", saved)
    assert rt.internet_settings()["enabled"] is True
    for p in ("public", "gateway", "dns", "speed"):
        saved[p]["enabled"] = False
    rt.db.set("module.internet", saved)
    assert rt.internet_settings()["enabled"] is False
