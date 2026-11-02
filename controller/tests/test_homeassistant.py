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
