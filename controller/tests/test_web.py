"""Web flows: first-run setup, login, roles, CSRF."""

import json
import os
import re
import time

import httpx
import pytest
import respx
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
                               "password2": "a-long-password", "code": c.app.state.auth.setup_code()})
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
    assert json.loads(hc["config"]) == {}  # the ping URL is a credential
    assert json.loads(client.rt.secrets.get(f"channel.{hc['id']}.private")) == {"url": "https://hc-ping.com/abc"}
    assert client.rt.db.get("home.name") == "Test Home"


def test_csrf_required(client):
    tok = setup_admin(client)
    r = client.post("/modules/internet/thresholds", data={"csrf": "wrong"})
    assert "Form expired" in r.text
    r = client.post("/maintenance/snooze", data={"check": "internet.loss", "for": "1h"})
    assert "Form expired" in r.text
    assert tok


def test_viewer_is_read_only(client, cfg):
    tok = setup_admin(client)
    client.post("/users", data={"csrf": tok, "username": "family", "password": "another-long-one", "role": "viewer", "current": "a-long-password"})
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
    data = {"csrf": tok}
    for cid in ("fresh", "gateway", "reachable", "loss", "latency", "jitter", "dns", "speed"):
        data[f"internet.{cid}.enabled"] = "on"
    data.update({"internet.loss.warn": "5", "internet.loss.error": "20", "internet.latency.warn": "80",
                 "internet.jitter.warn": "15", "internet.jitter.error": "30", "internet.dns.warn": "500",
                 "internet.speed.warn": "70", "internet.fresh.error": "180"})
    r = client.post("/modules/internet/thresholds", data=data)
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
    assert "speedport.snr.warn" in client.get("/modules/speedport").text
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
    client.post("/system/reset", data={"csrf": tok, "mode": "history", "confirm": "DELETE", "current": "a-long-password"})
    assert db.one("SELECT COUNT(*) n FROM issues")["n"] == 0
    assert db.one("SELECT COUNT(*) n FROM users")["n"] == 1


def test_reset_everything_restarts_the_wizard(client):
    tok = setup_admin(client)
    client.post("/setup/home", data={"csrf": tok, "name": "Old Home", "tz": "Europe/Athens"})
    r = client.post("/system/reset", data={"csrf": tok, "mode": "all", "confirm": "RESET", "current": "a-long-password"})
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
    assert json.loads(ch["config"]) == {"server": "https://ntfy.sh"}
    assert "home-alerts-x1" in client.rt.secrets.get(f"channel.{ch['id']}.private")
    assert "home-alerts-x1" not in client.get("/notifications").text
    r = client.post("/notifications/add", data={"csrf": tok, "kind": "telegram", "telegram.chat_id": "42"})
    assert "bot token" in r.text
    r = client.post("/notifications/add", data={"csrf": tok, "kind": "healthchecks",
                                                "healthchecks.url": "https://hc-ping.com/u1"})
    assert "Added" in r.text and "https://hc-ping.com/u1" not in r.text  # a credential: encrypted, not shown
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
    assert "https://hc-ping.com/old" in client.rt.secrets.get(f"channel.{ch['id']}.private")
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


