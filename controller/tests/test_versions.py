"""Configuration versions: recorded after each change, viewable, restorable; secrets and users untouched."""

import json
import base64
import html
import re
import time

import pytest
import yaml

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

    r = client.post(f"/system/versions/{old}/restore", data={"csrf": tok, "current": "a-long-password"})
    assert f"Version {old} restored" in r.text
    db = client.rt.db
    assert db.get("home.name") == "Old name" and db.get("home.tz") == "Europe/Athens"
    assert client.rt.secrets.get("module.unifi.secret") == "keep-me"
    assert db.one("SELECT username FROM users")["username"] == "admin"
    last = versions.latest(db)
    assert last["action"] == "config.restore" and last["id"] > old  # the restore itself can be undone
    r = client.post("/system/versions/99999/restore", data={"csrf": tok, "current": "a-long-password"})
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
    r = _import(client, tok, exported.encode())
    assert "Imported" in r.text and client.rt.db.get("home.name") == "Exported"
    assert client.rt.db.one("SELECT username FROM users")["username"] == "admin"
    # the settings from before the import are a version that can be restored
    client.post(f"/system/versions/{before}/restore", data={"csrf": tok, "current": "a-long-password"})
    assert client.rt.db.get("home.name") == "Changed"

    client.rt.nightly_export(time.time())
    page = client.get("/system").text
    assert "Export and import" in page and "/system/exports/homewatch-config-" in page
    name = versions.list_exports(client.rt.exports_dir)[0]
    assert "home.name: Changed" in client.get(f"/system/exports/{name}").text
    assert client.get("/system/exports/..%2Fhomewatch.db", follow_redirects=False).status_code in (303, 404)


def _channel(client, tok):
    client.post("/notifications/add", data={"csrf": tok, "kind": "ntfy", "ntfy.url": "https://ntfy.example/secret-topic",
                                            "ntfy.secret": "tk_1"})
    client.post("/notifications/add", data={"csrf": tok, "kind": "healthchecks", "healthchecks.url": "https://hc.example/uuid-1"})
    return {r["kind"]: r["id"] for r in client.rt.db.all("SELECT id, kind FROM channels")}


def test_exports_and_versions_hold_no_capability_urls(client):  # noqa: F811
    tok = setup_admin(client)
    ids = _channel(client, tok)
    text = client.get("/system/export").text
    assert "secret-topic" not in text and "uuid-1" not in text
    assert not any("secret-topic" in r["data"] or "uuid-1" in r["data"] for r in client.rt.db.all("SELECT data FROM config_versions"))
    # saving the channel with the field empty keeps the address
    r = client.post(f"/notifications/{ids['ntfy']}", data={"csrf": tok, "name": "n", "enabled": "on", "url": ""})
    assert "Saved" in r.text and "secret-topic" in client.rt.secrets.get(f"channel.{ids['ntfy']}.private")


def test_old_plain_channel_urls_are_moved(db, tmp_path):
    from homewatch.secrets import Secrets
    from tests.conftest import KEY

    cfg_key = base64.b64decode(KEY)
    cid = db.run("INSERT INTO channels(kind,name,levels,config) VALUES('healthchecks','h','[]',?)",
                 (json.dumps({"url": "https://hc.example/old"}),))
    versions.record(db, "t", "x")
    secrets = Secrets(db, cfg_key)
    assert versions.move_secret_fields(db, secrets)
    assert json.loads(db.one("SELECT config FROM channels")["config"]) == {}
    assert "hc.example" in secrets.get(f"channel.{cid}.private")
    assert "hc.example" not in db.one("SELECT data FROM config_versions ORDER BY id DESC")["data"]
    folder = tmp_path / "exports"
    folder.mkdir()
    old = folder / "homewatch-config-2026-01-01.yaml"
    old.write_text(yaml.safe_dump({"format": 1, "channels": [{"kind": "ntfy", "config": '{"url":"https://n/t","server":"https://n","topic":"t"}'}]}))
    versions.scrub_exports(str(folder))
    assert "https://n/t" not in old.read_text() and "https://n" in old.read_text()


def _export_with(client, change):
    doc = yaml.safe_load(client.get("/system/export").text)
    change(doc)
    return yaml.safe_dump(doc).encode()


def _import(client, tok, raw):
    """Upload, then confirm the preview."""
    r = client.post("/system/import", data={"csrf": tok}, files={"file": ("x.yaml", raw, "text/yaml")})
    m = re.search(r'<textarea name="text" hidden>(.*?)</textarea>', r.text, re.S)
    if not m:
        return r
    return client.post("/system/import/apply", data={"csrf": tok, "text": html.unescape(m.group(1)), "current": "a-long-password"})


