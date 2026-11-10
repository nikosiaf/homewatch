"""0.44: buttons in notifications (one-time links), acknowledged problems, runbooks, MQTT to Home
Assistant."""

import json
import time

import httpx

from homewatch import actions, runbooks
from homewatch.modules.registry import MODULES
from tests.test_web import client, csrf, setup_admin  # noqa: F401 - the fixture

NOW = 1_800_000_000.0


def open_issue(db, key="internet.loss", **over):
    cols = {"site": "home", "check_key": key, "level": 1, "worst": 1, "started": NOW - 600, "detail": "x", "notified": 1,
            "name": "Packet loss"} | over
    return db.run(f"INSERT INTO issues({','.join(cols)}) VALUES({','.join('?' * len(cols))})", tuple(cols.values()))


# ---- one-time links --------------------------------------------------------------------------------------
def test_links_snooze_and_ack_once(db):
    iid = open_issue(db)
    assert actions.links(db, "home", "", "internet.loss", NOW) == []  # no address set: no buttons
    snooze, ack = actions.links(db, "home", "https://hw.example/", "internet.loss", NOW)
    assert snooze["label"] == "Snooze 1 day" and snooze["url"].startswith("https://hw.example/act/")
    tok = snooze["url"].rsplit("/", 1)[1]
    assert db.one("SELECT COUNT(*) n FROM action_tokens WHERE token_hash=?", (tok,))["n"] == 0  # only its hash is kept
    done, text = actions.use(db, "home", tok, NOW + 60)
    assert done and "Snoozed internet.loss" in text
    assert db.one("SELECT until FROM snoozes")["until"] == NOW + 60 + 86400
    assert actions.use(db, "home", tok, NOW + 61) == (False, "This link was already used.")
    done, _ = actions.use(db, "home", ack["url"].rsplit("/", 1)[1], NOW + 62)
    assert done and db.one("SELECT acked FROM issues WHERE id=?", (iid,))["acked"] == NOW + 62
    late = actions.links(db, "home", "https://hw.example", "internet.loss", NOW)[0]["url"].rsplit("/", 1)[1]
    assert actions.use(db, "home", late, NOW + 2 * 86400)[0] is False  # expired
    assert actions.use(db, "home", "made-up", NOW)[0] is False


def test_acknowledged_problems_get_no_reminders(db):
    from homewatch.engine import Engine

    open_issue(db, started=NOW - 13 * 3600)
    open_issue(db, key="internet.latency", started=NOW - 13 * 3600, acked=NOW - 3600)
    evs = Engine(db, "home").reminders({}, NOW)
    assert [e.key for e in evs] == ["internet.loss"]


def test_act_needs_no_login_and_works_once(client):  # noqa: F811
    setup_admin(client)
    rt = client.rt
    open_issue(rt.db)
    url = actions.links(rt.db, "home", "https://hw.example", "internet.loss", time.time())[0]["url"]
    path = "/act/" + url.rsplit("/", 1)[1]
    client.cookies.clear()
    assert "Yes, do it" in client.get(path).text
    r = client.post(path)
    assert r.status_code == 200 and "Snoozed" in r.text
    assert client.post(path).status_code == 410


# ---- notifications ---------------------------------------------------------------------------------------
def test_ntfy_gets_buttons_and_the_message_what_to_do(db, respx_mock):
    import asyncio

    from homewatch.engine import Event
    from homewatch.notify import Notifier, ntfy
    from homewatch.secrets import Secrets

    db.set("home.url", "https://hw.example")
    db.run("INSERT INTO channels(kind,name,levels,config) VALUES('ntfy','n','[1,2]','{\"server\":\"https://ntfy.test\"}')")
    sec = Secrets(db, b"k" * 32)
    from homewatch.notify import channel_private, store_private
    store_private(sec, 1, {"url": "https://ntfy.test/t", "topic": "t"})
    open_issue(db, key="internet.reachable", level=2, worst=2)
    n = Notifier(db, sec)
    n.dispatch([Event("open", "internet.reachable", "Internet reachable", 2, 2, "none of 1.1.1.1 answers", NOW)], (1, 0), NOW)
    row = db.one("SELECT body, actions FROM outbox")
    assert "👉 Look at the modem's lights" in row["body"]
    acts = json.loads(row["actions"])
    assert [a["label"] for a in acts] == ["Snooze 1 day", "Acknowledge", "Open homeWatch"]
    seen = {}

    def post(request):
        seen["actions"] = request.headers.get("actions")
        return httpx.Response(200)

    respx_mock.post("https://ntfy.test/t").mock(side_effect=post)
    assert asyncio.run(n.send_due(NOW + 1)) == 1
    assert seen["actions"].startswith("http, Snooze 1 day, https://hw.example/act/")
    assert "method=POST, clear=true" in seen["actions"] and seen["actions"].endswith("view, Open homeWatch, https://hw.example")
    assert channel_private  # imported for the store above