def test_modem_certificate_pin_flow(client, monkeypatch):
    from homewatch.modules.speedport import client as modem
    from .test_speedport import CERT_A, CERT_B

    tok = setup_admin(client)
    form = {"csrf": tok, "enabled": "on", "model": "speedport-plus-2", "host": "192.0.2.1", "username": "admin",
            "password": "s3cret", "interval_min": "15"}
    client.post("/modules/speedport", data=form)
    seen, presented = [], [CERT_A]

    async def fetch(host, timeout=10):
        return presented[0]

    async def read(host, user, password, cert, timeout=20):
        seen.append(cert)
        if cert != presented[0]:
            raise modem.CertificateRejected("the modem's certificate is not the trusted one")
        return modem.parse({"Device.DSL.Line.1.LinkStatus": "Up"})

    monkeypatch.setattr(modem, "fetch_certificate", fetch)
    monkeypatch.setattr(modem, "read", read)
    post = lambda: client.post("/modules/speedport/read", data={"csrf": tok}, headers={"HX-Request": "true"})
    assert "Modem read successfully" in post().text and modem.fingerprint(CERT_A) in post().text
    assert client.rt.db.get("speedport.state")["cert"] == CERT_A  # first use pinned

    presented[0] = CERT_B
    page = post().text
    assert "did not log in" in page and modem.fingerprint(CERT_B) in page
    st = client.rt.db.get("speedport.state")
    assert st["cert"] == CERT_A and st["cert_changed"]  # never replaced by itself

    fp = modem.fingerprint(CERT_B)
    r = client.post("/modules/speedport/trust-certificate", data={"csrf": tok, "fingerprint": "AA"})
    assert "changed again" in r.text and client.rt.db.get("speedport.state")["cert"] == CERT_A
    r = client.post("/modules/speedport/trust-certificate", data={"csrf": tok, "fingerprint": fp})
    assert "New certificate trusted" in r.text
    assert "Modem read successfully" in post().text and seen[-1] == CERT_B

    client.post("/modules/speedport", data={**form, "password": "", "host": "192.0.2.2"})
    assert "cert" not in client.rt.db.get("speedport.state")  # another device: trust starts over


