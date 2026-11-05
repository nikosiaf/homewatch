"""Home Assistant checks: core state, newly unavailable entities, updates, backups."""

import time
from datetime import datetime, timezone

from homewatch.modules import ERROR, OK, WARNING, Thresholds
from homewatch.modules.homeassistant.plugin import plugin

NOW = time.time()


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def snap(**over):
    s = {"config": {"version": "2026.10.4", "state": "RUNNING", "safe_mode": False, "recovery_mode": False},
         "unavailable": [{"entity_id": "light.old", "state": "unavailable", "last_changed": iso(NOW - 9 * 86400),
                          "name": "Old light"}],
         "updates": [], "backup": {"last_successful_automatic_backup": iso(NOW - 20 * 3600),
                                   "last_attempted_automatic_backup": iso(NOW - 20 * 3600)},
         "_summary": "HA"}
    s.update(over)
    return s


def run(sn, ignored=("light.old",)):
    s = {"enabled": True, "chosen": list(ignored)}
    th = {d.id: Thresholds(d.warn, d.error, d.default_on) for d in plugin.checks}
    return {r.key.split(".")[1]: r for r in plugin.evaluate(s, th, (s, sn, "", NOW))}


def test_healthy_with_ignored_entity():
    res = run(snap())
    assert all(r.status == OK for r in res.values()) and "ignoring 1" in res["unavailable"].detail


def test_new_unavailable_waits_30_minutes():
    fresh = {"entity_id": "sensor.new", "state": "unavailable", "last_changed": iso(NOW - 600), "name": "New"}
    assert run(snap(unavailable=[fresh]))["unavailable"].status == OK
    old = fresh | {"last_changed": iso(NOW - 3600)}
    res = run(snap(unavailable=[old]))
    assert res["unavailable"].status == WARNING and "New" in res["unavailable"].detail


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


def test_no_backup_sensors():
    assert run(snap(backup={}))["backups"].status == OK


def test_unwatched_entity_or_domain_is_not_reported():
    old = {"entity_id": "sensor.gone", "state": "unavailable", "last_changed": iso(NOW - 3600), "name": "Gone"}
    sn = snap(unavailable=[old])
    th = {d.id: Thresholds(d.warn, d.error, d.default_on) for d in plugin.checks}
    for skipped in (["sensor.gone"], ["group:sensor"]):
        s = {"enabled": True, "chosen": [], "skipped": skipped}
        res = {r.key.split(".")[1]: r for r in plugin.evaluate(s, th, (s, sn, "", NOW))}
        assert res["unavailable"].status == OK and "ignoring 1" in res["unavailable"].detail
    s = {"enabled": True, "chosen": [], "skipped": ["sensor.other"]}
    assert plugin.evaluate(s, th, (s, sn, "", NOW))[2].status == WARNING


def test_choices_and_watch_list_grouped_by_domain():
    sn = snap(unavailable=[{"entity_id": "light.old", "name": "Old light"}, {"entity_id": "sensor.x", "name": ""}],
              all=[["light.old", "Old light"], ["sensor.x", ""]])
    assert [(c.group, c.label, c.detail) for c in plugin.choices(sn)] == [
        ("light", "Old light", "light.old"), ("sensor", "x", "")]
    assert [(c.group, c.value, c.label) for c in plugin.filters(sn)] == [
        ("light", "light.old", "Old light"), ("sensor", "sensor.x", "x")]


