"""Synology: login flow against a mocked DSM, and the checks."""

import time

import httpx
import pytest
import respx

from homewatch.modules import ERROR, OK, WARNING, Thresholds
from homewatch.modules.apibase import ApiError
from homewatch.modules.synology.plugin import plugin

NOW = time.time()
DATA = {
    "SYNO.Core.System": {"model": "DS920+", "firmware_ver": "DSM 7.4", "up_time": "1:00:00", "sys_temp": 41,
                         "temperature_warning": False},
    "SYNO.Core.System.Status": {"is_system_crashed": False},
    "SYNO.Core.System.Utilization": {"cpu": {"user_load": 9, "system_load": 4, "other_load": 11},
                                     "memory": {"real_usage": 47}},
    "SYNO.Storage.CGI.Storage": {
        "disks": [{"name": "Drive 1", "status": "normal", "smart_status": "normal", "temp": 26, "unc": 0}],
        "storagePools": [{"id": "reuse_1", "status": "normal"}],
        "volumes": [{"id": "volume_1", "vol_path": "/volume1", "status": "normal",
                     "size": {"used": "44", "total": "100"}}]},
    "SYNO.Core.Upgrade.Server": {"update": {"available": False}},
}


def dsm(overrides=None, login_error=None):
    def answer(request):
        api = request.url.params["api"]
        if api == "SYNO.API.Auth":
            if request.url.params["method"] == "logout":
                return httpx.Response(200, json={"success": True})
            if login_error:
                return httpx.Response(200, json={"success": False, "error": {"code": login_error}})
            return httpx.Response(200, json={"success": True, "data": {"sid": "SID"}})
        return httpx.Response(200, json={"success": True, "data": (overrides or {}).get(api, DATA[api])})
    return answer


S = {"url": "https://nas.test:5001", "username": "homewatch", "verify": False, "enabled": True}


@respx.mock
async def test_fetch_logs_in_reads_and_logs_out():
    route = respx.get("https://nas.test:5001/webapi/entry.cgi").mock(side_effect=dsm())
    snap = await plugin.fetch(S, "pw", NOW)
    calls = [c.request.url.params for c in route.calls]
    assert calls[0]["method"] == "login" and calls[-1]["method"] == "logout"
    assert all(c.get("_sid") == "SID" for c in calls[1:])
    assert snap["volumes"][0]["path"] == "/volume1" and snap["update"] is False


@respx.mock
async def test_login_errors_are_explained():
    respx.get("https://nas.test:5001/webapi/entry.cgi").mock(side_effect=dsm(login_error=403))
    with pytest.raises(ApiError, match="2-step verification"):
        await plugin.fetch(S, "pw", NOW)


def run(snap):
    th = {d.id: Thresholds(d.warn, d.error, d.default_on) for d in plugin.checks}
    return {r.key.split(".")[1]: r for r in plugin.evaluate(S, th, (S, snap, "", NOW))}


@respx.mock
async def test_checks_healthy_and_degraded():
    respx.get("https://nas.test:5001/webapi/entry.cgi").mock(side_effect=dsm())
    snap = await plugin.fetch(S, "pw", NOW)
    assert all(r.status == OK for r in run(snap).values())
    snap["pools"][0]["status"] = "degraded"
    snap["disks"][0].update(status="crashed")
    snap["update"] = True
    res = run(snap)
    assert res["storage"].status == ERROR and res["disks"].status == ERROR and res["updates"].status == WARNING
