"""Grafana: provisioning files written by the controller, and the login check Caddy asks before /grafana/."""

import json
import re

from tests.test_web import client, csrf, setup_admin  # noqa: F401 - fixtures

from homewatch import grafana


def all_panels(board):
    return [p for p in board["panels"]]


META = [{"id": "sensor.kitchen_temperature", "name": 'Kitchen "main" $x', "unit": "°C", "dc": "temperature",
         "domain": "sensor", "fields": ["value"]},
        {"id": "sensor.hall_lux", "name": "Hall", "unit": "lx", "dc": "illuminance", "domain": "sensor", "fields": ["value"]},
        {"id": "switch.pump", "name": "Pump", "unit": "", "dc": "", "domain": "switch", "fields": ["value"]},
        {"id": "binary_sensor.front_door", "name": "Front, door", "unit": "", "dc": "door", "domain": "binary_sensor",
         "fields": ["value"]},
        {"id": "climate.living", "name": "Living", "unit": "", "dc": "", "domain": "climate",
         "fields": ["active", "current_temperature", "target_temperature", "value"]}]


def test_every_dashboard_is_valid():
    enabled = set(grafana.BUILDERS)
    for ha in (None, META):
        check_files(enabled, grafana.files(enabled, ha))


def check_files(enabled, files):
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


def test_overview_firewall_row_is_opnsense_only_and_ups_is_last():
    every = {"internet", "speedport", "opnsense", "ups", "mikrotik", "unifi", "proxmox", "pbs", "synology"}
    panels = grafana.overview(every)["panels"]
    rows = [p["title"] for p in panels if p["type"] == "row"]
    assert rows[-1] == "UPS" and rows.index("Firewall") == rows.index("Internet") + 1
    start = next(i for i, p in enumerate(panels) if p["title"] == "Firewall")
    end = next(i for i, p in enumerate(panels) if i > start and p["type"] == "row")
    fw = panels[start + 1:end]
    assert [p["title"] for p in fw] == ["Firewall CPU", "Firewall memory", "Firewall uptime", "HAProxy servers down",
                                        "Firewall disk", "Gateway delay", "Received per interface", "Sent per interface"]
    exprs = [t["expr"] for p in fw for t in p["targets"]]
    assert all(m.startswith("hw_opnsense_") for e in exprs for m in re.findall(r"\b(hw_\w+|ping_\w+|dns_query\w*)", e))
    assert [p["gridPos"]["w"] for p in fw] == [4] * 6 + [12, 12]
    assert "Firewall" not in [p["title"] for p in grafana.overview(every - {"opnsense"})["panels"]]


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


def _all(board):
    """Every panel, the ones folded inside collapsed rows too."""
    return [q for p in board["panels"] for q in [p, *p.get("panels", [])]]


def test_ha_dashboards_follow_recorded_entities(tmp_path):
    exprs = [t["expr"] for p in _all(grafana.homeassistant(META)) for t in p.get("targets", [])]
    temps = next(e for e in exprs if "kitchen" in e and "current_temperature" not in e)
    assert temps.startswith('label_map(label_copy(max by (entity_id) (hw_ha_entity_value{site="$site",entity_id=~"sensor' + chr(92) * 2 + '.kitchen_temperature"})')
    assert '"sensor.kitchen_temperature", "Kitchen ' + chr(92) + '"main' + chr(92) + '" x"' in temps  # quotes escaped, no $ variable
    board = grafana.homeassistant(META)
    titles = [p["title"] for p in _all(board)]
    assert {"Temperatures", "Thermostats", "Heating", "Doors", "Illuminance (lx)", "Switch"} <= set(titles)
    assert "On and off" not in titles and "Power" not in titles  # nothing recorded for them
    top = [p["title"] for p in board["panels"]]
    assert top[0] == "Temperatures now" and "Switch" not in top  # the leftovers are folded away
    assert next(p for p in board["panels"] if p["title"].startswith("Other recorded"))["collapsed"]
    mixed = META + [{"id": "sensor.coffee_pressure", "name": "Coffee", "unit": "bar", "dc": "pressure", "domain": "sensor",
                     "fields": ["value"]}, {"id": "sensor.out_pressure", "name": "Out", "unit": "hPa", "dc": "pressure",
                                            "domain": "sensor", "fields": ["value"]}]
    press = {p["title"]: p["fieldConfig"]["defaults"]["unit"] for p in _all(grafana.homeassistant(mixed))
             if p["title"].startswith("Pressure") and p["type"] != "row"}
    assert press == {"Pressure (bar)": "pressurebar", "Pressure (hPa)": "pressurehpa"}  # one unit per panel
    many = [{"id": f"light.l{i:03}", "name": "N" * 60, "unit": "", "dc": "", "domain": "light", "fields": ["value"]}
            for i in range(300)] + [{"id": f"sensor.t{i:03}", "name": "T" * 60, "unit": "°C", "dc": "temperature",
                                     "domain": "sensor", "fields": ["value"]} for i in range(300)]
    for b in (grafana.homeassistant(many), grafana.overview({"homeassistant"}, many)):
        for p in _all(b):
            for tg in p.get("targets", []):
                assert len(tg["expr"].encode()) < 16384, p["title"]  # VictoriaMetrics' -search.maxQueryLen
    empty = grafana.homeassistant([])["panels"]
    assert empty[0]["type"] == "text" and "Show in Grafana" in empty[0]["options"]["content"]
    assert grafana._tidy("Sh Basement Door basement_door_temperature") == "Sh Basement Door temperature"
    assert grafana._tidy("Κουζίνα") == "Κουζίνα"
    ex = grafana.ha_explorer(META)
    var = ex["templating"]["list"][-1]
    assert var["name"] == "entity" and "Front  door · binary_sensor.front_door : binary_sensor.front_door" in var["query"]
    assert var["current"]["value"] == ["sensor.kitchen_temperature"] and ex["panels"][0]["repeat"] == "entity"
    assert 'entity_id=~"$entity"' in ex["panels"][0]["targets"][0]["expr"]  # Grafana sends a multi-value as a regex
    d = tmp_path / "g"
    d.mkdir()
    assert grafana.write(str(d), {"homeassistant"}, META)
    assert (d / "dashboards/homewatch/ha-explorer.json").exists()
    assert grafana.write(str(d), {"homeassistant"}, META) is False
    renamed = [e | {"name": "Kitchen"} if e["id"] == "sensor.kitchen_temperature" else e for e in META]
    assert grafana.write(str(d), {"homeassistant"}, renamed)  # a new name rebuilds the dashboards
    assert grafana.write(str(d), {"ups"})
    assert not (d / "dashboards/homewatch/ha-explorer.json").exists()


