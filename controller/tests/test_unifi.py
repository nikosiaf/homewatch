"""UniFi: login, CSRF header, logout, devices and firmware checks."""

import time

import httpx
import pytest
import respx

from homewatch.modules import ERROR, OK, WARNING, Thresholds
from homewatch.modules.apibase import ApiError
from homewatch.modules.unifi.plugin import plugin

NOW = time.time()
BASE = "https://unifi.test:11443"
S = {"url": BASE, "site": "default", "username": "homewatch", "verify": False, "enabled": True}
DEVICES = [{"mac": "m1", "name": "AP1", "model": "U7PG2", "state": 1, "version": "6.8", "upgradable": False,
            "uptime": 900000, "num_sta": 20},
           {"mac": "m2", "name": "AP2", "model": "U7LR", "state": 0, "version": "6.7", "upgradable": True,
            "upgrade_to_firmware": "6.8", "uptime": 0, "num_sta": 0}]


def mock(login_status=200):
    login = respx.post(BASE + "/api/auth/login").mock(
        return_value=httpx.Response(login_status, headers={"x-csrf-token": "CSRF"}))
    logout = respx.post(BASE + "/api/auth/logout").mock(return_value=httpx.Response(200))
    dev = respx.get(BASE + "/proxy/network/api/s/default/stat/device").mock(
        return_value=httpx.Response(200, json={"data": DEVICES}))
    respx.get(BASE + "/proxy/network/api/s/default/stat/health").mock(
        return_value=httpx.Response(200, json={"data": [{"subsystem": "wlan", "status": "ok", "num_user": 20}]}))
    return login, logout, dev


@respx.mock
async def test_login_read_logout():
    login, logout, dev = mock()
    snap = await plugin.fetch(S, "pw", NOW)
    assert login.calls.last.request.content == b'{"username":"homewatch","password":"pw","rememberMe":false}'
    assert dev.calls.last.request.headers["x-csrf-token"] == "CSRF" and logout.called
    assert [d["name"] for d in snap["devices"]] == ["AP1", "AP2"]


@respx.mock
async def test_bad_password():
    mock(login_status=400)
    with pytest.raises(ApiError, match="rejected"):
        await plugin.fetch(S, "bad", NOW)


@respx.mock
async def test_checks():
    mock()
    snap = await plugin.fetch(S, "pw", NOW)
    th = {d.id: Thresholds(d.warn, d.error, d.default_on) for d in plugin.checks}
    s = S | {"chosen": ["m1", "m2"]}
    res = {r.key.split(".")[1]: r for r in plugin.evaluate(s, th, (s, snap, "", NOW))}
    assert res["devices"].status == ERROR and "AP2 disconnected" in res["devices"].detail
    assert res["firmware"].status == WARNING and res["wifi"].status == OK
    s = S | {"chosen": ["m1"]}
    res = {r.key.split(".")[1]: r for r in plugin.evaluate(s, th, (s, snap, "", NOW))}
    assert all(r.status == OK for r in res.values())
