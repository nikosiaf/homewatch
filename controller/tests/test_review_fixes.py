"""Regression tests for the defects found in the hw-review (B1-B11): each asserts the correct behaviour."""

import asyncio
import os
import re
import socket
import ssl
import threading
from types import SimpleNamespace

import pytest
import respx
from fastapi.testclient import TestClient

from homewatch import api, mqtt, todo, versions
from homewatch.db import DB
from homewatch.engine import Engine, Event, thresholds
from homewatch.modules import ERROR, OK, WARNING, Thresholds
from homewatch.modules import internet as inet
from homewatch.modules.docker.plugin import DockerModule
from homewatch.modules.internet import checks as inet_checks
from homewatch.modules.internet.checks import Readings, Speed
from homewatch.modules.internet.collect import collect
from homewatch.modules.mikrotik.plugin import MikroTikModule
from homewatch.modules.opnsense.plugin import OPNsenseModule
from homewatch.modules.pinning import CHANGED, fingerprint
from homewatch.modules.proxmox import checks as pve_checks
from homewatch.modules.proxmox.client import ApiError as PveApiError, Snapshot
from homewatch.modules.proxmox.plugin import ProxmoxModule
from homewatch.notify import Notifier
from homewatch.runtime import CONFIG_TABLES, HISTORY_TABLES, Runtime
from homewatch.secrets import Secrets
from homewatch.web import create_app

from .test_speedport import CERT_A, CERT_B, KEY_A, self_signed

T0 = 1_800_000_000.0
DAY = 86400


def defaults(checks):
    return {c.id: Thresholds(c.warn, c.error, c.default_on) for c in checks}


# ---- B1. Proxmox: a failed read keeps the disks, updates and kernels of the last disk read ------------------------
def _proxmox(db, fail_on=()):
    m = ProxmoxModule()
    calls = {"n": 0, "with_disks": []}

    class FakeClient:
        async def snapshot(self, with_disks=True, now=None):
            calls["n"] += 1
            calls["with_disks"].append(with_disks)
            if calls["n"] in fail_on:
                raise PveApiError("can't reach Proxmox (ReadTimeout)")
            s = Snapshot(nodes=[{"node": "pve", "status": "online", "cpu": 0.1, "mem": 1, "maxmem": 2,
                                 "disk": 1, "maxdisk": 2, "uptime": 1000}])
            if with_disks:
                s.disks = {"pve": [{"devpath": "/dev/sda", "health": "FAILED"}]}
                s.updates = {"pve": [{"package": "pve-manager", "old": "9.0", "new": "9.1"}]}
                s.kernels = {"pve": {"running": "6.8.1-1", "newest": "6.8.1-1"}}
            return s

    async def fake_client(rt, s):
        return FakeClient()

    async def no_macs(rt, s, now):
        return None

    m._client, m._guest_macs = fake_client, no_macs
    s = {"url": "https://pve:8006", "verify": True}
    th = defaults(pve_checks.CHECKS)

    def run(now):
        asyncio.run(m.read(SimpleNamespace(db=db), s, now))
        res = pve_checks.evaluate(s, th, pve_checks.State(now=now, snap=m.snap, error=m.error))
        res = todo.apply(db, "home", m, th, res, now)
        return {r.key.split(".")[1]: r for r in res}

    return m, run, calls


def test_proxmox_failed_read_keeps_disk_and_update_data(db):
    _, run, calls = _proxmox(db, fail_on=(2,))
    first = run(T0)  # with disks: SMART failed, one update pending
    assert first["disks"].status == ERROR and "To do" in first["updates"].detail
    run(T0 + 300)  # one transient failure
    third = run(T0 + 600)  # the next good read, within the hour: the slow data is carried over
    assert calls["with_disks"] == [True, False, False]  # no extra smartctl run
    assert third["disks"].status == ERROR and "SMART not passed" in third["disks"].detail
    assert third["updates"].detail.startswith("To do:") and "pve-manager" not in third["updates"].detail
    assert db.one("SELECT done FROM todos WHERE check_key='proxmox.updates'")["done"] is None  # still open


def test_proxmox_restore_or_new_pin_keeps_slow_data(db):
    m, run, calls = _proxmox(db)
    run(T0)
    m.snap = None  # what a config restore or accepting a new certificate does
    again = run(T0 + 600)
    assert calls["with_disks"] == [True, False]
    assert again["disks"].status == ERROR