def test_alarm_safety_tamper_and_batteries():
    watch = [{"entity_id": "alarm_control_panel.home", "state": "armed_home", "dc": None, "name": "Home"},
             {"entity_id": "binary_sensor.kitchen_smoke", "state": "off", "dc": "smoke", "name": "Kitchen smoke"},
             {"entity_id": "binary_sensor.boiler_leak", "state": "off", "dc": "moisture", "name": "Boiler leak"},
             {"entity_id": "binary_sensor.zone1_tamper", "state": "off", "dc": "tamper", "name": "Zone 1 tamper"},
             {"entity_id": "sensor.door_battery", "state": "60", "dc": "battery", "name": "Door"},
             {"entity_id": "sensor.phone_battery", "state": "5", "dc": "battery", "name": "Phone"},
             {"entity_id": "sensor.watch_battery", "state": "-1", "dc": "battery", "name": "Watch"}]
    sn = snap(watch=watch)
    th = {d.id: Thresholds(d.warn, d.error, d.default_on) for d in plugin.checks}

    def res(s_over=None, w=watch):
        s = {"enabled": True, "chosen": ["light.old"], "skipped": []} | (s_over or {})
        return {r.key.split(".")[1]: r for r in plugin.evaluate(s, th, (s, snap(watch=w), "", NOW))}

    r = res()
    assert r["alarm"].status == OK and "Home armed home" in r["alarm"].detail
    assert r["safety"].status == OK and "2 safety sensors clear" in r["safety"].detail
    assert r["batteries"].status == ERROR and "Phone 5 % (2 batteries)" in r["batteries"].detail
    # unticking the phone in "Entities to watch" leaves the door battery
    assert res({"skipped": ["sensor.phone_battery"]})["batteries"].status == OK
    hot = [w | {"state": "on"} if w["dc"] in ("moisture", "tamper") else w for w in watch]
    hot[0] = hot[0] | {"state": "triggered"}
    r = res(w=hot)
    assert r["alarm"].status == ERROR and r["safety"].status == ERROR and "Boiler leak (moisture)" in r["safety"].detail
    assert r["tamper"].status == ERROR
    assert res(w=[watch[0] | {"state": "unavailable"}])["alarm"].status == WARNING
    assert sn["watch"] == watch


def test_rx_tx_sensors_are_never_reported():
    gone = {"entity_id": "sensor.phone_rx", "state": "unavailable", "last_changed": iso(NOW - 3600), "name": "Phone RX"}
    assert run(snap(unavailable=[gone]))["unavailable"].status == OK


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
    return calls


def test_only_essential_entities_are_read_after_discovery(monkeypatch):
    import asyncio

    from homewatch.modules.homeassistant.plugin import HomeAssistantModule
    door = {"entity_id": "sensor.door_battery", "state": "80",
            "attributes": {"device_class": "battery", "unit_of_measurement": "%"}}
    light = {"entity_id": "light.a", "state": "unavailable"}
    rx = {"entity_id": "sensor.phone_rx", "state": "unavailable"}
    upd = {"entity_id": "update.core", "state": "on", "attributes": {"latest_version": "2"}}
    calls = _ha(monkeypatch, [door, light, rx, upd])
    m = HomeAssistantModule()
    s = {"url": "http://ha", "verify": False}
    first = asyncio.run(m.fetch(s, "t", NOW))
    assert "/api/states" in calls and first["unavailable"] == [] and first["updates"][0]["latest_version"] == "2"
    assert [a[0] for a in first["all"]] == ["sensor.door_battery"] and first["entities"] == 4
    calls.clear()
    m.snap_ts = NOW
    second = asyncio.run(m.fetch(s, "t", NOW + 120))
    assert "/api/states" not in calls and "/api/states/sensor.door_battery" in calls
    assert len(second["watch"]) == 1 and second["updates"] == first["updates"]
    calls.clear()
    asyncio.run(m.fetch(s, "t", NOW + 7 * 3600))
    assert "/api/states" in calls