def test_import_to_another_destination_drops_the_credentials(client):  # noqa: F811
    tok = setup_admin(client)
    rt = client.rt
    rt.db.set("module.synology", {"enabled": True, "url": "https://nas.test:5001", "username": "u", "verify": True})
    rt.secrets.put("module.synology.secret", "pw")
    ids = _channel(client, tok)
    private = f"channel.{ids['ntfy']}.private"

    # same destination: everything stays
    r = _import(client, tok, _export_with(client, lambda d: None))
    assert "Imported" in r.text and rt.secrets.get("module.synology.secret") == "pw" and rt.secrets.has(private)

    def elsewhere(d):
        d["settings"]["module.synology"]["url"] = "https://evil.test:5001"
        next(c for c in d["channels"] if c["kind"] == "ntfy")["config"] = json.dumps({"server": "https://evil.test"})

    r = _import(client, tok, _export_with(client, elsewhere))
    assert "credentials were deleted" in r.text
    assert not rt.secrets.has("module.synology.secret") and not rt.secrets.has(private)
    assert not rt.secrets.has(f"channel.{ids['ntfy']}") and not rt.db.get("module.synology")["enabled"]
    assert rt.secrets.has(f"channel.{ids['healthchecks']}.private")  # untouched


def test_import_turning_off_certificate_check_drops_the_credentials(client):  # noqa: F811
    tok = setup_admin(client)
    rt = client.rt
    rt.db.set("module.synology", {"enabled": True, "url": "https://nas.test:5001", "username": "u", "verify": True})
    rt.secrets.put("module.synology.secret", "pw")
    _import(client, tok, _export_with(client, lambda d: d["settings"]["module.synology"].update(verify=False)))
    assert not rt.secrets.has("module.synology.secret")


def test_restore_to_another_destination_drops_the_credentials(client):  # noqa: F811
    tok = setup_admin(client)
    rt = client.rt
    rt.db.set("module.synology", {"enabled": True, "url": "https://old.test:5001", "username": "u", "verify": True})
    old = versions.record(rt.db, "t", "x")
    rt.db.set("module.synology", {"enabled": True, "url": "https://new.test:5001", "username": "u", "verify": True})
    versions.record(rt.db, "t", "y")
    rt.secrets.put("module.synology.secret", "pw")
    client.post(f"/system/versions/{old}/restore", data={"csrf": tok, "current": "a-long-password"})
    assert not rt.secrets.has("module.synology.secret")


def test_import_uses_the_module_and_channel_rules(client):  # noqa: F811
    tok = setup_admin(client)
    bad = [
        lambda d: d["settings"].update({"module.synology": {"url": "http://nas.test"}}),  # https only
        lambda d: d["settings"].update({"module.speedport": {"host": "a b"}}),
        lambda d: d["settings"].update({"module.proxmox": {"url": "ftp://x"}}),
        lambda d: d["channels"].append({"id": 9, "kind": "nope", "name": "n", "enabled": 1, "levels": "[]", "quiet_start": None,
                                        "quiet_end": None, "weekly_report": 0, "config": "{}"}),
        lambda d: d["channels"].append({"id": 9, "kind": "ntfy", "name": "n", "enabled": 1, "levels": "[]", "quiet_start": None,
                                        "quiet_end": None, "weekly_report": 0, "config": '{"server": "ftp://x"}'}),
    ]
    for change in bad:
        assert "Nothing imported" in _import(client, tok, _export_with(client, change)).text


def test_failed_apply_keeps_the_credentials(client):  # noqa: F811
    tok = setup_admin(client)
    rt = client.rt
    rt.db.set("module.synology", {"enabled": True, "url": "https://nas.test:5001", "username": "u", "verify": True})
    rt.secrets.put("module.synology.secret", "pw")
    snap = versions.snapshot(rt.db)
    snap["settings"]["module.synology"]["url"] = "https://evil.test:5001"
    within = rt.guard_credentials(snap)

    def fail():
        within()
        raise RuntimeError("disk full")

    with pytest.raises(RuntimeError):
        versions.apply(rt.db, snap, fail)
    assert rt.secrets.get("module.synology.secret") == "pw"
    assert rt.db.get("module.synology")["url"] == "https://nas.test:5001"


