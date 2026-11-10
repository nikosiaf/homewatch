"""Silence (0.41): learn how often watched Home Assistant entities report, name the ones gone quiet, and an
integration whose entities all stopped together (a bridge or coordinator gone while HA keeps the last values)."""

import time

from homewatch.modules import ERROR, OK, WARNING, CheckResult, Thresholds
from homewatch.modules.homeassistant.plugin import LEARN_DAYS, learn, plugin, silent_check
from tests.test_web import client  # noqa: F401 - the fixture

DAY = 86400
T0 = 1_800_000_000.0


def ent(eid, lr, integ="ESPHome", state="20", name=""):
    return {"entity_id": eid, "state": state, "name": name or eid, "integ": integ, "lr": lr}


def feed(db, eids, every, start, until, integ="ESPHome"):
    """Every entity reports each `every` seconds; homeWatch reads every 2 minutes."""
    t, out = start, {}
    while t <= until:
        last = t - (t - start) % every
        out = learn(db, "home", [ent(e, last, integ) for e in eids], t)
        t += 120
    return out


def test_learning_takes_seven_days_and_twenty_gaps(db):
    seen = feed(db, ["sensor.a"], 600, T0, T0 + DAY)
    assert not seen["sensor.a"]["learned"] and seen["sensor.a"]["usual"] == 600
    w = [ent("sensor.a", T0 + DAY)]
    assert silent_check(w, seen, Thresholds(1, None))[1].startswith("learning how often")
    seen = feed(db, ["sensor.a"], 600, T0 + DAY, T0 + LEARN_DAYS * DAY + 600)
    assert seen["sensor.a"]["learned"]


def test_a_silent_entity_is_named_with_its_usual_gap(db):
    feed(db, ["sensor.a", "sensor.b"], 600, T0, T0 + 8 * DAY)
    last = T0 + 8 * DAY - (8 * DAY) % 600
    now = last + 2 * 3600  # sensor.a quiet for 2 h; sensor.b still reports
    seen = learn(db, "home", [ent("sensor.a", last, "A"), ent("sensor.b", now - 60, "B")], now)
    w = [ent("sensor.a", last, "A", name="Oil level"), ent("sensor.b", now - 60, "B")]
    status, detail, n = silent_check(w, seen, Thresholds(1, None))
    assert status == WARNING and n == 1 and detail == "1 silent: Oil level (2 h, usually within 10 min)"
    # within an hour it is never silent, whatever its usual gap
    seen = learn(db, "home", [ent("sensor.a", last, "A"), ent("sensor.b", last + 1800, "B")], last + 1800)
    assert silent_check(w, seen, Thresholds(1, None))[0] == OK


def test_unavailable_entities_are_left_to_the_unavailable_check(db):
    feed(db, ["sensor.a"], 600, T0, T0 + 8 * DAY)
    now = T0 + 9 * DAY
    seen = learn(db, "home", [ent("sensor.a", T0 + 8 * DAY)], now)
    assert silent_check([ent("sensor.a", T0 + 8 * DAY, state="unavailable")], seen, Thresholds(1, None))[0] == OK


def test_an_integration_that_stops_is_an_error(db):
    doors = ["binary_sensor.front", "binary_sensor.back", "binary_sensor.side"]
    feed(db, doors, 3600, T0, T0 + 8 * DAY, integ="Zigbee2MQTT")
    last = T0 + 8 * DAY - (8 * DAY) % 3600
    now = last + 2.5 * 3600  # 2.5 h > twice the usual hour, but < 3 times: the group sees it before each entity
    seen = learn(db, "home", [ent(d, last, "Zigbee2MQTT") for d in doors], now)
    w = [ent(d, last, "Zigbee2MQTT") for d in doors]
    status, detail, n = silent_check(w, seen, Thresholds(1, None))
    assert status == ERROR and n == 3 and detail == "Zigbee2MQTT: 3 of 3 watched entities silent, longest 2.5 h"


def test_entities_no_longer_watched_are_forgotten(db):
    feed(db, ["sensor.a", "sensor.b"], 600, T0, T0 + 3600)
    learn(db, "home", [ent("sensor.a", T0 + 3600)], T0 + 3700)
    assert [r["entity_id"] for r in db.all("SELECT entity_id FROM entity_seen")] == ["sensor.a"]


def test_the_check_runs_with_the_module_and_hides_without_watched_entities():
    from tests.test_homeassistant import NOW, snap, w

    thr = {d.id: Thresholds(d.warn, d.error, d.default_on) for d in plugin.checks}
    plugin._seen = {"sensor.oil": {"age": 5 * 3600, "usual": 300, "learned": True},
                    "sensor.temp": {"age": 60, "usual": 300, "learned": True}}
    try:
        ents = [w("sensor.oil", "150", name="Oil") | {"integ": "ESPHome"},
                w("sensor.temp", "21", name="Temp") | {"integ": "Shelly"}]
        r = {x.key.split(".")[1]: x for x in plugin.check({}, thr, snap(watched=ents), NOW)}["silent"]
        assert r.status == WARNING and r.detail == "1 silent: Oil (5 h, usually within 5 min)"
    finally:
        plugin._seen = {}
    assert "silent" in plugin.hidden_checks({})


def test_stale_module_tile(client):  # noqa: F811
    from tests.test_web import setup_admin

    setup_admin(client)
    rt = client.rt
    now = time.time()
    rt.results = [CheckResult("internet.reachable", "Internet reachable", OK, "2 of 2 targets answer")]
    rt.db.run("INSERT INTO coverage(site,module,start,end) VALUES('home','internet',?,?)", (now - 7200, now - 3600))
    assert "No new reading for 1 h" in client.get("/").text
    rt.db.run("UPDATE coverage SET end=?", (now - 60,))
    assert "No new reading for" not in client.get("/").text