def test_installed_update_clears_before_the_next_full_read(monkeypatch):
    import asyncio

    from homewatch.modules.homeassistant.plugin import HomeAssistantModule
    a = {"entity_id": "update.a", "state": "on", "attributes": {"title": "A", "latest_version": "2"}}
    b = {"entity_id": "update.b", "state": "on", "attributes": {"title": "B", "latest_version": "5"}}
    calls = _ha(monkeypatch, [a, b])
    m = HomeAssistantModule()
    s = {"url": "http://ha", "verify": False}

    def read(minutes):
        m.snap_ts = NOW
        calls.clear()
        return [u["entity_id"] for u in asyncio.run(m.fetch(s, "t", NOW + minutes * 60))["updates"]]

    assert read(0) == ["update.a", "update.b"]
    a["state"] = "unavailable"  # Home Assistant restarting installs nothing
    assert read(2) == ["update.a", "update.b"] and "/api/states" not in calls
    a["state"] = "off"
    assert read(4) == ["update.b"] and "/api/states" not in calls
    b["attributes"]["latest_version"] = "6"
    m.snap_ts = NOW
    assert asyncio.run(m.fetch(s, "t", NOW + 360))["updates"][0]["latest_version"] == "6"


def test_watch_every_entity_keeps_the_full_list_without_rx_tx(monkeypatch):
    import asyncio

    from homewatch.modules.homeassistant.plugin import HomeAssistantModule
    _ha(monkeypatch, [{"entity_id": "light.a", "state": "unavailable"}, {"entity_id": "sensor.phone_tx", "state": "1"}])
    snap_all = asyncio.run(HomeAssistantModule().fetch({"url": "http://ha", "watch_all": True}, "t", NOW))
    assert [a[0] for a in snap_all["all"]] == ["light.a"] and len(snap_all["unavailable"]) == 1


def test_full_read_interval_is_a_setting(monkeypatch):
    import asyncio

    from homewatch.modules.homeassistant.plugin import HomeAssistantModule
    calls = _ha(monkeypatch, [{"entity_id": "update.a", "state": "off"}])
    m = HomeAssistantModule()

    def full_read(minutes, **settings):
        m.snap_ts = NOW
        calls.clear()
        asyncio.run(m.fetch({"url": "http://ha", "verify": False} | settings, "t", NOW + minutes * 60))
        return "/api/states" in calls

    assert full_read(0)  # first read
    assert not full_read(29) and full_read(31)  # settings saved before the field existed: 30 minutes
    assert not full_read(40, full_every=10) and full_read(41.1, full_every=10)

    form = {"url": "http://ha", "enabled": "on"}
    assert m.validate(form, {})[0]["full_every"] == 30
    assert m.validate(form | {"full_every": "60"}, {})[0]["full_every"] == 60
    for bad in ("4", "1441", "x"):
        assert m.validate(form | {"full_every": bad}, {})[1]


def addon(eid, state, name):
    return {"entity_id": eid, "state": state, "dc": "running", "name": name}


def test_addons_running_stopped_and_unavailable():
    ok = [addon("binary_sensor.mosquitto_broker_running", "on", "Mosquitto broker Running")]
    res = run(snap(watch=ok))
    assert res["addons"].status == OK and "1 watched" in res["addons"].detail
    stopped = ok + [addon("binary_sensor.zigbee2mqtt_running", "off", "Zigbee2MQTT Running")]
    res = run(snap(watch=stopped))
    assert res["addons"].status == ERROR and res["addons"].detail == "not running: Zigbee2MQTT"
    gone = ok + [addon("binary_sensor.zigbee2mqtt_running", "unavailable", "Zigbee2MQTT Running")]
    assert run(snap(watch=gone))["addons"].status == WARNING


def test_addons_unticked_and_other_running_sensors_ignored():
    w = [addon("binary_sensor.node_red_running", "off", "Node-RED Running"),
         addon("binary_sensor.qemu_win11_200_status", "off", "QEMU Win11 Status"),  # Proxmox VM, not an add-on
         addon("binary_sensor.brew_switch", "off", "Brew Switch")]
    s = {"enabled": True, "chosen": ["light.old"], "skipped": ["binary_sensor.node_red_running"]}
    th = {d.id: Thresholds(d.warn, d.error, d.default_on) for d in plugin.checks}
    res = {r.key.split(".")[1]: r for r in plugin.evaluate(s, th, (s, snap(watch=w), "", NOW))}
    assert res["addons"].status == OK and "no add-ons watched" in res["addons"].detail


