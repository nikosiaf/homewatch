"""0.42: the read-only JSON API and its tokens, storage forecasts, latency under load, the Health dashboard and
time-to-recover lines."""

import time

import httpx

from homewatch.modules import OK, WARNING, ERROR, CheckResult, Thresholds, forecast_result
from tests.test_web import client, csrf, setup_admin  # noqa: F401 - the fixture


def make_token(client):  # noqa: F811
    tok = setup_admin(client)
    r = client.post("/system/api-tokens", data={"csrf": tok, "name": "Home Assistant", "current": "a-long-password"})
    assert r.url.path == "/system"
    page = r.text
    import re

    token = re.search(r"(hw_[A-Za-z0-9_-]{40,})", page).group(1)
    assert "not shown again" in page
    assert token not in client.get("/system").text  # shown once
    return token, tok


def test_api_needs_a_token_and_only_reads(client):  # noqa: F811
    token, _ = make_token(client)
    assert client.get("/api/v1/status").status_code == 401
    assert client.get("/api/v1/status", headers={"Authorization": "Bearer hw_wrong"}).status_code == 401
    h = {"Authorization": f"Bearer {token}"}
    r = client.get("/api/v1/status", headers=h)
    assert r.status_code == 200 and r.json()["level_word"] == "OK" and "modules" in r.json()
    assert client.post("/api/v1/status", headers=h).status_code == 405
    assert client.get("/api/v1/nope", headers=h).status_code == 404
    row = client.rt.db.one("SELECT last_used, token_hash FROM api_tokens")
    assert row["last_used"] and token not in row["token_hash"]  # stored as a hash only


def test_api_lists(client):  # noqa: F811
    token, _ = make_token(client)
    h = {"Authorization": f"Bearer {token}"}
    rt = client.rt
    now = time.time()
    rt.results = [CheckResult("internet.loss", "Packet loss", WARNING, "packet loss 30 %", 30, "%")]
    rt.db.run("INSERT INTO check_state(site,check_key,status,since,detail) VALUES('home','internet.loss',1,?,'x')", (now,))
    inc = rt.db.run("INSERT INTO incidents(site,started,worst,cause) VALUES('home',?,1,'Line problem')", (now - 600,))
    rt.db.run("INSERT INTO issues(site,check_key,level,worst,started,detail,notified,incident_id) "
              "VALUES('home','internet.loss',1,1,?,'packet loss 30 %',1,?)", (now - 600, inc))
    rt.db.run("INSERT INTO events(site,ts,module,device,kind,detail,exact,seen) VALUES('home',?,'speedport','modem',"
              "'line_resync','DSL line re-synced',1,?)", (now - 300, now))
    rt.db.run("INSERT INTO todos(site,check_key,name,item,first_seen,last_seen) VALUES('home','pbs.updates','Updates',"
              "'3 package updates',?,?)", (now - 86400, now))
    checks = client.get("/api/v1/checks", headers=h).json()
    assert checks[0]["key"] == "internet.loss" and checks[0]["level_word"] == "WARNING" and checks[0]["value"] == 30
    issues = client.get("/api/v1/issues?open=1", headers=h).json()
    assert len(issues) == 1 and issues[0]["name"] == "Packet loss" and issues[0]["incident"] == inc
    incs = client.get("/api/v1/incidents", headers=h).json()
    assert incs[0]["cause"] == "Line problem" and incs[0]["checks"] == ["internet.loss"]
    assert client.get("/api/v1/events?days=1", headers=h).json()[0]["kind"] == "line_resync"
    assert client.get("/api/v1/todos", headers=h).json()[0]["item"] == "3 package updates"
    assert client.get("/api/v1/status", headers=h).json()["todos"] == 1


def test_deleted_token_stops_working(client):  # noqa: F811
    token, tok = make_token(client)
    tid = client.rt.db.one("SELECT id FROM api_tokens")["id"]
    client.post(f"/system/api-tokens/{tid}/delete", data={"csrf": tok})
    assert client.get("/api/v1/status", headers={"Authorization": f"Bearer {token}"}).status_code == 401


# ---- forecasts -------------------------------------------------------------------------------------------
def test_forecast_result():
    t = Thresholds(30, 7)
    assert forecast_result("synology.forecast", {}, t).detail == "nothing grew over the last 7 days"
    r = forecast_result("synology.forecast", {"/volume1": 2457.1}, t)
    assert r.status == OK and r.detail == "/volume1 full in 2457 days" and r.value == 2457.1
    r = forecast_result("pbs.forecast", {"a": 20, "b": 400}, t)
    assert r.status == WARNING and r.detail == "a full in 20 days (the soonest of 2)"
    assert forecast_result("pbs.forecast", {"a": 3}, t).status == ERROR
    assert forecast_result("pbs.forecast", {"a": 9e9}, t).detail == "a full not within 10 years"
    assert forecast_result("pbs.forecast", None, t).unknown