def test_import_stores_what_it_checked(client):  # noqa: F811
    tok = setup_admin(client)
    rt = client.rt
    rt.db.set("module.synology", {"enabled": True, "url": "https://nas.test:5001", "username": "u", "verify": True})
    rt.secrets.put("module.synology.secret", "pw")
    # another way to write the same address is the same destination, and the normalized form is stored
    r = _import(client, tok, _export_with(client, lambda d: d["settings"]["module.synology"].update(url="nas.test")))
    assert "Imported" in r.text and rt.db.get("module.synology")["url"] == "https://nas.test:5001"
    assert rt.secrets.has("module.synology.secret")
    # the text "false" would read as on, and pass as "certificate check still on"
    r = _import(client, tok, _export_with(client, lambda d: d["settings"]["module.synology"].update(verify="false")))
    assert "Nothing imported" in r.text and rt.db.get("module.synology")["verify"] is True
    for bad in ({"enabled": "yes"},):
        r = _import(client, tok, _export_with(client, lambda d: d["settings"]["module.synology"].update(bad)))
        assert "Nothing imported" in r.text
    r = _import(client, tok, _export_with(client, lambda d: d["settings"].update({"module.ups": {"host": "h", "port": ".nan", "ups": ""}})))
    assert "Nothing imported" in r.text


def test_restore_message_tells_what_happened(client):  # noqa: F811
    tok = setup_admin(client)
    rt = client.rt
    rt.db.set("module.synology", {"enabled": True, "url": "https://old.test:5001", "username": "u", "verify": True})
    old = versions.record(rt.db, "t", "x")
    rt.db.set("module.synology", {"enabled": True, "url": "https://new.test:5001", "username": "u", "verify": True})
    versions.record(rt.db, "t", "y")
    rt.secrets.put("module.synology.secret", "pw")
    r = client.post(f"/system/versions/{old}/restore", data={"csrf": tok, "current": "a-long-password"})
    assert "credentials were deleted" in r.text and "left as they are" not in r.text


def test_import_preview_changes_nothing_and_says_what_goes(client):  # noqa: F811
    tok = setup_admin(client)
    rt = client.rt
    rt.db.set("module.synology", {"enabled": True, "url": "https://nas.test:5001", "username": "u", "verify": True})
    rt.secrets.put("module.synology.secret", "pw")
    raw = _export_with(client, lambda d: d["settings"]["module.synology"].update(url="https://evil.test:5001", verify=False))
    r = client.post("/system/import", data={"csrf": tok}, files={"file": ("x.yaml", raw, "text/yaml")})
    assert "Credentials that will be deleted" in r.text and "evil.test" in r.text and "Synology" in r.text
    assert rt.secrets.has("module.synology.secret") and rt.db.get("module.synology")["url"] == "https://nas.test:5001"
    text = html.unescape(re.search(r'<textarea name="text" hidden>(.*?)</textarea>', r.text, re.S).group(1))
    r = client.post("/system/import/apply", data={"csrf": tok, "text": text, "current": "nope"})
    assert "current password is wrong" in r.text and rt.secrets.has("module.synology.secret")
    r = client.post("/system/import/apply", data={"csrf": tok, "text": text, "current": "a-long-password"})
    assert "Imported" in r.text and not rt.secrets.has("module.synology.secret")


def test_import_of_the_same_settings_affects_no_credentials(client):  # noqa: F811
    tok = setup_admin(client)
    rt = client.rt
    rt.db.set("module.synology", {"enabled": True, "url": "https://nas.test:5001", "username": "u", "verify": True})
    rt.secrets.put("module.synology.secret", "pw")
    r = client.post("/system/import", data={"csrf": tok}, files={"file": ("x.yaml", client.get("/system/export").content, "text/yaml")})
    assert "No passwords or tokens are affected" in r.text


def test_restore_preview_comes_first(client):  # noqa: F811
    tok = setup_admin(client)
    rt = client.rt
    rt.db.set("module.synology", {"enabled": True, "url": "https://old.test:5001", "username": "u", "verify": True})
    old = versions.record(rt.db, "t", "x")
    rt.db.set("module.synology", {"enabled": True, "url": "https://new.test:5001", "username": "u", "verify": True})
    versions.record(rt.db, "t", "y")
    rt.secrets.put("module.synology.secret", "pw")
    page = client.get(f"/system/versions/{old}/restore").text
    assert "old.test" in page and "Credentials that will be deleted" in page and rt.secrets.has("module.synology.secret")
    r = client.post(f"/system/versions/{old}/restore", data={"csrf": tok})
    assert "current password is wrong" in r.text and rt.db.get("module.synology")["url"] == "https://new.test:5001"

