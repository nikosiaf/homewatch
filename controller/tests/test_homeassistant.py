"""Home Assistant: health checks, ticked entities (watch / show in Grafana / rules), reading, the 0.35 conversion."""

import asyncio
import time
from datetime import datetime, timezone

from homewatch.modules import ERROR, OK, WARNING, Thresholds
from homewatch.modules.homeassistant.plugin import HomeAssistantModule, plugin

NOW = time.time()


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def snap(**over):
    s = {"config": {"version": "2026.10.4", "state": "RUNNING", "safe_mode": False, "recovery_mode": False},
         "unavailable": [], "watched": [], "rules": "",
         "updates": [], "backup": {"last_successful_automatic_backup": iso(NOW - 20 * 3600),
                                   "last_attempted_automatic_backup": iso(NOW - 20 * 3600)},
         "_summary": "HA"}
    s.update(over)
    return s


def run(sn):
    s = {"enabled": True}
    th = {d.id: Thresholds(d.warn, d.error, d.default_on) for d in plugin.checks}
    return {r.key.split(".")[1]: r for r in plugin.evaluate(s, th, (s, sn, "", NOW))}


def w(eid, state, kind="", name="", dc="", unit=""):
    return {"entity_id": eid, "state": state, "kind": kind, "name": name, "dc": dc, "unit": unit}


def _ha(monkeypatch, states):
    import httpx
    calls = []

    def handler(request):
        path = request.url.path
        calls.append(path)
        if path == "/api/config":
            return httpx.Response(200, json={"version": "1", "state": "RUNNING"})
        if path == "/api/states":
            return httpx.Response(200, json=states)
        found = [e for e in states if path.endswith("/" + e["entity_id"])]
        return httpx.Response(200, json=found[0]) if found else httpx.Response(404)

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))

    async def no_registries(*a):
        return {}

    monkeypatch.setattr("homewatch.modules.homeassistant.plugin.registries", no_registries)
    return calls


# ---- checks ---------------------------------------------------------------------------------------

def test_health_only_when_nothing_is_watched():
    res = run(snap())
    assert set(res) == {"api", "core", "updates", "backups", "disk", "memory", "cpu"}  # the rest has nothing to check
    assert all(r.status == OK for r in res.values())


def test_updates_backups_and_safe_mode():
    res = run(snap(updates=[{"entity_id": "update.core", "title": "Core", "installed_version": "1",
                             "latest_version": "2", "skipped_version": None},
                            {"entity_id": "update.x", "title": "X", "installed_version": "1", "latest_version": "3",
                             "skipped_version": "3"}],
                   backup={"last_successful_automatic_backup": iso(NOW - 60 * 3600),
                           "last_attempted_automatic_backup": iso(NOW - 3 * 3600)},
                   config={"version": "2026.10.4", "state": "RUNNING", "safe_mode": True}))
    assert res["updates"].status == WARNING and "Core 1 → 2" in res["updates"].detail and "X" not in res["updates"].detail
    assert res["backups"].status == ERROR and "failed" in res["backups"].detail
    assert res["core"].status == WARNING
    assert run(snap(backup={}))["backups"].status == OK


def test_watched_unavailable_waits_30_minutes():
    ent = w("sensor.oil", "unavailable", name="Oil")
    fresh = snap(watched=[ent], unavailable=[{"entity_id": "sensor.oil", "name": "Oil", "last_changed": iso(NOW - 600)}])
    assert run(fresh)["unavailable"].status == OK
    old = snap(watched=[ent], unavailable=[{"entity_id": "sensor.oil", "name": "Oil", "last_changed": iso(NOW - 3600)}])
    res = run(old)
    assert res["unavailable"].status == WARNING and res["unavailable"].detail == "1 unavailable: Oil"
    assert run(snap(watched=[w("sensor.oil", "5")]))["unavailable"].detail == "1 watched, none unavailable"


