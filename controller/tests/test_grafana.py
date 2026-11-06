"""Grafana: provisioning files written by the controller, and the login check Caddy asks before /grafana/."""

import json
import re

from tests.test_web import client, csrf, setup_admin  # noqa: F401 - fixtures

from homewatch import grafana


def all_panels(board):
    return [p for p in board["panels"]]


def test_every_dashboard_is_valid():
    enabled = set(grafana.BUILDERS)
    files = grafana.files(enabled)
    uids = set()
    for rel, text in files.items():
        data = json.loads(text)
        if not rel.endswith(".json"):
            continue
        assert data["uid"] not in uids
        uids.add(data["uid"])
        for p in all_panels(data):
            g = p["gridPos"]
            assert 0 <= g["x"] and g["x"] + g["w"] <= 24, (rel, p["title"])
            for t in p.get("targets", []):
                assert 'site="$site"' in t["expr"], (rel, p["title"], t["expr"])
                assert t["expr"].count("(") == t["expr"].count(")"), t["expr"]
                assert t["datasource"]["uid"] == "homewatch-vm"
    assert {f"dashboards/homewatch/{m}.json" for m in enabled} <= set(files)
    ds = json.loads(files["datasources/homewatch.yaml"])["datasources"][0]
    assert ds["url"] == "http://victoriametrics:8428" and ds["editable"] is False
    assert "max_lookback=20m" in ds["jsonData"]["customQueryParameters"]  # 10/15-minute modules between readings
    over = json.loads(files["dashboards/homewatch/overview.json"])
    assert [link["title"] for link in over["links"]] == ["homewatch"]  # no way "back" from the Overview itself
    assert json.loads(files["dashboards/homewatch/ups.json"])["links"][-1]["url"] == "/grafana/d/hw-overview"
    prov = json.loads(files["dashboards/homewatch.yaml"])["providers"]
    assert prov[0]["allowUiUpdates"] is False  # built-ins can't be saved; "Save as" goes to My dashboards


def test_overview_follows_enabled_modules():
    small = json.dumps(grafana.overview({"internet"}))
    assert "ping_average_response_ms" in small and "hw_ups" not in small
    big = json.dumps(grafana.overview({"internet", "ups", "proxmox"}))
    assert "hw_ups_power_watts" in big and "hw_proxmox_node_cpu_percent" in big


def test_write_adds_and_removes(tmp_path):
    d = tmp_path / "g"
    assert grafana.write(str(d), {"ups"}) is False  # no volume: Grafana not installed, nothing written
    d.mkdir()
    assert grafana.write(str(d), {"ups", "proxmox"})
    assert (d / "dashboards/homewatch/ups.json").exists() and (d / "dashboards/my/welcome.json").exists()
    assert grafana.write(str(d), {"ups", "proxmox"}) is False  # unchanged
    assert grafana.write(str(d), {"ups"})
    assert not (d / "dashboards/homewatch/proxmox.json").exists()


def test_runtime_writes_dashboards(cfg, tmp_path):
    from dataclasses import replace

    from homewatch.runtime import Runtime

    (tmp_path / "gp").mkdir()
    rt = Runtime.create(replace(cfg, grafana_dir=str(tmp_path / "gp")))
    rt.write_grafana()
    assert (tmp_path / "gp/dashboards/homewatch/overview.json").exists()