def test_runbook_defaults_name_real_checks_and_can_be_changed(client):  # noqa: F811
    keys = {f"{m.id}.{c.id}" for m in MODULES.values() for c in m.checks}
    assert set(runbooks.DEFAULTS) <= keys, set(runbooks.DEFAULTS) - keys
    tok = setup_admin(client)
    rt = client.rt
    page = client.get("/modules/internet").text
    assert 'name="internet.reachable.runbook"' in page and "Look at the modem" in page
    form = {"csrf": tok, "internet.reachable.enabled": "on", "internet.reachable.runbook": "Call Nikos  first."}
    client.post("/modules/internet/thresholds", data=form)
    assert runbooks.get(rt.db, "home", "internet.reachable") == "Call Nikos first."
    form["internet.reachable.runbook"] = runbooks.DEFAULTS["internet.reachable"]
    client.post("/modules/internet/thresholds", data=form)  # back to the built-in text: nothing stored
    assert rt.db.one("SELECT runbook FROM checks WHERE check_key='internet.reachable'")["runbook"] is None


# ---- MQTT to Home Assistant ------------------------------------------------------------------------------
class FakeClient:
    def __init__(self):
        self.out = []

    def publish(self, topic, payload, retain=False):
        self.out.append((topic, payload, retain))


def test_mqtt_discovery_and_state(cfg):
    from homewatch.engine import Event
    from homewatch.mqtt import Bridge
    from homewatch.modules import CheckResult, WARNING
    from homewatch.runtime import Runtime

    rt = Runtime.create(cfg)
    rt.results = [CheckResult("internet.loss", "Packet loss", WARNING, "30 %")]
    rt.db.run("INSERT INTO check_state(site,check_key,status) VALUES('home','internet.loss',1)")
    b = Bridge(rt)
    b.client, b.connected = FakeClient(), True
    b.publish_state()
    topics = {t: (json.loads(p), r) for t, p, r in b.client.out}
    cfg_ = topics["homeassistant/binary_sensor/homewatch_home/internet/config"][0]
    assert cfg_["device_class"] == "problem" and cfg_["device"]["identifiers"] == ["homewatch_home"]
    assert "homeassistant/button/homewatch_home/check_now/config" in topics
    state, retained = topics["homewatch/home/state"]
    assert retained and state["level_word"] == "WARNING" and state["module_levels"] == {"internet": 1}
    n = len(b.client.out)
    b.publish_state()
    assert len(b.client.out) == n + 1  # discovery only once; then just the state
    b.publish_events([Event("open", "internet.loss", "Packet loss", 1, 1, "30 %", NOW),
                      Event("remind", "internet.loss", "Packet loss", 1, 1, "30 %", NOW)])
    t, p, r = b.client.out[-1]
    assert t == "homewatch/home/event" and json.loads(p)["event_type"] == "problem" and not r


def test_mqtt_settings_page(client):  # noqa: F811
    tok = setup_admin(client)
    rt = client.rt
    rt.mqtt = type("M", (), {"connected": False, "error": "", "apply": lambda self: None})()
    page = client.get("/system").text
    assert "Home Assistant (MQTT)" in page
    r = client.post("/system/mqtt", data={"csrf": tok, "enabled": "on", "host": "bad host!", "port": "1883"})
    assert "MQTT broker: a host name" in r.text
    client.post("/system/mqtt", data={"csrf": tok, "enabled": "on", "host": "192.168.2.251", "port": "1883",
                                      "username": "homewatch", "password": "pw", "prefix": "homeassistant"})
    from homewatch import mqtt
    assert mqtt.load(rt.db)["host"] == "192.168.2.251" and rt.secrets.get(mqtt.SECRET) == "pw"
    assert "pw" not in json.dumps(rt.db.get(mqtt.SETTINGS))