def test_kinds_alarm_safety_tamper_batteries_addons():
    watched = [w("alarm_control_panel.home", "armed_away", "alarm", "Home"),
               w("binary_sensor.kitchen_smoke", "off", "safety", "Kitchen smoke", "smoke"),
               w("binary_sensor.panel_tamper", "off", "tamper", "Panel", "tamper"),
               w("sensor.door_battery", "40", "battery", "Door"), w("sensor.watch_battery", "-1", "battery", "Watch"),
               w("binary_sensor.z2m_running", "on", "addon", "Zigbee2MQTT Running", "running")]
    res = run(snap(watched=watched))
    assert all(res[c].status == OK for c in ("alarm", "safety", "tamper", "batteries", "addons"))
    assert res["batteries"].detail == "lowest Door 40 % (1 batteries)"  # -1: a device that doesn't report it
    watched[0]["state"], watched[1]["state"], watched[2]["state"] = "triggered", "on", "on"
    watched[3]["state"], watched[5]["state"] = "8", "off"
    res = run(snap(watched=watched))
    assert res["alarm"].status == ERROR and "TRIGGERED: Home" in res["alarm"].detail
    assert res["safety"].status == ERROR and "Kitchen smoke (smoke)" in res["safety"].detail
    assert res["tamper"].status == ERROR and res["batteries"].status == ERROR
    assert res["addons"].status == ERROR and res["addons"].detail == "not running: Zigbee2MQTT"
    only = run(snap(watched=[w("sensor.door_battery", "40", "battery")]))
    assert "batteries" in only and "alarm" not in only and "addons" not in only


def test_rules_on_watched_entities():
    ents = [w("sensor.oil", "150", name="Heating oil", unit="L"), w("sensor.panel_link", "unavailable"),
            w("binary_sensor.trouble", "off")]
    rules = "sensor.oil below 200 warning\nsensor.oil below 100 error\nsensor.panel_link is not online error\n" \
            "binary_sensor.trouble is on warning"
    r = run(snap(watched=ents, rules=rules))["rules"]
    assert r.status == ERROR and r.value == 2
    assert "Heating oil 150 L (below 200)" in r.detail and "panel_link unavailable (is not online)" in r.detail
    ents[0]["state"] = "abc"
    r = run(snap(watched=ents, rules="sensor.oil below 200 warning"))["rules"]
    assert r.status == WARNING and "no reading" in r.detail
    assert "rules" not in run(snap(watched=ents))  # no rules: not listed


def test_rule_lines_and_check_rule():
    from homewatch.modules.homeassistant.plugin import check_rule, rule_lines

    rules = {"sensor.oil": "below 200 warning; below 100 error", "sensor.x": "is on error"}
    assert rule_lines(rules, {"sensor.oil"}) == "sensor.oil below 200 warning\nsensor.oil below 100 error"
    assert check_rule("sensor.oil", "below 200 warning; above 9 error") == ""
    assert "Rule for sensor.oil" in check_rule("sensor.oil", "below lots warning")
    assert check_rule("sensor.oil", "under 5 warning")


def test_hidden_checks_follow_the_ticks():
    m = HomeAssistantModule()
    m._catalog = [{"id": "sensor.door_battery", "kind": "battery"}, {"id": "sensor.oil", "kind": ""}]
    assert m.hidden_checks({}) == {"unavailable", "silent", "alarm", "safety", "tamper", "batteries", "addons", "rules"}
    s = {"watch": ["sensor.door_battery", "sensor.oil"], "entity_rules": {"sensor.oil": "below 1 error"}}
    assert m.hidden_checks(s) == {"alarm", "safety", "tamper", "addons"}
    assert "rules" in m.hidden_checks({"watch": ["sensor.door_battery"], "entity_rules": {"sensor.oil": "below 1 error"}})


def test_disk_and_memory():
    res = run(snap(host={"disk": ["17.7", "%"], "memory": ["85.2", "%"]}))
    assert res["disk"].status == OK and res["disk"].value == 17.7
    assert res["memory"].status == WARNING and "85 %" in res["memory"].detail
    assert run(snap(host={"disk": ["95", "%"], "memory": None}))["disk"].status == ERROR
    res = run(snap(host={"disk": None, "memory": ["12", "GB"]}))
    assert res["disk"].status == OK and "no disk sensor" in res["disk"].detail
    assert res["memory"].status == WARNING and "not a percentage" in res["memory"].detail


