"""Configuration versions: recorded after each change, viewable, restorable; secrets and users untouched."""

import json

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