def test_proxmox_new_address_reads_disks_again(db):
    m, run, calls = _proxmox(db)
    run(T0)
    m.slow = ("https://other:8006",) + m.slow[1:]  # kept from another host: not reused
    run(T0 + 600)
    assert calls["with_disks"] == [True, True]


def test_proxmox_updates_not_read_is_unknown():
    s = Snapshot(nodes=[{"node": "pve", "status": "online", "cpu": 0.1, "mem": 1, "maxmem": 2, "disk": 1,
                         "maxdisk": 2, "uptime": 1000}])
    res = {r.key: r for r in pve_checks.evaluate({}, defaults(pve_checks.CHECKS), pve_checks.State(now=T0, snap=s))}
    assert res["proxmox.updates"].detail == "No reading yet" and res["proxmox.updates"].unknown


# ---- B2. To do: a source that wasn't read leaves the to-do open, and its waiting clock goes on ---------------------
def test_todo_clock_keeps_running_through_an_unreadable_exporter_read(db):
    m = DockerModule()
    th = defaults(m.checks)
    s = {"enabled": True, "hosts": "10.0.0.5", "exporters": "10.0.0.5"}
    host = {"host": "10.0.0.5:2375", "name": "dock", "version": "29", "containers": []}
    pending = {"hosts": [host], "errors": {}, "exporters": [{"addr": "10.0.0.5:9100", "name": "dock",
                                                             "updates": 5, "reboot": False}]}
    down = {"hosts": [host], "errors": {"10.0.0.5:9100 (node_exporter)": "can't reach node_exporter (ConnectError)"},
            "exporters": []}

    def updates(snap, now):
        res = m.evaluate(s, th, (s, snap, "", now))
        return next(r for r in todo.apply(db, "home", m, th, res, now) if r.key == "docker.updates")

    assert updates(pending, T0).detail.startswith("To do:")
    r = updates(down, T0 + DAY)  # one missed scrape
    assert r.status == OK and r.detail.startswith("To do: dock: 5 package updates") and "not read now" in r.detail
    assert r.value == 1.0
    assert db.one("SELECT done FROM todos WHERE check_key='docker.updates'")["done"] is None
    r = updates(pending, T0 + 15 * DAY)  # 15 days after the updates appeared
    assert r.status == WARNING and r.value == 15.0  # limit 14 days


def test_docker_partly_read_exporters_are_not_up_to_date():
    m = DockerModule()
    s = {"enabled": True, "hosts": "a", "exporters": "a, b"}
    snap = {"hosts": [{"host": "a:2375", "name": "a", "version": "29", "containers": []}],
            "errors": {"b:9100 (node_exporter)": "timeout"},
            "exporters": [{"addr": "a:9100", "name": "a", "updates": 0, "reboot": False}]}
    r = next(r for r in m.evaluate(s, defaults(m.checks), (s, snap, "", T0)) if r.key == "docker.updates")
    assert r.status == OK and r.unknown and "b:9100" in r.detail


@respx.mock
async def test_synology_update_server_not_read_is_unknown():
    from . import test_synology as t
    respx.get("https://nas.test:5001/webapi/entry.cgi").mock(side_effect=t.dsm())
    snap = await t.plugin.fetch(t.S, "pw", t.NOW)
    snap["update"] = None  # SYNO.Core.Upgrade.Server check failed (e.g. no internet)
    r = t.run(snap)["updates"]
    assert r.status == OK and r.detail == "not readable" and r.unknown


@respx.mock
async def test_pbs_updates_not_read_is_unknown():
    from . import test_pbs as t
    t.mock()
    snap = await t.plugin.fetch(t.S, "s", t.NOW)
    snap["updates"] = None
    r = t.run(snap, [])["updates"]
    assert r.status == OK and r.detail == "not readable" and r.unknown


def test_mikrotik_unread_device_keeps_firmware_todo_open():
    m = MikroTikModule()
    from .test_mikrotik import dev, run
    res = run(m, {"devices": [dev("sw1")], "errors": {"sw2": "timeout"}}, [], os_updates=False)
    assert res["firmware"].status == OK and res["firmware"].unknown
    res = run(m, {"devices": [dev("sw1")], "errors": {}}, [])
    assert res["firmware"].status == OK and not res["firmware"].unknown