def test_grafana_login_check(client):  # noqa: F811
    r = client.get("/auth/grafana", follow_redirects=False)
    assert r.status_code == 303  # before setup: the wizard
    tok = setup_admin(client)
    r = client.get("/auth/grafana", follow_redirects=False)
    assert r.status_code == 200
    assert r.headers["X-Webauth-User"] == "admin" and r.headers["X-Webauth-Role"] == "Admin"
    from homewatch.auth import Auth

    Auth(client.rt.db).create_user("viewer", "a-long-password", "viewer")
    client.post("/logout", data={"csrf": tok})
    r = client.get("/auth/grafana", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("/login")
    login = client.get("/login")
    client.post("/login", data={"username": "viewer", "password": "a-long-password", "csrf": csrf(login.text)})
    r = client.get("/auth/grafana", follow_redirects=False)
    assert r.status_code == 200 and r.headers["X-Webauth-Role"] == "Viewer"


def test_caddy_strips_client_supplied_identity():
    import pathlib

    caddy = (pathlib.Path(__file__).parents[2] / "deploy" / "Caddyfile").read_text()
    block = re.search(r"handle /grafana\* \{.*?\n\t\}", caddy, re.S).group(0)
    assert block.index("request_header -X-Webauth-User") < block.index("forward_auth")
    assert "copy_headers X-Webauth-User X-Webauth-Role" in block


def test_updater_commands(tmp_path):
    from homewatch import updater

    upd = tmp_path / "update"
    (upd / "ask").mkdir(parents=True)
    d = str(upd)
    assert updater.features(d) == set()
    assert "older" in updater.ask(d, "", "admin", "grafana on")  # an older updater would just update
    (upd / "features").write_text("update grafana retention\n")
    assert updater.ask(d, "", "admin", "grafana rm -rf") != ""  # only the fixed words
    assert updater.ask(d, "", "admin", "retention 90d") == ""
    assert (upd / "ask" / "request").read_text() == "retention 90d\n"
    assert updater.ask(d, "9.9.9", "admin") == "" and (upd / "ask" / "request").read_text().endswith(" admin 9.9.9\n")


def test_system_grafana_and_history(client, tmp_path):  # noqa: F811
    tok = setup_admin(client)
    page = client.get("/system").text
    box = page.split("<h2>Grafana and history</h2>")[1].split('id="users"')[0]
    assert "Kept for</td><td>1 year" in box and "not part of this install" in box  # no provisioning volume
    upd = tmp_path / "update"
    (upd / "ask").mkdir(parents=True)
    (upd / "features").write_text("update grafana retention\n")
    (tmp_path / "gp").mkdir()
    object.__setattr__(client.rt.cfg, "update_dir", str(upd))
    object.__setattr__(client.rt.cfg, "grafana_dir", str(tmp_path / "gp"))
    box = client.get("/system").text.split("<h2>Grafana and history</h2>")[1].split('id="users"')[0]
    assert 'action="/system/grafana"' in box and ">Turn on</button>" in box and 'name="keep"' in box
    r = client.post("/system/grafana", data={"csrf": tok, "to": "on"})
    assert "Asked the host to turn Grafana on" in r.text and (upd / "ask" / "request").read_text() == "grafana on\n"
    (upd / "ask" / "request").unlink()
    r = client.post("/system/retention", data={"csrf": tok, "keep": "90d", "current": "wrong"})
    assert not (upd / "ask" / "request").exists()  # a shorter history deletes data: the password first
    r = client.post("/system/retention", data={"csrf": tok, "keep": "90d", "current": "a-long-password"})
    assert "Asked the host to keep 3 months" in r.text and (upd / "ask" / "request").read_text() == "retention 90d\n"
    r = client.post("/system/retention", data={"csrf": tok, "keep": "5y", "current": "a-long-password"})
    assert "Choose how long" in r.text
    assert client.get("/system/grafana-exports/..%2F..%2Fetc%2Fpasswd", follow_redirects=False).status_code in (303, 404)
    assert client.get("/system/grafana-exports/homewatch.db", follow_redirects=False).status_code == 303  # not an export


def test_export_my_dashboards(tmp_path):
    import asyncio

    import httpx

    seen = []

    def handler(req):
        seen.append(req.headers.get("X-Webauth-Role"))
        if req.url.path.endswith("/api/health"):
            return httpx.Response(200, json={"database": "ok"})
        if req.url.path.endswith("/api/search"):
            assert req.url.params["folderUIDs"] == "my-dashboards"
            return httpx.Response(200, json=[{"uid": "hw-my-welcome"}, {"uid": "mine1"}])
        return httpx.Response(200, json={"dashboard": {"uid": "mine1", "title": "Mine"}})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await grafana.export_my(http, "http://g/grafana", str(tmp_path), "2026-11-06")

    path = asyncio.run(go())
    data = json.loads(open(path).read())
    assert [d["uid"] for d in data["dashboards"]] == ["mine1"]  # the welcome note is not yours
    assert "Admin" not in seen  # read as a viewer


def test_export_skips_without_grafana(tmp_path):
    import asyncio

    import httpx

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(502))) as http:
            return await grafana.export_my(http, "http://g/grafana", str(tmp_path), "2026-11-06")

    assert asyncio.run(go()) == ""


def test_links_from_grafana_go_back_to_homewatch():
    import pathlib
    import re as _re

    over = json.dumps(grafana.overview({"ups"}))
    assert '"/grafana/homewatch/open/${__value.raw}"' in over
    caddy = (pathlib.Path(__file__).parents[2] / "deploy" / "Caddyfile").read_text()
    assert caddy.index("handle /grafana/homewatch/*") < caddy.index("handle /grafana* {")
    pattern = _re.search(r"@open path_regexp open (\S+)", caddy).group(1)  # Caddy matches the decoded path
    assert _re.match(pattern, "/grafana/homewatch/open/opnsense").group(1) == "opnsense"
    for bad in ("/grafana/homewatch/open//evil.com", "/grafana/homewatch/open/a/b", "/grafana/homewatch/open/",
                "/grafana/homewatch/open/x?y", "/grafana/homewatch/modules/opnsense", "/grafana/homewatch/open/%2F"):
        assert not _re.match(pattern, bad), bad
    assert "redir @open /?open={re.open.1} 302" in caddy


def test_status_and_menu_link_to_grafana_when_it_runs(client):  # noqa: F811
    from homewatch.modules import CheckResult

    setup_admin(client)
    client.rt.results = [CheckResult("ups.load", "Load", 0, "fine")]
    client.rt.last_run = 1
    page = client.get("/").text
    assert "Grafana ↗" not in page and "gf-link" not in page
    client.rt.grafana_on = True
    page = client.get("/").text
    assert '<a href="/grafana/" target="_blank" rel="noopener"' in page
    assert 'class="gf-link when-open" href="/grafana/d/hw-ups" target="_blank"' in page
