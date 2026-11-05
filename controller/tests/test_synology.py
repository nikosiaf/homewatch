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


def run_with(s, snap):
    th = {d.id: Thresholds(d.warn, d.error, d.default_on) for d in plugin.checks}
    return {r.key.split(".")[1]: r for r in plugin.evaluate(s, th, (s, snap, "", NOW))}


SNMP = S | {"snmp_on": True, "snmp_user": "hw", "snmp_auth_protocol": "SHA", "snmp_auth": "authpass1",
            "snmp_priv_on": True, "snmp_priv_protocol": "AES", "snmp_priv": "privpass1"}


@respx.mock
async def test_fans_and_power_over_snmp(monkeypatch):
    from homewatch.modules.synology import snmp
    respx.get("https://nas.test:5001/webapi/entry.cgi").mock(side_effect=dsm())
    asked = []

    async def fake(host, user, auth, priv, proto="SHA", priv_proto="AES", timeout=3):
        asked.append((host, user, auth, priv, proto, priv_proto))
        return {"system": 1, "power": 1, "system_fan": 2, "cpu_fan": None}

    monkeypatch.setattr(snmp, "read", fake)
    snap = await plugin.fetch(SNMP, "pw", NOW)
    assert asked == [("nas.test", "hw", "authpass1", "privpass1", "SHA", "AES")]
    res = run_with(SNMP, snap)
    assert res["hardware"].status == ERROR and res["hardware"].detail == "system fan failed"
    snap["hw"]["system_fan"] = 1
    assert run_with(SNMP, snap)["hardware"].detail == "system, power supply, system fan normal"
    # MD5 without privacy (authNoPriv): no privacy password needed
    no_priv = SNMP | {"snmp_auth_protocol": "MD5", "snmp_priv_on": False, "snmp_priv": ""}
    asked.clear()
    snap = await plugin.fetch(no_priv, "pw", NOW)
    assert asked == [("nas.test", "hw", "authpass1", "", "MD5", "")] and not snap["hw_error"]


@respx.mock
async def test_snmp_off_missing_password_and_unreadable(monkeypatch):
    from homewatch.modules.synology import snmp
    respx.get("https://nas.test:5001/webapi/entry.cgi").mock(side_effect=dsm())
    snap = await plugin.fetch(S, "pw", NOW)
    assert run_with(S, snap)["hardware"].status == OK and "not watched" in run_with(S, snap)["hardware"].detail
    half = SNMP | {"snmp_priv": ""}
    snap = await plugin.fetch(half, "pw", NOW)
    assert run_with(half, snap)["hardware"].status == WARNING and "privacy password" in snap["hw_error"]
    snap = await plugin.fetch(SNMP | {"snmp_on": False}, "pw", NOW)  # a user but the box unticked: not read
    assert snap["hw"] is None and "not watched" in run_with(SNMP | {"snmp_on": False}, snap)["hardware"].detail

    async def broken(*a, **k):
        raise snmp.SnmpError("SNMP: Unknown USM user")

    monkeypatch.setattr(snmp, "read", broken)
    snap = await plugin.fetch(SNMP, "pw", NOW)
    res = run_with(SNMP, snap)
    assert res["hardware"].status == WARNING and "Unknown USM user" in res["hardware"].detail
    assert res["api"].status == OK  # DSM itself still reads fine


def test_snmp_settings_validated_and_passwords_are_secret():
    base = {"url": "nas.test", "username": "homewatch"}
    out, err = plugin.validate(base | {"snmp_auth_protocol": "md5", "snmp_priv_protocol": "des"}, {})
    assert not err and out["snmp_auth_protocol"] == "MD5" and out["snmp_priv_protocol"] == "DES"
    assert "SHA, MD5, SHA256" in plugin.validate(base | {"snmp_auth_protocol": "SHA512"}, {})[1]
    assert "AES, DES" in plugin.validate(base | {"snmp_priv_protocol": "3DES"}, {})[1]
    assert "SNMPv3 user name" in plugin.validate(base | {"snmp_on": "on", "_new": "1"}, {})[1]
    assert {f.key for f in plugin.fields if f.secret} == {"snmp_auth", "snmp_priv"}
    assert "snmp_auth" not in out and "snmp_priv" not in out


def test_settings_from_before_028_keep_snmp_on(db):
    from types import SimpleNamespace
    rt = SimpleNamespace(db=db)
    rt.db.set("module.synology", {"url": "https://nas.test:5001", "username": "homewatch", "snmp_user": "hw"})
    s = plugin.load(rt)
    assert s["snmp_on"] and s["snmp_priv_on"] and s["snmp_priv_protocol"] == "AES"
    rt.db.set("module.synology", {"url": "https://nas.test:5001", "username": "homewatch", "snmp_user": ""})
    assert not plugin.load(rt)["snmp_on"]
