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
    respx.get(BASE + "/proxy/network/api/s/default/stat/alluser").mock(
        return_value=httpx.Response(200, json={"data": [{"mac": "cc:01", "hostname": "door", "last_uplink_mac": "aa:01",
                                                         "last_ip": "192.168.10.9", "last_seen": 1800000000}]}))
    respx.get(BASE + "/proxy/network/api/s/default/stat/sta").mock(
        return_value=httpx.Response(200, json={"data": [{"mac": "cc:00", "hostname": "phone", "ap_mac": "aa:01",
                                                         "is_wired": False}]}))
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


def test_device_cpu():
    from homewatch.modules.unifi.plugin import plugin as uni
    snap = {"devices": [{"mac": "aa", "name": "AP1", "model": "U6", "type": "uap", "state": 1, "version": "1",
                         "upgradable": False, "upgrade_to": None, "uptime": 99999, "clients": 3, "adopted": True,
                         "cpu": 93.0}], "health": [], "_summary": "1"}
    th = {d.id: Thresholds(d.warn, d.error, d.default_on) for d in uni.checks}
    r = {x.key.split(".")[1]: x for x in uni.check({"chosen": ["aa"]}, th, snap, 0)}
    assert r["cpu"].status == WARNING and "AP1 93 %" in r["cpu"].detail


def _watch(entries, clients=(), history=(), ok=True, now=10000.0):
    snap = {"devices": [], "health": [], "clients": list(clients), "history": list(history), "clients_ok": ok}
    th = {d.id: Thresholds(d.warn, d.error, d.default_on) for d in plugin.checks}
    r = {x.key.split(".")[1]: x for x in plugin.check({"must_online": entries}, th, snap, now)}
    return r["watched"]


def test_watched_matching_by_mac_and_name():
    hist = [{"mac": "AA:BB:CC:00:00:01", "name": "Camera garden", "last_seen": 10000 - 47 * 60},
            {"mac": "aa:bb:cc:00:00:02", "name": "Doorbell", "last_seen": 10000 - 60}]
    r = _watch("camera GARDEN\nAA-BB-CC-00-00-02", history=hist)
    assert r.status == WARNING and r.detail == "camera GARDEN not seen for 47 min"
    r = _watch("aabb.cc00.0001", history=hist)
    assert r.status == WARNING and "not seen for 47 min" in r.detail
    # connected right now counts as seen, whatever the history says
    r = _watch("Camera garden", clients=[{"mac": "aa:bb:cc:00:00:01", "name": ""}], history=hist)
    assert r.status == OK and r.detail == "1 watched devices seen"


def test_watched_30_minute_boundary():
    hist = [{"mac": "aa:bb:cc:00:00:01", "name": "cam", "last_seen": 10000 - 30 * 60}]
    assert _watch("cam", history=hist).status == OK
    hist[0]["last_seen"] -= 1
    assert _watch("cam", history=hist).status == WARNING


def test_watched_unknown_entry_and_empty():
    r = _watch("nosuch\n\n", history=[{"mac": "aa:bb:cc:00:00:01", "name": "cam", "last_seen": 10000}])
    assert r.status == WARNING and r.detail == "not found: nosuch"
    r = _watch("")
    assert r.status == OK and r.detail == "no devices listed" and not r.unknown


def test_watched_unreadable_clients():
    r = _watch("cam", ok=False)
    assert r.status == OK and r.unknown and "not readable" in r.detail


@respx.mock
async def test_watched_clients_read_fails_and_controller_down():
    mock()
    respx.get(BASE + "/proxy/network/api/s/default/stat/alluser").mock(return_value=httpx.Response(500))
    snap = await plugin.fetch(S, "pw", NOW)
    assert snap["clients_ok"] is False
    th = {d.id: Thresholds(d.warn, d.error, d.default_on) for d in plugin.checks}
    s = S | {"must_online": "door"}
    res = {r.key.split(".")[1]: r for r in plugin.evaluate(s, th, (s, None, "can't reach UniFi", NOW))}
    assert res["watched"].status == OK and res["watched"].suppressed_by == "unifi.api"


def test_watched_back_to_ok():
    hist = [{"mac": "aa:bb:cc:00:00:01", "name": "cam", "last_seen": 0}]
    assert _watch("cam", history=hist).status == WARNING
    assert _watch("cam", clients=[{"mac": "aa:bb:cc:00:00:01", "name": "cam"}], history=hist).status == OK


def test_watched_error_limit():
    hist = [{"mac": "aa:bb:cc:00:00:01", "name": "cam", "last_seen": 10000 - 120 * 60}]
    assert _watch("cam", history=hist).status == WARNING  # no ERROR limit by default: WARNING only
    snap = {"devices": [], "health": [], "clients": [], "history": hist, "clients_ok": True}
    th = {d.id: Thresholds(d.warn, d.error, d.default_on) for d in plugin.checks}
    th["watched"] = Thresholds(30, 60)
    r = {x.key.split(".")[1]: x for x in plugin.check({"must_online": "cam"}, th, snap, 10000.0)}
    assert r["watched"].status == ERROR and r["watched"].detail == "cam not seen for 120 min"
    hist[0]["last_seen"] = 10000 - 45 * 60
    r = {x.key.split(".")[1]: x for x in plugin.check({"must_online": "cam"}, th, snap, 10000.0)}
    assert r["watched"].status == WARNING


def test_watched_name_picks_newest_mac():
    # an old, replaced camera and the current one share a name; the current one is online
    hist = [{"mac": "aa:bb:cc:00:00:01", "name": "Camera garden", "last_seen": 1000},
            {"mac": "aa:bb:cc:00:00:02", "name": "Camera garden", "last_seen": 10000 - 60}]
    assert _watch("Camera garden", history=hist).status == OK
    assert _watch("Camera garden", history=hist[::-1]).status == OK
    r = _watch("Camera garden", clients=[{"mac": "aa:bb:cc:00:00:02", "name": "Camera garden"}], history=hist)
    assert r.status == OK