def test_login_refuses_uploads_and_oversized_bodies(client):
    from homewatch.web import MAX_BODY

    setup_admin(client)
    client.cookies.clear()
    tok = csrf(client.get("/login").text)
    r = client.post("/login", data={"csrf": tok}, files={"x": ("big.bin", b"0" * 1000)})
    assert r.status_code == 400
    r = client.post("/login", content=b"a" * (MAX_BODY + 1), headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 413

    def chunks():  # no Content-Length: counted as it arrives
        for _ in range(9):
            yield b"a" * (1024 * 1024)

    r = client.post("/login", content=chunks(), headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 413


async def _slow(*a):
    import asyncio
    await asyncio.sleep(5)


def test_one_module_failing_doesnt_stop_the_others(client, monkeypatch):
    import asyncio

    from homewatch import runtime
    from homewatch.metrics import MetricsError

    setup_admin(client)
    rt = client.rt
    inet, ups = MODULES["internet"], MODULES["ups"]
    rt.db.set("module.ups", {"enabled": True, "host": "ups.test", "port": 3493, "ups": ""})
    monkeypatch.setattr(runtime, "MODULE_LIMIT", 0.01)
    monkeypatch.setattr(ups, "readings", _slow)
    ran = []

    async def fine(*a):
        ran.append(1)
        return "r"

    monkeypatch.setattr(inet, "readings", fine)
    monkeypatch.setattr(inet, "evaluate", lambda *a: [])
    res = asyncio.run(rt.run_checks())
    assert ran and any(r.key == "ups." + ups.checks[0].id and "no answer" in r.detail for r in res)

    async def store_down(*a):
        raise MetricsError("store down")

    pings = []

    async def heartbeat():
        pings.append(1)

    monkeypatch.setattr(inet, "readings", store_down)
    monkeypatch.setattr(rt, "heartbeat", heartbeat)
    asyncio.run(rt.run_checks())
    assert rt.last_error == "store down" and not pings
    assert rt.db.get("engine.fails") == 1


def test_status_says_what_is_unknown(client):
    from homewatch.modules import OK, CheckResult

    setup_admin(client)
    rt = client.rt
    first = MODULES["ups"].checks[0]
    rt.results = [CheckResult(f"ups.{first.id}", first.name, OK, "No reading yet", unknown=True)]
    page = client.get("/").text
    assert "UNKNOWN" in page and "All checks OK" not in page and "not read" in page


def test_history_shows_coverage(client):
    setup_admin(client)
    rt = client.rt
    assert "unknown" in client.get("/history?days=7").text
    now = time.time()
    rt.db.run("INSERT INTO coverage(site,module,start,end) VALUES(?,?,?,?)", (rt.cfg.site, "internet", now - 86400, now))
    page = client.get("/history?days=2").text
    assert "100.00 %" in page and "Monitored 50 %" in page


def test_first_admin_needs_the_setup_code(client):
    auth = client.app.state.auth
    page = client.get("/setup").text
    form = {"csrf": csrf(page), "username": "admin", "password": "a-long-password", "password2": "a-long-password"}
    r = client.post("/setup", data={**form, "code": "wrong"})
    assert "setup code is wrong" in r.text and not auth.has_users()
    code = auth.setup_code()
    r = client.post("/setup", data={**form, "code": code.upper() + " "})
    assert r.url.path == "/setup/home" and auth.has_users()
    assert auth.setup_code() != code  # used once


def test_setup_code_guessing_locks_out(client):
    page = client.get("/setup").text
    form = {"csrf": csrf(page), "username": "admin", "password": "a-long-password", "password2": "a-long-password"}
    for _ in range(5):
        client.post("/setup", data={**form, "code": "wrong"})
    r = client.post("/setup", data={**form, "code": client.app.state.auth.setup_code()})
    assert "Too many" in r.text and not client.app.state.auth.has_users()


def test_sensitive_actions_ask_for_the_password(client):
    tok = setup_admin(client)
    db = client.rt.db
    r = client.post("/users", data={"csrf": tok, "username": "x", "password": "another-long-one", "role": "admin", "current": "nope"})
    assert "current password is wrong" in r.text and not db.one("SELECT 1 FROM users WHERE username='x'")
    r = client.post("/users", data={"csrf": tok, "username": "x", "password": "another-long-one", "role": "admin",
                                    "current": "a-long-password"})
    assert "User added" in r.text
    uid = db.one("SELECT id FROM users WHERE username='x'")["id"]
    r = client.post(f"/users/{uid}/delete", data={"csrf": tok})
    assert "current password is wrong" in r.text and db.one("SELECT 1 FROM users WHERE id=?", (uid,))
    client.post("/setup/home", data={"csrf": tok, "name": "Keep", "tz": "UTC"})
    r = client.post("/system/reset", data={"csrf": tok, "mode": "all", "confirm": "RESET", "current": "nope"})
    assert "current password is wrong" in r.text and db.get("home.name") == "Keep"
    r = client.post(f"/users/{uid}/delete", data={"csrf": tok, "current": "a-long-password"})
    assert "User removed" in r.text


def test_sessions_are_stored_as_hashes(client):
    setup_admin(client)
    token = client.cookies.get("hw_session") or next(v for k, v in client.cookies.items() if k != "hw_pre")
    stored = client.rt.db.one("SELECT token FROM sessions")["token"]
    assert stored != token and client.app.state.auth.session(token) and not client.app.state.auth.session(stored)


async def test_module_pins_first_certificate_and_refuses_a_change(client, tmp_path):
    from homewatch.modules.apibase import why
    from homewatch.modules.pinning import fingerprint, verify_for
    import httpx

    from .test_speedport import CERT_A, CERT_B, FakeModem, KEY_A

    setup_admin(client)
    rt, m = client.rt, MODULES["synology"]
    ok = b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
    async with FakeModem(tmp_path, ok, CERT_A, KEY_A) as dev:
        url = f"https://127.0.0.1:{dev.port}"
        s = await m.pin_settings(rt, {"url": url, "verify": False})
        hp = f"127.0.0.1:{dev.port}"
        assert fingerprint(m.pins(rt)[hp]["cert"]) == fingerprint(CERT_A)
        assert rt.db.one("SELECT 1 FROM audit WHERE detail LIKE 'trusting the certificate%'")
        async with httpx.AsyncClient(verify=verify_for(s, url)) as http:
            assert (await http.get(url)).status_code == 200
        # the device now shows another certificate: refused before anything is sent
        pins = m.pins(rt)
        pins[hp]["cert"] = CERT_B
        rt.db.set(f"tls.{m.id}", pins)
        s = await m.pin_settings(rt, {"url": url, "verify": False})
        dev.received = b""
        async with httpx.AsyncClient(verify=verify_for(s, url)) as http:
            with pytest.raises(httpx.ConnectError) as e:
                await http.get(url)
        assert dev.received == b"" and "changed since" in why(e.value)
        await m.note_changes(rt, {"url": url}, why(e.value))
        row = m.cert_rows(rt)[0]
        assert row["new_fp"] == fingerprint(CERT_A)
        assert m.trust_certificate(rt, hp, "AA", type("U", (), {"username": "a"})())
        assert not m.trust_certificate(rt, hp, row["new_fp"], type("U", (), {"username": "a"})())
        assert m.pins(rt)[hp] == {"cert": CERT_A}


def test_trust_button_and_connections_list(client):
    from homewatch.modules.pinning import fingerprint

    from .test_speedport import CERT_A, CERT_B

    tok = setup_admin(client)
    rt = client.rt
    rt.db.set("module.synology", {"enabled": True, "url": "https://nas.test:5001", "username": "u", "verify": False})
    rt.db.set("module.homeassistant", {"enabled": True, "url": "http://ha.test:8123", "verify": False})
    rt.db.set("tls.synology", {"nas.test:5001": {"cert": CERT_A, "new": CERT_B}})
    page = client.get("/modules/synology").text
    assert fingerprint(CERT_B) in page and "Trust the new certificate" in page
    r = client.post("/modules/synology/trust-certificate", data={"csrf": tok, "host": "nas.test:5001", "fingerprint": fingerprint(CERT_B)})
    assert "New certificate trusted" in r.text and rt.db.get("tls.synology")["nas.test:5001"] == {"cert": CERT_B}
    page = client.get("/system").text
    assert "HTTPS, certificate pinned" in page and "Plain HTTP" in page


def test_changed_destination_forgets_the_pin_and_pins_stay_out_of_exports(client):
    from homewatch import versions

    from .test_speedport import CERT_A

    setup_admin(client)
    rt = client.rt
    rt.db.set("tls.synology", {"nas.test:5001": {"cert": CERT_A}})
    assert "tls.synology" not in versions.snapshot(rt.db)["settings"]
    MODULES["synology"].forget_credentials(rt)
    assert rt.db.get("tls.synology") is None


def test_wrong_master_key_is_named(db):
    import os as _os

    from homewatch.secrets import Secrets, WrongMasterKey

    Secrets(db, _os.urandom(32)).put("x", "y")
    with pytest.raises(WrongMasterKey):
        Secrets(db, _os.urandom(32)).get("x")


def test_failed_save_keeps_what_was_typed_but_no_secret(client):
    from homewatch.modules.apibase import ApiError

    setup_admin(client)
    tok = csrf(client.get("/modules/synology").text)
    m = MODULES["synology"]

    async def fail(*a):
        raise ApiError("no such NAS")

    orig, m.fetch = m.fetch, fail
    try:
        r = client.post("/modules/synology", data={"csrf": tok, "url": "https://typed.test:5001", "username": "typed-user",
                                                   "secret": "the-password"})
    finally:
        m.fetch = orig
    assert "no such NAS" in r.text and "typed.test" in r.text and "typed-user" in r.text and "the-password" not in r.text
    assert not client.rt.db.get("module.synology")
    assert "typed.test" not in client.get("/modules/synology").text  # once only


def test_read_now_keeps_unsaved_ticks(client):
    from homewatch.modules.apibase import Choice

    setup_admin(client)
    tok = csrf(client.get("/modules/pbs").text)
    m = MODULES["pbs"]
    client.rt.db.set("module.pbs", {"enabled": True, "url": "https://p.test:8007", "token_id": "a@b!c", "verify": True, "chosen": ["a"]})
    client.rt.secrets.put("module.pbs.secret", "x")
    m.snap, m.snap_ts = {"_summary": "s"}, 1.0
    saved = m.fetch, m.choices, m.check, m.tables

    async def read(*a):
        return {"_summary": "s"}

    m.fetch, m.choices, m.check = read, lambda snap: [Choice("a", "A"), Choice("b", "B")], lambda *a: []
    m.tables = lambda *a: []
    try:
        r = client.post("/modules/pbs/refresh", data={"csrf": tok, "enabled": "on", "url": "https://p.test:8007", "choose.b": "on"})
        assert re.search(r'name="choose.b" checked', r.text) and not re.search(r'name="choose.a" checked', r.text)
    finally:
        m.fetch, m.choices, m.check, m.tables = saved
        m.snap = None


@respx.mock
def test_channel_test_uses_the_form_and_saves_nothing(client):
    tok = setup_admin(client)
    client.post("/notifications/add", data={"csrf": tok, "kind": "ntfy", "ntfy.url": "https://saved.test/topic"})
    ch = client.rt.db.one("SELECT * FROM channels WHERE kind='ntfy'")
    typed = respx.post("https://typed.test/other").mock(return_value=httpx.Response(200))
    r = client.post(f"/notifications/{ch['id']}/test", data={"csrf": tok, "name": "Typed name", "url": "https://typed.test/other"})
    assert typed.called and "test message sent" in r.text and "Typed name" in r.text
    assert "saved.test" in client.rt.secrets.get(f"channel.{ch['id']}.private")
    # empty: the saved address
    saved = respx.post("https://saved.test/topic").mock(return_value=httpx.Response(200))
    client.post(f"/notifications/{ch['id']}/test", data={"csrf": tok, "name": "n", "url": ""})
    assert saved.called


def test_add_channel_error_keeps_the_form(client):
    tok = setup_admin(client)
    r = client.post("/notifications/add", data={"csrf": tok, "kind": "telegram", "telegram.name": "My bot", "telegram.chat_id": "42"})
    assert "bot token" in r.text and "My bot" in r.text and re.search(r'value="telegram"\s+selected', r.text)


def test_setup_headings_fit_the_module(client):
    setup_admin(client)
    assert "separate account" in client.get("/modules/synology").text
    ups = client.get("/modules/ups").text
    assert "Before you connect" in ups and "read-only account" not in ups


def test_status_isnt_green_when_it_cant_tell(client):
    setup_admin(client)
    rt = client.rt
    rt.last_run, rt.last_error = time.time(), "VictoriaMetrics unreachable"
    page = client.get("/").text
    assert "Status unknown" in page and "All checks OK" not in page
    rt.last_error = ""
    page = client.get("/").text
    assert "Nothing is being checked yet" in page and "All checks OK" not in page


def test_history_says_when_it_is_cut_off(client, monkeypatch):
    import homewatch.web as w

    setup_admin(client)
    monkeypatch.setattr(w, "HISTORY_ROWS", 2)
    add = "INSERT INTO issues(site,check_key,level,worst,started,resolved) VALUES(?,'internet.loss',1,1,?,NULL)"
    now = time.time()
    for i in range(2):
        client.rt.db.run(add, (client.rt.cfg.site, now - i))
    assert "older ones in this period are left out" not in client.get("/history").text
    client.rt.db.run(add, (client.rt.cfg.site, now - 3))
    assert "older ones in this period are left out" in client.get("/history").text


def test_delivery_health_on_the_notifications_page(client):
    tok = setup_admin(client)
    client.post("/notifications/add", data={"csrf": tok, "kind": "ntfy", "ntfy.url": "https://n.test/topic"})
    rt = client.rt
    cid = rt.db.one("SELECT id FROM channels")["id"]
    assert "Last delivered: nothing yet" in client.get("/notifications").text
    now = time.time()
    rt.db.set(f"channel.{cid}.last_ok", now - 7200)
    rt.db.run("INSERT INTO outbox(channel_id,created,level,title,body,attempts,next_try,last_error) VALUES(?,?,2,'t','b',3,?,'ntfy answered HTTP 500')",
              (cid, now - 600, now + 60))
    page = client.get("/notifications").text
    assert re.search(r"Last delivered: 2\s*h\S*( 0 min)? ago", page) and "1 message waiting" in page and "Next try in" in page and "ntfy answered HTTP 500" in page
    client.post(f"/notifications/{cid}/delete", data={"csrf": tok})
    assert rt.db.get(f"channel.{cid}.last_ok") is None


def test_long_lists_get_search_short_ones_dont(client):
    from homewatch.modules.apibase import Choice

    setup_admin(client)
    m = MODULES["pbs"]
    client.rt.db.set("module.pbs", {"enabled": True, "url": "https://p.test:8007", "token_id": "a@b!c", "verify": True, "chosen": []})
    m.snap = {"_summary": "s"}
    saved = m.choices, m.tables
    m.tables = lambda *a: []
    try:
        m.choices = lambda snap: [Choice(str(i), f"n{i}") for i in range(14)]
        assert "data-choice-search" not in client.get("/modules/pbs").text
        m.choices = lambda snap: [Choice(str(i), f"n{i}") for i in range(15)]
        assert "data-choice-search" in client.get("/modules/pbs").text
    finally:
        m.choices, m.tables = saved
        m.snap = None


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


def test_checks_menu_is_gone_and_its_parts_moved(client):
    tok = setup_admin(client)
    assert 'href="/checks"' not in client.get("/").text
    assert client.get("/checks").url.path == "/modules"
    assert 'action="/modules/internet/thresholds"' in client.get("/modules/internet").text
    assert "/thresholds" not in client.get("/modules/ups").text  # not connected yet: nothing to set
    r = client.post("/modules/internet/thresholds", data={"csrf": tok, "internet.loss.warn": "60", "internet.loss.error": "50",
                                                         "internet.loss.enabled": "on"})
    assert "must come before" in r.text
    assert 'action="/notifications/timing"' in client.get("/notifications").text
    r = client.post("/notifications/timing", data={"csrf": tok, "timing.interval": "300", "timing.confirm": "3",
                                                  "timing.remind_h": "6", "timing.fail_alert": "5"})
    assert "Saved" in r.text and client.rt.db.get("engine.timing")["interval"] == 300
    assert "must be between" in client.post("/notifications/timing", data={"csrf": tok, "timing.interval": "1"}).text
    assert 'action="/maintenance/snooze"' in client.get("/maintenance").text
    r = client.post("/maintenance/snooze", data={"csrf": tok, "check": "internet.loss", "for": "1h"})
    assert r.url.path == "/maintenance" and "Snoozed" in r.text and "Packet loss" in r.text
    r = client.post("/maintenance/unsnooze", data={"csrf": tok, "check": "internet.loss"})
    assert "Snooze ended" in r.text and "Nothing is snoozed" in r.text


def test_snooze_from_a_problem_on_the_status_page(client):
    tok = setup_admin(client)
    client.rt.db.run("INSERT INTO issues(site,check_key,level,worst,started,detail) VALUES('home','internet.loss',1,1,1,'x')")
    assert 'action="/maintenance/snooze"' in client.get("/partials/status").text
    r = client.post("/maintenance/snooze", data={"csrf": tok, "check": "internet.loss", "for": "4h", "_from": "status"})
    assert r.url.path == "/" and "Snoozed" in r.text


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


def test_history_period_counts_events_not_start_times(client):
    setup_admin(client)
    db, now = client.rt.db, time.time()
    add = "INSERT INTO issues(site,check_key,level,worst,started,resolved,detail,resolved_detail) VALUES('home',?,1,1,?,?,?,?)"
    db.run(add, ("internet.loss", now - 3 * 86400, now - 3600, "started-earlier", "ended-today"))
    db.run(add, ("internet.dns", now - 3 * 86400, None, "still-open", ""))
    db.run(add, ("internet.jitter", now - 3 * 86400, now - 2 * 86400, "over-before", "over-before-too"))
    day = client.get("/history?days=1").text
    assert "ended-today" in day and "started-earlier" not in day and "still-open" in day and "over-before" not in day
    week = client.get("/history?days=7").text
    assert "started-earlier" in week and "over-before" in week
    assert "data-autosubmit" in day


def test_check_now_button_says_running(client):
    setup_admin(client)
    client.post("/setup/home", data={})
    assert 'data-busy="Running…"' in client.get("/partials/status").text


def test_history_level_filter_lists_only_that_level(client):
    setup_admin(client)
    db, now = client.rt.db, time.time()
    add = "INSERT INTO issues(site,check_key,level,worst,started,resolved,detail,resolved_detail) VALUES('home',?,?,?,?,?,?,?)"
    db.run(add, ("internet.loss", 1, 1, now - 7200, now - 3600, "a-warning", "warning-over"))
    db.run(add, ("internet.reachable", 2, 2, now - 7200, now - 3600, "an-error", "error-over"))
    warn = client.get("/history?level=1").text
    assert "a-warning" in warn and "lasted" in warn and "warning-over" not in warn and "an-error" not in warn
    err = client.get("/history?level=2").text
    assert "an-error" in err and "error-over" not in err and "a-warning" not in err
    every = client.get("/history").text
    assert "warning-over" in every and "error-over" in every


def test_history_all_modules_is_one_timeline_and_one_module_is_a_section(client):
    setup_admin(client)
    db, now = client.rt.db, time.time()
    add = "INSERT INTO issues(site,check_key,level,worst,started,detail) VALUES('home',?,1,1,?,?)"
    db.run(add, ("ups.load", now - 90, "older-ups"))
    db.run(add, ("internet.loss", now - 60, "line-event"))
    db.run(add, ("ups.health", now - 30, "newer-ups"))
    every = client.get("/history").text
    assert every.index("newer-ups") < every.index("line-event") < every.index("older-ups")  # newest first
    assert "<th>Source</th>" in every and "<h2>UPS" not in every and "<h2>Internet line" not in every
    assert ">UPS (NUT)<" in every or ">UPS<" in every
    assert '<option value="ups"' in every and 'value="ups.load"' not in every
    one = client.get("/history?module=ups").text
    assert "newer-ups" in one and "older-ups" in one and "line-event" not in one
    assert "<th>Source</th>" not in one and "<h2>UPS" in one


def test_opnsense_key_is_encrypted_and_kept_out_of_versions_and_exports(client, monkeypatch):
    from homewatch import versions

    tok = setup_admin(client)
    rt, m = client.rt, MODULES["opnsense"]
    seen = {}

    async def fetch(s, secret, now):
        seen.update(key=s["api_key"], secret=secret)
        return {"_summary": "ok"}

    monkeypatch.setattr(m, "fetch", fetch)
    for part in ("tables", "choices", "filters"):  # the fake reading has nothing to show
        monkeypatch.setattr(m, part, lambda *a: [])
    r = client.post("/modules/opnsense", data={"csrf": tok, "url": "192.168.1.1", "secret": "s"})
    assert "Enter the API key" in r.text
    client.post("/modules/opnsense", data={"csrf": tok, "url": "192.168.1.1", "api_key": "the-key", "secret": "s"})
    assert seen == {"key": "the-key", "secret": "s"}
    assert "api_key" not in rt.db.get("module.opnsense") and rt.secrets.get("module.opnsense.api_key") == "the-key"
    # kept when left empty, and still used
    seen.clear()
    client.post("/modules/opnsense", data={"csrf": tok, "enabled": "on", "url": "192.168.1.1"})
    assert seen["key"] == "the-key"
    assert "the-key" not in client.get("/modules/opnsense").text
    assert "the-key" not in versions.to_yaml(rt.db)
    assert all("the-key" not in r["data"] for r in rt.db.all("SELECT data FROM config_versions"))
    assert all("the-key" not in r["detail"] for r in rt.db.all("SELECT detail FROM audit"))
    m.snap = None


def test_old_plain_key_moves_to_secrets_everywhere(client, tmp_path):
    from homewatch import versions

    setup_admin(client)
    rt = client.rt
    rt.db.set("module.opnsense", {"enabled": True, "url": "https://fw", "api_key": "old-key"})
    versions.record(rt.db, "t", "t")
    assert any("old-key" in r["data"] for r in rt.db.all("SELECT data FROM config_versions"))
    export = tmp_path / "homewatch-config-2026-10-30.yaml"
    export.write_text("settings:\n  module.opnsense:\n    api_key: old-key\n    url: https://fw\n")
    assert versions.move_secret_fields(rt.db, rt.secrets)
    versions.scrub_exports(str(tmp_path))
    assert rt.secrets.get("module.opnsense.api_key") == "old-key"
    assert rt.db.get("module.opnsense") == {"enabled": True, "url": "https://fw"}
    assert all("old-key" not in r["data"] for r in rt.db.all("SELECT data FROM config_versions"))
    assert "old-key" not in export.read_text() and "url: https://fw" in export.read_text()
    assert not versions.move_secret_fields(rt.db, rt.secrets)
    # an old export imported later can't bring it back
    snap = versions.snapshot(rt.db)
    snap["settings"]["module.opnsense"]["api_key"] = "from-import"
    versions.apply(rt.db, snap)
    assert "api_key" not in rt.db.get("module.opnsense")


def test_help_page_has_every_modules_instructions(client, cfg):
    setup_admin(client)
    text = client.get("/help").text
    assert 'href="/help"' in text and "www-ssl" in text and "pveum user token add" in text
    for m in MODULES.values():
        assert m.name.replace("&", "&amp;") in text and m.setup and m.description, m.id
        for c in m.checks:
            assert c.name in text or c.name.replace("'", "&#39;") in text
    assert 'href="/modules/ups"' in text
    from homewatch.auth import Auth
    Auth(client.rt.db).create_user("viewer", "a-long-password", "viewer")
    client.post("/logout", data={"csrf": csrf(text)})
    login = client.get("/login")
    client.post("/login", data={"username": "viewer", "password": "a-long-password", "csrf": csrf(login.text)})
    seen = client.get("/help")
    assert seen.status_code == 200 and "www-ssl" in seen.text and 'href="/modules/ups"' not in seen.text


def test_status_tiles_start_closed(client):
    from homewatch.modules import CheckResult

    setup_admin(client)
    rt = client.rt
    rt.results = [CheckResult("ups.load", "Load", 0, "fine")]
    body = client.get("/partials/status").text
    assert "module-grid" in body and 'data-module="ups">' in body and " open>" not in body
    assert "Show modules as tiles" not in client.get("/system").text
    rt.results = []


def test_home_assistant_form_shows_full_read_default_for_old_settings(client):
    setup_admin(client)
    client.rt.db.set("module.homeassistant", {"enabled": True, "url": "http://ha.test:8123", "verify": False})
    page = client.get("/modules/homeassistant").text
    assert 'name="full_every" value="30"' in page and "From 5 to 1440" in page


def test_docker_save_refused_when_one_host_unreachable(client, monkeypatch):
    tok = setup_admin(client)
    from homewatch.modules.docker.plugin import plugin

    async def partial(s, secret, now):
        return {"hosts": [{"host": "socket-proxy:2375", "name": "hw", "version": "29", "containers": []}],
                "errors": {"hosts:2375": "can't reach the Docker proxy (ConnectError)"}, "_summary": "1 host(s)"}

    monkeypatch.setattr(plugin, "fetch", partial)
    r = client.post("/modules/docker", data={"csrf": tok, "hosts": "hosts, socket-proxy"})
    assert "Nothing saved: can&#39;t read hosts:2375" in r.text or "Nothing saved: can't read hosts:2375" in r.text
    assert client.rt.db.get("module.docker") is None
    assert "no password" not in client.get("/modules/docker").text  # the standing banner is gone

    async def full(s, secret, now):
        return {"hosts": [{"host": "socket-proxy:2375", "name": "hw", "version": "29", "containers": []}],
                "errors": {}, "_summary": "1 host(s)"}

    monkeypatch.setattr(plugin, "fetch", full)
    r = client.post("/modules/docker", data={"csrf": tok, "hosts": "socket-proxy"})
    assert "Nothing saved" not in r.text and client.rt.db.get("module.docker")["hosts"] == "socket-proxy"