def test_kind_and_sensor_fields():
    from homewatch.modules.homeassistant.plugin import _addon, _sensors, kind
    e = {"entity_id": "binary_sensor.samba_share_running", "attributes": {"device_class": "running"}}
    assert _addon(e) and kind(e) == "addon"
    pve = {"entity_id": "binary_sensor.node_pve_backup_running", "attributes": {  # Proxmox VE integration, not an add-on
        "running_since": None, "device_class": "running", "friendly_name": "Node pve Backup running"}}
    assert kind(pve) == ""
    assert kind({"entity_id": "sensor.b", "attributes": {"device_class": "battery", "unit_of_measurement": "%"}}) == "battery"
    assert kind({"entity_id": "binary_sensor.leak", "attributes": {"device_class": "moisture"}}) == "safety"
    assert _sensors({})["cpu"] == "sensor.system_monitor_processor_use"
    base = {"url": "http://ha.local:8123", "full_every": "30"}
    out, err = plugin.validate(base | {"disk_sensor": " sensor.my_disk ", "memory_sensor": ""}, {})
    assert not err and out["disk_sensor"] == "sensor.my_disk" and out["memory_sensor"] == ""
    assert "entity id" in plugin.validate(base | {"disk_sensor": "light.x"}, {})[1]
    out, _ = plugin.validate(base | {"_new": "1"}, {})
    assert out["watch"] == [] and out["grafana"] == [] and out["entities_v"] == 2  # a new install: nothing ticked


# ---- reading --------------------------------------------------------------------------------------

STATES = [
    {"entity_id": "sensor.oil", "state": "420", "attributes": {"friendly_name": "Oil", "unit_of_measurement": "L"}},
    {"entity_id": "sensor.hall_temperature", "state": "21.5",
     "attributes": {"friendly_name": "Hall", "unit_of_measurement": "°C", "device_class": "temperature"}},
    {"entity_id": "binary_sensor.kitchen_smoke", "state": "off", "attributes": {"device_class": "smoke"}},
    {"entity_id": "automation.lights", "state": "on", "attributes": {}},
    {"entity_id": "update.core", "state": "on", "attributes": {"title": "Core", "installed_version": "1",
                                                               "latest_version": "2", "supported_features": 23}},
    {"entity_id": "sensor.system_monitor_disk_usage", "state": "17", "attributes": {"unit_of_measurement": "%"}},
    # reports only (no INSTALL feature): Home Assistant keeps it off Settings → Updates, and so does homeWatch
    {"entity_id": "update.node_pve", "state": "on", "attributes": {"installed_version": "9.2", "latest_version": "9.2-p0-d1",
                                                                   "supported_features": 16}},
]


def test_nothing_ticked_reads_only_health(monkeypatch):
    calls = _ha(monkeypatch, [dict(e) for e in STATES])
    m = HomeAssistantModule()
    s = {"url": "http://ha", "verify": False, "entities_v": 2}
    first = asyncio.run(m.fetch(s, "t", NOW))
    assert "/api/states" in calls and first["watched"] == [] and first["stored"] == [] and first["recorded"] == []
    assert [c["id"] for c in first["catalog"]] == ["sensor.oil", "sensor.hall_temperature", "binary_sensor.kitchen_smoke",
                                                   "sensor.system_monitor_disk_usage"]  # automation / update hidden
    assert first["catalog"][2]["kind"] == "safety" and [u["entity_id"] for u in first["updates"]] == ["update.core"]
    assert first["host"]["disk"] == ["17", "%"]
    m.snap, m.snap_ts = first, NOW
    calls.clear()
    asyncio.run(m.fetch(s, "t", NOW + 120))
    assert "/api/states" not in calls and "/api/states/sensor.oil" not in calls
    assert "/api/states/sensor.system_monitor_disk_usage" in calls and "/api/states/update.core" in calls