def test_runtime_keeps_ha_names(cfg, tmp_path):
    from dataclasses import replace

    from homewatch.modules.homeassistant.plugin import plugin
    from homewatch.runtime import Runtime

    rt = Runtime.create(replace(cfg, grafana_dir=""))
    plugin.snap = {"recorded": [{"entity_id": "sensor.t", "values": {"value": 1.0}, "name": "T", "unit": "°C", "dc": ""}]}
    assert rt.ha_meta()[0]["fields"] == ["value"]
    plugin.snap = {"recorded": [{"entity_id": "sensor.t", "values": {}, "name": "T2", "unit": "°C", "dc": ""}]}
    assert rt.ha_meta() == [{"id": "sensor.t", "name": "T2", "unit": "°C", "dc": "", "domain": "sensor", "fields": ["value"]}]
    plugin.snap = None
    assert rt.ha_meta()[0]["name"] == "T2"  # no reading: the saved list


def test_entities_with_the_same_name_are_told_apart():
    disk = {"name": "Disk pve: Samsung SSD 990 EVO Plus 1TB Temperature", "unit": "°C", "dc": "temperature",
            "domain": "sensor", "fields": ["value"]}
    meta = [disk | {"id": "sensor.disk_pve_samsung_ssd_970_evo_plus_1tb_dev_nvme0n1_temperature"},
            disk | {"id": "sensor.disk_pve_samsung_ssd_980_1tb_dev_nvme1n1_temperature"},
            {"id": "sensor.hall_temperature", "name": "Hall", "unit": "°C", "dc": "temperature", "domain": "sensor",
             "fields": ["value"]},
            {"id": "sensor.a_x", "name": "Same"}, {"id": "binary_sensor.a_x", "name": "same"}]
    names = [e["name"] for e in grafana.unique_names(meta)]
    assert names == ["Disk pve: Samsung SSD 990 EVO Plus 1TB Temperature (nvme0n1)",
                     "Disk pve: Samsung SSD 990 EVO Plus 1TB Temperature (nvme1n1)", "Hall",
                     "Same (sensor)", "same (binary_sensor)"]
    temps = json.dumps(grafana.homeassistant(meta[:3]))
    assert "Temperature (nvme0n1)" in temps and "Temperature (nvme1n1)" in temps
    assert "(nvme1n1)" in json.dumps(grafana.ha_explorer(meta[:3])) and "(nvme0n1)" in json.dumps(
        grafana.overview({"homeassistant"}, meta[:3]))


def test_collapsed_rows_hold_their_panels():
    out = grafana._layout([grafana.stat("a", "x", w=24), grafana.row("Open"), grafana.ts("b", []),
                           grafana.row("Folded", collapsed=True), grafana.ts("c", []), grafana.ts("d", []),
                           grafana.ts("e", []), grafana.row("Folded too", collapsed=True), grafana.ts("f", [])])
    assert [p["title"] for p in out] == ["a", "Open", "b", "Folded", "Folded too"]
    assert [p["gridPos"]["y"] for p in out] == [0, 4, 5, 13, 14]  # a folded row takes one line
    assert [(q["title"], q["gridPos"]["x"], q["gridPos"]["y"]) for q in out[3]["panels"]] == [
        ("c", 0, 14), ("d", 12, 14), ("e", 0, 22)]
    assert [q["title"] for q in out[4]["panels"]] == ["f"]


def test_problem_counts_show_ok_at_zero():
    tile = grafana.stat("Unhealthy", "x", steps=[(None, "green"), (1, "red")])
    assert tile["fieldConfig"]["defaults"]["mappings"][-1]["options"]["0"]["text"] == "✔ OK"
    cpu = grafana.stat("CPU", "x", "percent", steps=grafana._pct_steps())
    assert cpu["fieldConfig"]["defaults"]["mappings"] == []  # 0 % CPU is a number, not a verdict


def test_docker_shows_only_containers_that_still_exist():
    d = grafana.docker()
    for title in ("Running", "Restarts"):
        (p,) = [p for p in d["panels"] if p.get("title") == title]
        assert "@ end()" in p["targets"][0]["expr"]
    (p,) = [p for p in grafana.overview(set())["panels"] if p.get("title") == "Modules"]
    assert "max_over_time" not in p["targets"][0]["expr"]