# ---- B3. Internet: "Speed test recent" stays when the newest tests all failed ------------------------------------
class NoMetrics:
    async def query(self, expr, at=None):
        return []

    async def query_range(self, expr, start, end, step):
        return []


def test_speed_age_warns_when_tests_keep_failing(db):
    s = inet.load(db)
    s["public"]["enabled"] = s["gateway"]["enabled"] = s["dns"]["enabled"] = False
    s["speed"]["enabled"] = True
    th = thresholds(db, "home", "internet", inet_checks.CHECKS)
    eng = Engine(db, "home")
    db.run("INSERT INTO speedtests(site,ts,engine,down,up,latency,ok) VALUES('home',?,'cloudflare',90,9,10,1)",
           (T0 - 20 * 3600,))
    for h in range(8):  # 8 failures since the last good test
        db.run("INSERT INTO speedtests(site,ts,engine,ok) VALUES('home',?,'cloudflare',0)", (T0 - h * 3600,))
    r = asyncio.run(collect(NoMetrics(), db, "home", T0))
    assert len(r.speeds) == 6 and r.speeds[-1].ok and not any(x.ok for x in r.speeds[:5])  # newest first
    res = inet_checks.evaluate(s, th, r)
    age = next(x for x in res if x.key == "internet.speed_age")
    assert age.status == WARNING and "20.0 h ago" in age.detail
    events = eng.apply("internet", res, T0) + eng.apply("internet", res, T0 + 120)
    assert any(e.kind == "open" and e.key == "internet.speed_age" for e in events)
    assert db.one("SELECT resolved FROM issues WHERE check_key='internet.speed_age'")["resolved"] is None


def test_speed_age_without_any_good_test_is_unknown(db):
    s = inet.load(db)
    s["public"]["enabled"] = s["gateway"]["enabled"] = s["dns"]["enabled"] = False
    s["speed"]["enabled"] = True
    th = thresholds(db, "home", "internet", inet_checks.CHECKS)
    fails = [Speed(T0 - h * 3600, None, None, False, engine="cloudflare") for h in range(5)]
    res = {x.key: x for x in inet_checks.evaluate(s, th, Readings(now=T0, speeds=fails))}
    assert res["internet.speed"].detail == "No speed test yet"
    assert res["internet.speed_age"].status == OK and res["internet.speed_age"].unknown


# ---- B4. "Monitoring data readable again" goes where "can't read" went ---------------------------------------------
def test_selfok_is_routed_like_the_alert_it_ends(db):
    n = Notifier(db, Secrets(db, os.urandom(32)))
    db.run("INSERT INTO channels(kind,name,levels,config) VALUES('telegram','tg','[2]','{}')")
    fail = Event("selffail", "homewatch.read", "homewatch data", ERROR, ERROR, "Can't read monitoring data")
    ok = Event("selfok", "homewatch.read", "homewatch data", OK, ERROR, "Monitoring data readable again")
    assert ok.route_level == ERROR
    assert n.dispatch([fail], (0, 0), T0) == 1
    assert n.dispatch([ok], (0, 0), T0) == 1
    assert "readable again" in db.one("SELECT body FROM outbox ORDER BY id DESC LIMIT 1")["body"]


# ---- B5. "Reset everything" deletes tokens, config copies and the rest -------------------------------------------
@pytest.fixture
def client(cfg):
    rt = Runtime.create(cfg)
    app = create_app(rt, start_loops=False)
    with TestClient(app, base_url="https://testserver") as c:
        c.rt = rt
        yield c


def _csrf(html):
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def _setup(c):
    r = c.get("/")
    r = c.post("/setup", data={"csrf": _csrf(r.text), "username": "admin", "password": "a-long-password",
                               "password2": "a-long-password"})
    return _csrf(r.text)