def test_ticked_entities_are_read_stored_and_shown(monkeypatch):
    states = [dict(e) for e in STATES]
    calls = _ha(monkeypatch, states)
    m = HomeAssistantModule()
    s = {"url": "http://ha", "verify": False, "entities_v": 2, "watch": ["sensor.oil", "binary_sensor.kitchen_smoke"],
         "grafana": ["sensor.hall_temperature", "sensor.oil"], "entity_rules": {"sensor.oil": "below 500 warning",
                                                                              "sensor.hall_temperature": "above 1 error"},
         "full_every": 30}
    m.snap = asyncio.run(m.fetch(s, "t", NOW))
    m.snap_ts = NOW
    assert [e["entity_id"] for e in m.snap["watched"]] == ["sensor.oil", "binary_sensor.kitchen_smoke"]
    assert m.snap["rules"] == "sensor.oil below 500 warning"  # a rule on an unwatched entity is kept, not checked
    assert sorted(e["entity_id"] for e in m.snap["stored"]) == ["binary_sensor.kitchen_smoke", "sensor.hall_temperature",
                                                               "sensor.oil"]
    assert [e["entity_id"] for e in m.snap["recorded"]] == ["sensor.oil", "sensor.hall_temperature"]
    out = m.series("home")
    assert len(out) == 3 and all("name=" not in x and "unit=" not in x for x in out)
    assert any("entity_id=sensor.oil" in x and "domain=sensor" in x and " value=420" in x for x in out)
    states[0]["state"] = "unavailable"
    states[4]["state"] = "off"  # the update got installed
    calls.clear()
    second = asyncio.run(m.fetch(s, "t", NOW + 120))
    assert "/api/states" not in calls and "/api/states/sensor.oil" in calls
    assert second["unavailable"][0]["entity_id"] == "sensor.oil" and second["updates"] == []
    calls.clear()
    asyncio.run(m.fetch(s | {"full_every": 5}, "t", NOW + 400))
    assert "/api/states" in calls  # the full read follows the setting


# ---- 0.35 settings turned into ticks --------------------------------------------------------------

def test_convert_old_settings():
    from homewatch.modules.homeassistant.plugin import convert
    states = [
        {"entity_id": "sensor.phone_battery", "state": "50", "attributes": {"device_class": "battery", "unit_of_measurement": "%"}},
        {"entity_id": "sensor.door_battery", "state": "80", "attributes": {"device_class": "battery", "unit_of_measurement": "%"}},
        {"entity_id": "binary_sensor.kitchen_smoke", "state": "off", "attributes": {"device_class": "smoke"}},
        {"entity_id": "sensor.hall_temperature", "state": "21", "attributes": {}},
        {"entity_id": "binary_sensor.panel_trouble_ac", "state": "off", "attributes": {}},
        {"entity_id": "binary_sensor.panel_trouble_bell", "state": "off", "attributes": {}},
        {"entity_id": "sensor.oil", "state": "400", "attributes": {}},
        {"entity_id": "light.old", "state": "unavailable", "attributes": {}},
        {"entity_id": "automation.x", "state": "on", "attributes": {}},
        {"entity_id": "sensor.phone_rx", "state": "5", "attributes": {}},
    ]
    old = {"skipped": ["sensor.phone_battery"], "chosen": ["light.old"], "watch": ["sensor.oil"],
           "record": "# temps\nsensor.*_temperature\nautomation.*",
           "rules": "binary_sensor.panel_trouble_* is on warning\nsensor.oil below 100 error\nsensor.gone below 1 error"}
    new = convert(old, states)
    assert new["watch"] == ["binary_sensor.kitchen_smoke", "binary_sensor.panel_trouble_ac",
                            "binary_sensor.panel_trouble_bell", "sensor.door_battery", "sensor.oil"]
    assert new["grafana"] == ["binary_sensor.kitchen_smoke", "sensor.door_battery", "sensor.hall_temperature",
                              "sensor.phone_battery"]  # always recorded before; automations aren't listed now
    assert new["entity_rules"] == {"binary_sensor.panel_trouble_ac": "is on warning",
                                   "binary_sensor.panel_trouble_bell": "is on warning", "sensor.oil": "below 100 error"}
    every = convert({"watch_all": True, "chosen": ["light.old"], "skipped": ["group:sensor"]}, states)
    assert every["watch"] == ["binary_sensor.kitchen_smoke", "binary_sensor.panel_trouble_ac",
                              "binary_sensor.panel_trouble_bell"]  # light.old ignored, sensors skipped as a domain
    every = convert({"watch_all": True}, states)
    assert "sensor.phone_rx" not in every["watch"] and "sensor.oil" in every["watch"]  # RX/TX were never reported