def test_disk_and_memory():
    res = run(snap(host={"disk": ["17.7", "%"], "memory": ["85.2", "%"]}))
    assert res["disk"].status == OK and res["disk"].value == 17.7
    assert res["memory"].status == WARNING and "85 %" in res["memory"].detail
    assert run(snap(host={"disk": ["95", "%"], "memory": None}))["disk"].status == ERROR
    res = run(snap(host={"disk": None, "memory": ["12", "GB"]}))
    assert res["disk"].status == OK and "no disk sensor" in res["disk"].detail
    assert res["memory"].status == WARNING and "not a percentage" in res["memory"].detail
    assert run(snap())["disk"].status == OK  # older snapshots have no host part


def test_addon_entities_are_essential_and_sensor_fields_validated():
    from homewatch.modules.homeassistant.plugin import _addon, _sensors, _special
    e = {"entity_id": "binary_sensor.samba_share_running", "attributes": {"device_class": "running"}}
    assert _addon(e) and _special(e)
    assert not _addon({"entity_id": "binary_sensor.washer_status", "attributes": {"device_class": "running"}})
    assert _sensors({}) == {"disk": "sensor.system_monitor_disk_usage", "memory": "sensor.system_monitor_memory_usage",
                            "cpu": "sensor.system_monitor_processor_use"}
    base = {"url": "http://ha.local:8123", "full_every": "30"}
    out, err = plugin.validate(base | {"disk_sensor": " sensor.my_disk ", "memory_sensor": ""}, {})
    assert not err and out["disk_sensor"] == "sensor.my_disk" and out["memory_sensor"] == ""
    assert "entity id" in plugin.validate(base | {"disk_sensor": "light.x"}, {})[1]


def test_addon_and_host_sensors_read_between_full_reads(monkeypatch):
    import asyncio

    from homewatch.modules.homeassistant.plugin import HomeAssistantModule
    z2m = {"entity_id": "binary_sensor.zigbee2mqtt_running", "state": "on",
           "attributes": {"device_class": "running", "friendly_name": "Zigbee2MQTT Running"}}
    vm = {"entity_id": "binary_sensor.qemu_win11_status", "state": "off", "attributes": {"device_class": "running"}}
    disk = {"entity_id": "sensor.system_monitor_disk_usage", "state": "17.7", "attributes": {"unit_of_measurement": "%"}}
    mem = {"entity_id": "sensor.ram", "state": "40", "attributes": {"unit_of_measurement": "%"}}
    calls = _ha(monkeypatch, [z2m, vm, disk, mem])
    m = HomeAssistantModule()
    s = {"url": "http://ha", "verify": False, "memory_sensor": "sensor.ram"}
    first = asyncio.run(m.fetch(s, "t", NOW))
    assert first["host"] == {"disk": ["17.7", "%"], "memory": ["40", "%"], "cpu": None}
    assert [e["entity_id"] for e in first["watch"]] == ["binary_sensor.zigbee2mqtt_running"]
    calls.clear()
    m.snap_ts = NOW
    z2m["state"] = "off"
    second = asyncio.run(m.fetch(s, "t", NOW + 120))
    assert "/api/states" not in calls and "/api/states/sensor.ram" in calls
    assert "/api/states/binary_sensor.qemu_win11_status" not in calls
    assert second["watch"][0]["state"] == "off" and second["host"]["disk"] == ["17.7", "%"]


RULES = """# alarm panel
binary_sensor.panel_troubles_* is on warning
binary_sensor.panel_zone_*_tamper is on error
sensor.panel_link is not online error
sensor.oil below 200 warning
sensor.oil below 100 error"""


def ruled(eid, state, name="", unit=""):
    return {"entity_id": eid, "state": state, "name": name, "unit": unit}