def test_reset_everything_deletes_api_tokens_and_config_copies(client):
    tok = _setup(client)
    token = api.new_token(client.rt.db, "script", "admin")
    client.rt.db.run("INSERT INTO device_configs(site,module,device,name,ts,text) VALUES('home','mikrotik','sw1',"
                     "'sw1',1,'/system identity set name=sw1')")
    r = client.post("/system/reset", data={"csrf": tok, "mode": "all", "confirm": "RESET", "current": "a-long-password"})
    assert r.url.path == "/setup"
    _setup(client)  # a new owner sets homeWatch up again
    r = client.get("/api/v1/status", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401
    for t in ("api_tokens", "action_tokens", "device_configs", "topology_edges", "known_devices", "todos", "flaps",
              "entity_seen"):
        assert client.rt.db.one(f"SELECT COUNT(*) n FROM {t}")["n"] == 0, t  # noqa: S608


RESET_KEEPS: tuple[str, ...] = ()  # tables a reset deliberately leaves alone (none today)


def test_every_table_is_covered_by_a_reset_list(tmp_path):
    """A new migration's table must be put in HISTORY_TABLES or CONFIG_TABLES (or, on purpose, RESET_KEEPS)."""
    db = DB(str(tmp_path / "r.db"))
    tables = {r["name"] for r in db.all("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    assert tables - set(HISTORY_TABLES) - set(CONFIG_TABLES) - set(RESET_KEEPS) == set()
    assert set(HISTORY_TABLES) | set(CONFIG_TABLES) <= tables  # and every listed table exists


# ---- B7. Imported quiet hours are checked; a bad stored value doesn't stop the run's messages ----------------------
IMPORT = ("format: 1\nsettings: {}\nchecks: []\nmaintenance: []\nchannels:\n"
          "- {id: 1, kind: telegram, name: tg, enabled: 1, levels: '[1, 2]', quiet_start: %s, quiet_end: '07:00',\n"
          "   weekly_report: 0, config: '{\"chat_id\": \"1\"}'}\n")


def test_imported_quiet_hours_are_validated():
    snap, err = versions.from_yaml(IMPORT % "22:00")  # YAML reads an unquoted 22:00 as the integer 1320
    assert not err and snap["channels"][0]["quiet_start"] == 1320
    assert "Quiet hours" in versions.clean_import(snap)
    snap, err = versions.from_yaml(IMPORT % "'22:00'")
    assert not err and not versions.clean_import(snap)


def test_bad_stored_quiet_hours_dont_break_dispatch(db):
    n = Notifier(db, Secrets(db, os.urandom(32)))
    db.run("INSERT INTO channels(kind,name,levels,config,quiet_start,quiet_end) VALUES('telegram','a','[1,2]','{}',"
           "'1320','07:00')")
    db.run("INSERT INTO channels(kind,name,levels,config) VALUES('telegram','b','[1,2]','{}')")
    warn = Event("open", "proxmox.cpu", "Proxmox VE · Node CPU", WARNING, WARNING, "CPU 90 %", T0)
    assert n.dispatch([warn], (0, 1), T0) == 2  # sent now, not lost


def test_one_failing_channel_doesnt_cost_the_others(db, monkeypatch):
    from homewatch import notify
    n = Notifier(db, Secrets(db, os.urandom(32)))
    db.run("INSERT INTO channels(kind,name,levels,config) VALUES('telegram','a','[1,2]','{}')")
    db.run("INSERT INTO channels(kind,name,levels,config) VALUES('telegram','b','[1,2]','{}')")
    real = notify.in_quiet

    def boom(ch, now, zone):
        if ch["name"] == "a":
            raise ValueError("broken")
        return real(ch, now, zone)

    monkeypatch.setattr(notify, "in_quiet", boom)
    warn = Event("open", "proxmox.cpu", "Proxmox VE · Node CPU", WARNING, WARNING, "CPU 90 %", T0)
    assert n.dispatch([warn], (0, 1), T0) == 1


def test_channel_edit_checks_quiet_hours():
    from homewatch.notify import quiet_error
    assert quiet_error("22:00", "07:00") == "" and quiet_error("", "") == "" and quiet_error(None, None) == ""
    assert quiet_error("22:00", "") and quiet_error("25:00", "07:00") and quiet_error("1320", "07:00")
    assert quiet_error(1320, "07:00")


# ---- B8. OPNsense: two in-use certificates with the same expiry ---------------------------------------------------
def _opnsense_certs(certs):
    m = OPNsenseModule()
    snap = {"gateways": [], "resources": {"memory": {"used": 1, "total": 2}}, "disk": {"devices": []},
            "time": {}, "activity": None, "services": None, "haproxy": None, "firmware": None, "certs": certs}
    return next(r for r in m.check({}, defaults(m.checks), snap, T0) if r.key == "opnsense.certs")


def test_opnsense_equal_cert_expiry():
    r = _opnsense_certs([{"descr": "Web GUI", "valid_to": str(T0 + 90 * DAY), "in_use": "1"},
                         {"descr": "HAProxy", "valid_to": str(T0 + 90 * DAY), "in_use": "1"}])
    assert r.status == OK and r.value == 90 and "expires in 90 days" in r.detail


def test_opnsense_certs_without_expiry():
    r = _opnsense_certs([{"descr": "Web GUI", "in_use": "1"}])
    assert r.status == OK and "No expiry date" in r.detail


# ---- B6. Several hosts or devices: an item on one that can't be read keeps its last known state ---------------------
def test_unreadable_host_keeps_its_stopped_container():
    m = DockerModule()
    th = defaults(m.checks)
    s = {"enabled": True, "hosts": "a, b"}

    def ctr(state):
        return {"name": "nvr", "project": "", "image": "x", "state": state, "health": "", "exit": 1,
                "policy": "unless-stopped", "restarts": 0, "since": ""}

    a = {"host": "a:2375", "name": "a", "version": "29", "containers": []}
    b = {"host": "b:2375", "name": "b", "version": "29", "containers": [ctr("exited")]}
    res = {r.key: r for r in m.evaluate(s, th, (s, {"hosts": [a, b], "errors": {}}, "", T0))}
    assert res["docker.stopped"].status == ERROR
    res = {r.key: r for r in m.evaluate(s, th, (s, {"hosts": [a], "errors": {"b:2375": "unreachable"}}, "", T0 + 120))}
    assert res["docker.stopped"].status == ERROR and "b/nvr" in res["docker.stopped"].detail
    assert "host not read now" in res["docker.stopped"].detail
    b["containers"] = [ctr("running")]  # back, and running again
    res = {r.key: r for r in m.evaluate(s, th, (s, {"hosts": [a, b], "errors": {}}, "", T0 + 240))}
    assert res["docker.stopped"].status == OK


def test_unreadable_switch_keeps_its_down_port():
    from .test_mikrotik import dev, run
    m = MikroTikModule()
    res = run(m, {"devices": [dev("sw1"), dev("sw2")], "errors": {}}, ["sw2/ether2"])
    assert res["ports"].status == ERROR
    res = run(m, {"devices": [dev("sw1")], "errors": {"sw2": "timeout"}}, ["sw2/ether2"])
    assert res["ports"].status == ERROR and "sw2 ether2" in res["ports"].detail
    assert res["ports"].extra["nodes"] == ["port:sw2/ether2"]


# ---- B9. Reminders: each issue on its own timer ----------------------------------------------------------------
def test_each_issue_is_reminded_on_its_own_timer(db):
    eng = Engine(db, "home")
    for key, started in (("a.x", T0), ("b.y", T0 + 3600)):
        db.run("INSERT INTO issues(site,check_key,level,worst,started,notified) VALUES('home',?,2,2,?,1)", (key, started))
    assert [e.key for e in eng.reminders({}, T0 + 12 * 3600)] == ["a.x"]  # b.y is 11 h old: not yet
    assert [e.key for e in eng.reminders({}, T0 + 13 * 3600)] == ["b.y"]  # 12 h old: its first reminder
    assert eng.reminders({}, T0 + 20 * 3600) == []
    assert [e.key for e in eng.reminders({}, T0 + 24 * 3600)] == ["a.x"]  # 12 h after its last reminder
    assert [e.key for e in eng.reminders({}, T0 + 25 * 3600)] == ["b.y"]


# ---- B10. MQTT over TLS checks the broker before sending the login --------------------------------------------------
class FakeBroker:
    """A TLS listener on 127.0.0.1 in a thread; records what the client sends after the handshake."""

    def __init__(self, tmp_path, cert, key):
        self.path = tmp_path / f"broker{id(self)}.pem"
        self.path.write_text(cert + key)
        self.received = b""
        self.sock = socket.create_server(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(self.path)
        conn, _ = self.sock.accept()
        conn.settimeout(5)
        try:
            with ctx.wrap_socket(conn, server_side=True) as tls:
                self.received = tls.recv(1024)
        except (OSError, ssl.SSLError):
            pass
        self.sock.close()


def _bridge(db):
    return mqtt.Bridge(SimpleNamespace(db=db, secrets=Secrets(db, os.urandom(32)), cfg=SimpleNamespace(site="home")))


def _connect(ctx, port):
    """What paho does with the context: wrap, then the handshake, then the CONNECT packet with the login."""
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    tls = ctx.wrap_socket(sock, server_hostname="127.0.0.1", do_handshake_on_connect=False)
    try:
        tls.do_handshake()
        tls.sendall(b"CONNECT user password")
    finally:
        tls.close()


def test_mqtt_tls_pins_the_first_certificate_and_refuses_another(db, tmp_path):
    b = _bridge(db)
    s = mqtt.DEFAULTS | {"enabled": True, "host": "127.0.0.1", "tls": True}
    broker = FakeBroker(tmp_path, CERT_A, KEY_A)
    s["port"] = broker.port
    _connect(b.tls_context(s), broker.port)
    broker.thread.join(5)
    assert broker.received == b"CONNECT user password"
    hp = f"127.0.0.1:{broker.port}"
    assert mqtt.pins(db)[hp]["cert"].strip() == CERT_A.strip()

    other_cert, other_key = self_signed()
    impostor = FakeBroker(tmp_path, other_cert, other_key)
    db.set(mqtt.PINS, {f"127.0.0.1:{impostor.port}": mqtt.pins(db)[hp]})  # same broker address, new certificate
    s["port"] = impostor.port
    with pytest.raises(ssl.SSLCertVerificationError, match=CHANGED):
        _connect(b.tls_context(s), impostor.port)
    impostor.thread.join(5)
    assert impostor.received == b""  # no login was sent
    assert CHANGED in b.error
    pin = mqtt.pins(db)[f"127.0.0.1:{impostor.port}"]
    assert pin["new"].strip() == other_cert.strip()
    assert mqtt.trust_certificate(db, f"127.0.0.1:{impostor.port}", fingerprint(CERT_B), "admin")  # not what was shown
    assert mqtt.trust_certificate(db, f"127.0.0.1:{impostor.port}", fingerprint(pin["new"]), "admin") == ""
    assert mqtt.pins(db)[f"127.0.0.1:{impostor.port}"] == {"cert": pin["new"]}


def test_mqtt_tls_with_verify_uses_the_system_cas(db, tmp_path):
    b = _bridge(db)
    broker = FakeBroker(tmp_path, CERT_A, KEY_A)
    s = mqtt.DEFAULTS | {"enabled": True, "host": "127.0.0.1", "port": broker.port, "tls": True, "verify": True}
    ctx = b.tls_context(s)
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
    with pytest.raises(ssl.SSLCertVerificationError):  # self-signed: not trusted by the system
        _connect(ctx, broker.port)
    broker.thread.join(5)
    assert broker.received == b"" and mqtt.pins(db) == {}


def test_mqtt_apply_hands_paho_the_checking_context(db, monkeypatch):
    import paho.mqtt.client as paho
    got = {}
    monkeypatch.setattr(paho.Client, "tls_set_context", lambda self, ctx: got.setdefault("ctx", ctx))
    monkeypatch.setattr(paho.Client, "connect_async", lambda self, *a, **k: None)
    monkeypatch.setattr(paho.Client, "loop_start", lambda self: None)
    db.set(mqtt.SETTINGS, {"enabled": True, "host": "broker", "port": 8883, "tls": True})
    b = _bridge(db)
    b.apply()
    assert got["ctx"].sslsocket_class.__name__ == "Checked"
    assert mqtt.transport(db) == ("TLS, certificate not pinned yet", False)
    db.set(mqtt.PINS, {"broker:8883": {"cert": CERT_A}})
    assert mqtt.transport(db) == ("TLS, certificate pinned", True)
    db.set(mqtt.SETTINGS, {"enabled": True, "host": "broker", "port": 1883, "tls": False})
    assert mqtt.transport(db)[1] is False


def test_system_page_lists_the_mqtt_connection(client):
    _setup(client)
    client.rt.db.set(mqtt.SETTINGS, {"enabled": True, "host": "broker", "port": 1883, "tls": False})
    r = client.get("/system")
    assert "MQTT broker" in r.text and "Plain MQTT" in r.text


# ---- B11. Home Assistant: "dry" is off, except a climate entity's drying mode ---------------------------------------
def test_dry_state_values():
    from homewatch.modules.homeassistant.plugin import OFF, ON, entity_values
    assert not ON & OFF
    assert entity_values({"entity_id": "sensor.rain_state", "state": "dry"}) == {"value": 0.0}
    assert entity_values({"entity_id": "climate.living", "state": "dry"})["value"] == 1.0