def test_conversion_is_saved_once(monkeypatch, cfg):
    from dataclasses import replace

    from homewatch.runtime import Runtime

    _ha(monkeypatch, [dict(e) for e in STATES])
    rt = Runtime.create(replace(cfg, grafana_dir=""))
    rt.db.set("module.homeassistant", {"url": "http://ha", "verify": False, "enabled": True, "chosen": [],
                                        "skipped": [], "record": "sensor.*_temperature", "watch_all": False,
                                        "rules": "sensor.oil below 100 error"})
    m = HomeAssistantModule()
    asyncio.run(m.readings(rt, m.load(rt), NOW))
    s = rt.db.get("module.homeassistant")
    assert s["entities_v"] == 2 and not {"record", "watch_all", "skipped", "chosen", "rules"} & set(s)
    assert s["watch"] == ["binary_sensor.kitchen_smoke", "sensor.oil"]
    assert s["grafana"] == ["binary_sensor.kitchen_smoke", "sensor.hall_temperature"]
    assert s["entity_rules"] == {"sensor.oil": "below 100 error"}
    assert [e["entity_id"] for e in m.snap["watched"]] == ["sensor.oil", "binary_sensor.kitchen_smoke"]
    assert rt.db.one("SELECT detail FROM audit WHERE action=?", ("homeassistant",))["detail"].startswith("entities converted")
    v = rt.db.one("SELECT username, summary FROM config_versions ORDER BY id DESC LIMIT 1")
    assert v["username"] == "system" and "Home Assistant" in v["summary"]  # a version: Restore undoes it
    m.reread()
    asyncio.run(m.readings(rt, m.load(rt), NOW + 120))
    assert rt.db.get("module.homeassistant") == s  # once


def test_registries_give_integration_device_and_area(monkeypatch):
    import json

    from homewatch.modules.homeassistant import plugin as ha

    answers = {
        "config/entity_registry/list_for_display": {"entities": [
            {"ei": "sensor.door_battery", "pl": "esphome", "di": "d1"},
            {"ei": "sensor.oil", "pl": "garmin_connect", "di": "d2", "ai": "kitchen"},
            {"ei": "input_boolean.x", "pl": "input_boolean"}]},
        "config/device_registry/list": [{"id": "d1", "name": "sh_basement_door", "area_id": "basement"},
                                        {"id": "d2", "name": "Tank", "name_by_user": "Oil tank", "area_id": None}],
        "config/area_registry/list": [{"area_id": "basement", "name": "Basement"}, {"area_id": "kitchen", "name": "Kitchen"}]}
    seen = {}

    class FakeWS:
        def __init__(self):
            self.out = [json.dumps({"type": "auth_required"})]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def recv(self):
            return self.out.pop(0)

        async def send(self, text):
            msg = json.loads(text)
            if msg["type"] == "auth":
                seen["token"] = msg["access_token"]
                self.out.append(json.dumps({"type": "auth_ok"}))
            else:
                self.out.append(json.dumps({"id": msg["id"], "type": "result", "success": True, "result": answers[msg["type"]]}))

    def fake_connect(url, **kw):
        seen["url"], seen["ssl"] = url, kw.get("ssl")
        return FakeWS()

    monkeypatch.setattr("websockets.asyncio.client.connect", fake_connect)
    reg = asyncio.run(ha.registries("https://ha.example:8123", "tok", True))
    assert seen["url"] == "wss://ha.example:8123/api/websocket" and seen["token"] == "tok" and seen["ssl"] is not None
    assert reg["sensor.door_battery"] == {"integration": "ESPHome", "device": "sh_basement_door", "area": "Basement"}
    assert reg["sensor.oil"] == {"integration": "Garmin connect", "device": "Oil tank", "area": "Kitchen"}
    assert reg["input_boolean.x"] == {"integration": "Input boolean", "device": "", "area": ""}
    asyncio.run(ha.registries("http://ha.local:8123", "tok", True))
    assert seen["url"] == "ws://ha.local:8123/api/websocket" and seen["ssl"] is None
    row = ha.catalog_row({"entity_id": "sensor.door_battery", "state": "80",
                          "attributes": {"device_class": "battery", "unit_of_measurement": "%"}}, reg)
    assert row["integration"] == "ESPHome" and row["device"] == "sh_basement_door" and row["first"] is True


def test_registry_failure_falls_back_to_domains(monkeypatch):
    calls = _ha(monkeypatch, [dict(e) for e in STATES])

    async def broken(*a):
        raise OSError("no websockets through the proxy")

    monkeypatch.setattr("homewatch.modules.homeassistant.plugin.registries", broken)
    m = HomeAssistantModule()
    first = asyncio.run(m.fetch({"url": "http://ha", "verify": False, "entities_v": 2}, "t", NOW))
    assert "/api/states" in calls and len(first["catalog"]) == 4 and first["catalog"][0]["integration"] == ""
    assert m._registry_error == "OSError"