def run_rules(entities, rules=RULES):
    s = {"enabled": True, "chosen": [], "rules": rules}
    th = {d.id: Thresholds(d.warn, d.error, d.default_on) for d in plugin.checks}
    res = {r.key.split(".")[1]: r for r in plugin.evaluate(s, th, (s, snap(ruled=entities), "", NOW))}
    return res["rules"]


QUIET = [ruled("binary_sensor.panel_troubles_ac", "off"), ruled("binary_sensor.panel_troubles_phone", "off"),
         ruled("binary_sensor.panel_zone_kitchen_tamper", "off"), ruled("sensor.panel_link", "online"),
         ruled("sensor.oil", "640", "Heating oil", "L")]


def test_entity_rules_all_fine():
    r = run_rules(QUIET)
    assert r.status == OK and r.detail == "5 rules, 5 entities fine"


def test_entity_rules_fire_with_the_worst_rule_per_entity():
    ents = [dict(e) for e in QUIET]
    ents[1]["state"] = "on"      # a trouble -> warning
    ents[4]["state"] = "80"      # below 200 and below 100 -> one line, error
    r = run_rules(ents)
    assert r.status == ERROR and r.value == 2
    assert r.detail.startswith("Heating oil 80 L (below 100)") and "binary_sensor.panel_troubles_phone on (is on)" in r.detail
    assert "below 200" not in r.detail


def test_entity_rules_is_not_counts_an_unavailable_link():
    ents = [dict(e) for e in QUIET]
    ents[3]["state"] = "unavailable"
    r = run_rules(ents)
    assert r.status == ERROR and "is not online" in r.detail


def test_entity_rules_warn_on_no_match_and_no_number():
    ents = [e for e in QUIET if e["entity_id"] != "sensor.panel_link"]
    ents[-1] = ruled("sensor.oil", "unknown")
    r = run_rules(ents)
    assert r.status == WARNING
    assert "no entity matches sensor.panel_link" in r.detail and "sensor.oil: no reading (unknown)" in r.detail


def test_entity_rules_empty_and_bad_lines():
    assert run_rules([], rules="").status == OK
    base = {"url": "http://ha.local:8123", "full_every": "30"}
    out, err = plugin.validate(base | {"rules": "sensor.oil below lots warning"}, {})
    assert "line 1" in err and "number" in err
    out, err = plugin.validate(base | {"rules": "\n# note\nsensor.oil is low\n"}, {})
    assert "line 3" in err
    out, err = plugin.validate(base | {"rules": " sensor.oil  below 100   ERROR \r\n\r\nsensor.x is not on warning"}, {})
    assert not err and out["rules"] == "sensor.oil  below 100   ERROR\n\nsensor.x is not on warning"


def test_rule_entities_are_read_between_full_reads(monkeypatch):
    import asyncio

    from homewatch.modules.homeassistant.plugin import HomeAssistantModule
    trouble = {"entity_id": "binary_sensor.panel_troubles_ac", "state": "off", "attributes": {"friendly_name": "AC loss"}}
    oil = {"entity_id": "sensor.oil", "state": "300", "attributes": {"unit_of_measurement": "L"}}
    other = {"entity_id": "sensor.other", "state": "1"}
    calls = _ha(monkeypatch, [trouble, oil, other])
    m = HomeAssistantModule()
    s = {"url": "http://ha", "verify": False, "rules": RULES}
    first = asyncio.run(m.fetch(s, "t", NOW))
    assert sorted(e["entity_id"] for e in first["ruled"]) == ["binary_sensor.panel_troubles_ac", "sensor.oil"]
    assert first["ruled"][0]["name"] == "AC loss" and first["ruled"][1]["unit"] == "L"
    calls.clear()
    m.snap_ts = NOW
    trouble["state"] = "on"
    second = asyncio.run(m.fetch(s, "t", NOW + 120))
    assert "/api/states" not in calls and "/api/states/binary_sensor.panel_troubles_ac" in calls
    assert "/api/states/sensor.other" not in calls
    assert next(e for e in second["ruled"] if e["entity_id"].startswith("binary"))["state"] == "on"
