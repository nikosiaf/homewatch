"""Configuration versions: recorded after each change, viewable, restorable; secrets and users untouched."""

import json
import time

from homewatch import versions
from tests.test_web import client, setup_admin  # noqa: F401 - fixture


def test_record_only_when_config_changes(db):
    assert versions.record(db, "a", "x") is not None  # first version, even when empty
    assert versions.record(db, "a", "x") is None
    db.set("engine.fails", 3)  # runtime state, not configuration
    assert versions.record(db, "a", "x") is None
    db.set("home.name", "Cabin")
    db.run("INSERT INTO checks(site, check_key, enabled, warn, error) VALUES('home', 'internet.loss', 1, 5, 20)")
    vid = versions.record(db, "a", "home")
    assert db.one("SELECT summary FROM config_versions WHERE id=?", (vid,))["summary"] == "home, thresholds"


def test_keeps_the_last_versions(db, monkeypatch):
    monkeypatch.setattr(versions, "KEEP", 3)
    for i in range(6):
        db.set("home.name", f"h{i}")
        versions.record(db, "a", "home")
    rows = db.all("SELECT id FROM config_versions ORDER BY id")
    assert len(rows) == 3 and json.loads(db.one("SELECT data FROM config_versions ORDER BY id DESC")["data"])[
        "settings"]["home.name"] == "h5"


def test_restore_through_the_system_page(client):  # noqa: F811
    tok = setup_admin(client)
    client.post("/setup/home", data={"csrf": tok, "name": "Old name", "tz": "Europe/Athens"})
    client.rt.secrets.put("module.unifi.secret", "keep-me")
    old = versions.latest(client.rt.db)["id"]
    client.post("/system/home", data={"csrf": tok, "_box": "home", "name": "New name", "tz": "UTC",
                                      "url": "https://hw.example"})
    assert client.rt.db.get("home.name") == "New name"
    page = client.get("/system").text
    assert "Configuration versions" in page and f'/system/versions/{old}/restore' in page

    view = client.get(f"/system/versions/{old}")
    assert view.json()["settings"]["home.name"] == "Old name" and "secret" not in view.text.lower()

    r = client.post(f"/system/versions/{old}/restore", data={"csrf": tok})
    assert f"Version {old} restored" in r.text
    db = client.rt.db
    assert db.get("home.name") == "Old name" and db.get("home.tz") == "Europe/Athens"
    assert client.rt.secrets.get("module.unifi.secret") == "keep-me"
    assert db.one("SELECT username FROM users")["username"] == "admin"
    last = versions.latest(db)
    assert last["action"] == "config.restore" and last["id"] > old  # the restore itself can be undone
    r = client.post("/system/versions/99999/restore", data={"csrf": tok})
    assert "no longer exists" in r.text


def test_yaml_round_trip_and_bad_files(db):
    db.set("home.name", "Cabin")
    db.set("module.ups", {"enabled": True, "host": "10.0.0.5"})
    db.set("speedport.state", {"x": 1})  # runtime state: not exported
    db.run("INSERT INTO checks(site, check_key, enabled, warn, error) VALUES('home', 'ups.load', 1, 70, 90)")
    text = versions.to_yaml(db)
    assert text.startswith("# homewatch configuration export") and "speedport.state" not in text
    snap, err = versions.from_yaml(text)
    assert not err and snap == versions.snapshot(db)
    for bad, why in (("a: [", "not valid YAML"), ("format: 2", "format 1"),
                     ("format: 1\nsettings: {engine.fails: 3}", "unknown keys engine.fails"),
                     ("format: 1\nchecks: [{site: home}]", "checks entry 1: missing check_key"),
                     ("format: 1\nchannels: [{id: 1, kind: x, name: {a: 1}, enabled: 1, levels: '', quiet_start: null, "
                      "quiet_end: null, weekly_report: 0, config: '{}'}]", "name must be a plain value")):
        assert why in versions.from_yaml(bad)[1], bad


def test_nightly_export_keeps_last_files(db, tmp_path, monkeypatch):
    monkeypatch.setattr(versions, "EXPORTS_KEPT", 2)
    folder = str(tmp_path / "exports")
    for day in ("2026-10-01", "2026-10-02", "2026-10-03"):
        versions.write_export(db, folder, day)
    assert versions.list_exports(folder) == ["homewatch-config-2026-10-03.yaml", "homewatch-config-2026-10-02.yaml"]
    assert not versions.export_name_ok("../homewatch.db")


def test_export_and_import_through_the_system_page(client):  # noqa: F811
    tok = setup_admin(client)
    client.post("/setup/home", data={"csrf": tok, "name": "Exported", "tz": "Europe/Athens"})
    r = client.get("/system/export")
    assert r.headers["content-disposition"].startswith("attachment") and "home.name: Exported" in r.text
    exported = r.text
    client.post("/system/home", data={"csrf": tok, "_box": "home", "name": "Changed", "tz": "UTC", "url": ""})
    before = versions.latest(client.rt.db)["id"]

    r = client.post("/system/import", data={"csrf": tok}, files={"file": ("x.yaml", b"format: 7", "text/yaml")})
    assert "Nothing imported" in r.text and client.rt.db.get("home.name") == "Changed"
    r = client.post("/system/import", data={"csrf": tok}, files={"file": ("x.yaml", exported.encode(), "text/yaml")})
    assert "Imported" in r.text and client.rt.db.get("home.name") == "Exported"
    assert client.rt.db.one("SELECT username FROM users")["username"] == "admin"
    # the settings from before the import are a version that can be restored
    client.post(f"/system/versions/{before}/restore", data={"csrf": tok})
    assert client.rt.db.get("home.name") == "Changed"

    client.rt.nightly_export(time.time())
    page = client.get("/system").text
    assert "Export and import" in page and "/system/exports/homewatch-config-" in page
    name = versions.list_exports(client.rt.exports_dir)[0]
    assert "home.name: Changed" in client.get(f"/system/exports/{name}").text
    assert client.get("/system/exports/..%2Fhomewatch.db", follow_redirects=False).status_code in (303, 404)