async def test_forecast_query(cfg, respx_mock):
    from homewatch.modules import forecast
    from homewatch.runtime import Runtime

    rt = Runtime.create(cfg)
    route = respx_mock.get(url__regex=r".*/api/v1/query.*").mock(return_value=httpx.Response(200, json={
        "status": "success", "data": {"resultType": "vector", "result": [
            {"metric": {"site": "home", "store": "siafhome"}, "value": [1, "678.9"]}]}}))
    assert await forecast(rt, "hw_pbs_store_used_bytes", "hw_pbs_store_total_bytes", "store") == {"siafhome": 678.9}
    assert "deriv(hw_pbs_store_used_bytes" in str(route.calls[0].request.url).replace("%28", "(")
    respx_mock.get(url__regex=r".*/api/v1/query.*").mock(side_effect=httpx.ConnectError("down"))
    assert await forecast(rt, "a", "b", "c") is None


# ---- latency under load ----------------------------------------------------------------------------------
def test_bufferbloat_grade_and_check():
    from homewatch.modules.internet.checks import CHECKS, Speed, add_speed, grade

    assert [grade(x) for x in (2, 20, 50, 150, 300, 900)] == ["A+", "A", "B", "C", "D", "F"]
    th = {c.id: Thresholds(c.warn, c.error, True) for c in CHECKS}
    out = {}

    def add(cid, status, detail, value=None):
        out[cid] = (status, detail, value)

    now = time.time()
    s1 = Speed(now - 60, 95, 9, True, 40, "cloudflare", 52, 290)
    s2 = Speed(now - 3 * 3600, 95, 9, True, 40, "cloudflare", 50, 260)
    add_speed({}, th, [s1, s2], add, now)
    status, detail, value = out["bufferbloat"]
    assert status == WARNING and value == 250
    assert detail == "+250 ms under load, grade D (idle 40, downloading 52, uploading 290 ms) on 2 tests in a row"
    add_speed({}, th, [Speed(now, 95, 9, True, 40, "ookla", 41, 45)], add, now)
    assert out["bufferbloat"][0] == OK and "grade A" in out["bufferbloat"][1]


def test_ookla_loaded_latency():
    from homewatch.modules.internet.speedtest import parse_ookla

    r = parse_ookla('{"ping": {"latency": 12.5}, "download": {"bandwidth": 12500000, "latency": {"iqm": 30.1}}, '
                    '"upload": {"bandwidth": 1250000, "latency": {"iqm": 180.4}}}')
    assert (r.down, r.up, r.latency, r.loaded_down, r.loaded_up) == (100, 10, 12.5, 30.1, 180.4)


# ---- Grafana ---------------------------------------------------------------------------------------------
def test_health_dashboard_and_forecast_row():
    from homewatch.grafana import files, health, overview

    h = health({"internet", "synology"})
    titles = [p["title"] for p in h["panels"]]
    assert "Reachable, last 30 days" in titles and "Incidents" in titles and "Time to recover, median of 30 days" in titles
    reach = next(p for p in h["panels"] if p["title"] == "Reachable, last 30 days")
    assert 'check=~"internet.reachable|synology.api"' in reach["targets"][0]["expr"]
    assert "dashboards/homewatch/health.json" in files(set())
    ov = [p["title"] for p in overview({"synology", "pbs"})["panels"]]
    assert "Days until full: NAS volumes" in ov and "Days until full: backup datastores" in ov
    assert not any(t.startswith("Days until full") for t in [p["title"] for p in overview(set())["panels"]])


def test_recovery_lines(cfg):
    from homewatch.runtime import Runtime

    rt = Runtime.create(cfg)
    now = time.time()
    rt.db.set("metrics.issues_seen", now - 300)
    rt.db.run("INSERT INTO issues(site,check_key,level,worst,started,resolved,resolved_detail) "
              "VALUES('home','internet.loss',1,1,?,?,'ok')", (now - 900, now - 60))
    rt.db.run("INSERT INTO issues(site,check_key,level,worst,started,resolved,resolved_detail) "
              "VALUES('home','pbs.api',2,2,?,?,'check switched off')", (now - 900, now - 60))
    (one,) = rt.recovery_lines(now)
    assert one.startswith("hw_issue,check=internet.loss,level=WARNING,module=internet,site=home duration_seconds=840.0")
