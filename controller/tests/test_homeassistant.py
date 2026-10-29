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


def test_unwatched_domain_is_not_reported():
    old = {"entity_id": "sensor.gone", "state": "unavailable", "last_changed": iso(NOW - 3600), "name": "Gone"}
    sn = snap(unavailable=[old])
    s = {"enabled": True, "chosen": [], "skipped": ["sensor"]}
    th = {d.id: Thresholds(d.warn, d.error, d.default_on) for d in plugin.checks}
    res = {r.key.split(".")[1]: r for r in plugin.evaluate(s, th, (s, sn, "", NOW))}
    assert res["unavailable"].status == OK and "ignoring 1" in res["unavailable"].detail


def test_choices_grouped_by_domain_and_domain_filters():
    sn = snap(unavailable=[{"entity_id": "light.old", "name": "Old light"}, {"entity_id": "sensor.x", "name": ""}],
              domains={"sensor": 40, "light": 3})
    assert [(c.group, c.label, c.detail) for c in plugin.choices(sn)] == [
        ("light", "Old light", "light.old"), ("sensor", "x", "")]
    assert [(c.value, c.detail) for c in plugin.filters(sn)] == [("light", "(3)"), ("sensor", "(40)")]
