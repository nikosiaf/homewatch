"""Speedport: reply parsing, the login flow against a mocked modem, and the checks."""

import hashlib
import json

import httpx
import pytest
import respx

from homewatch.modules import ERROR, OK, WARNING, Thresholds
from homewatch.modules.speedport import client
from homewatch.modules.speedport.checks import CHECKS, State, evaluate

NOW = 1_800_000_000.0
RESULTS = {
    "Device.DSL.Channel.1.DownstreamCurrRate": "109932", "Device.DSL.Channel.1.UpstreamCurrRate": "11000",
    "Device.DSL.Line.1.DownstreamMaxBitRate": "115000", "Device.DSL.Line.1.UpstreamMaxBitRate": "53700",
    "Device.DSL.Line.1.DownstreamNoiseMargin": "101", "Device.DSL.Line.1.UpstreamNoiseMargin": "310",
    "Device.DSL.Line.1.DownstreamAttenuation": "132", "Device.DSL.Line.1.UpstreamAttenuation": "139",
    "Device.DSL.Line.1.LastChange": "2419200", "Device.DSL.Line.1.LinkStatus": "Up",
    "Device.PPP.Interface.1.Status": "Down", "Device.DSL.Line.1.CurrentProfile": "17a",
    "Device.DeviceInfo.SoftwareVersion": "2.9.003.4", "Device.DSL.Line.1.UpstreamPower": "N/A",
}


def test_parse_scales_values():
    r = client.parse(RESULTS)
    assert r.values["sync_down_kbps"] == 109932 and r.values["snr_down_db"] == pytest.approx(10.1)
    assert r.status == {"link_up": 1, "ppp_up": 0}
    assert r.info["profile"] == "17a" and "power_up_dbm" not in r.values


def test_parse_empty_reply_is_an_error():
    with pytest.raises(ValueError):
        client.parse({})


@respx.mock
async def test_login_flow_and_logout():
    base = "https://modem.test/"
    respx.get(base + "login.html").mock(return_value=httpx.Response(200, text="var csrf_token = 'AAA';"))
    respx.post(url__startswith=base + "data/reset.json").mock(return_value=httpx.Response(200, text="{}"))
    login = respx.post(url__startswith=base + "data/login.json").mock(return_value=httpx.Response(200, text='"1"'))
    respx.get(base + "overview.html").mock(return_value=httpx.Response(200, text="csrf_token = 'BBB';"))
    data = respx.post(url__startswith=base + "data/data.cgi").mock(
        return_value=httpx.Response(200, json=[{"result": {k: v}} for k, v in RESULTS.items()]))
    logout = respx.post(url__startswith=base + "data/logout.json").mock(return_value=httpx.Response(200))
    r = await client.read("modem.test", "admin", "pw")
    assert r.values["sync_up_kbps"] == 11000
    body = login.calls.last.request.content.decode()
    assert body == f"LoginName=admin&LoginPWD={hashlib.sha256(b'pw').hexdigest()}"
    assert "login_uid=0.5" in login.calls.last.request.headers["cookie"]
    assert "csrf_token=BBB" in str(data.calls.last.request.url)
    assert all(c["method"] == "GET" for c in json.loads(data.calls.last.request.content))  # read-only
    assert logout.called


@respx.mock
async def test_rejected_login():
    base = "https://modem.test/"
    respx.get(base + "login.html").mock(return_value=httpx.Response(200, text="csrf_token = 'A';"))
    respx.post(url__startswith=base + "data/reset.json").mock(return_value=httpx.Response(200))
    respx.post(url__startswith=base + "data/login.json").mock(return_value=httpx.Response(200, text='"0"'))
    with pytest.raises(client.LoginRejected):
        await client.read("modem.test", "admin", "wrong")


def th(**over):
    t = {c.id: Thresholds(c.warn, c.error, c.default_on) for c in CHECKS}
    t.update(over)
    return t


def state(**over):
    r = client.parse(RESULTS)
    st = State(now=NOW, interval=900, last_ok=NOW - 300, values=r.values, status=r.status, resyncs_24h=0,
               contract_down=100, contract_up=10)
    for k, v in over.items():
        setattr(st, k, v)
    return st


def by_id(results):
    return {r.key.split(".")[1]: r for r in results}


def test_healthy_line():
    res = by_id(evaluate({}, th(), state()))
    assert {k: r.status for k, r in res.items()} == {"readable": OK, "link": OK, "sync": OK, "snr": OK,
                                                    "resyncs": OK}
    assert "ppp" not in res  # off by default (bridge setups)
    assert "109.9 / 11.0 Mbit/s" in res["sync"].detail


def test_link_down_suppresses_line_quality():
    st = state(status={"link_up": 0})
    res = by_id(evaluate({}, th(), st))
    assert res["link"].status == ERROR
    assert res["sync"].suppressed_by == "speedport.link" and res["snr"].effective == OK


def test_unreadable_modem():
    res = by_id(evaluate({}, th(), state(last_ok=NOW - 3 * 900, last_error="ConnectTimeout")))
    assert res["readable"].status == ERROR and "ConnectTimeout" in res["readable"].detail
    assert res["link"].suppressed_by == "speedport.readable"
    res = by_id(evaluate({}, th(), state(last_ok=None, last_error="", values={}, status={})))
    assert res["readable"].status == OK and set(res) == {"readable"}


def test_rejected_login_is_an_error():
    res = by_id(evaluate({}, th(), state(rejected=True)))
    assert res["readable"].status == ERROR and "rejected" in res["readable"].detail


def test_low_snr_sync_and_resyncs():
    v = dict(state().values, snr_down_db=5.2, sync_down_kbps=40000)
    res = by_id(evaluate({}, th(), state(values=v, resyncs_24h=6)))
    assert res["snr"].status == WARNING
    assert res["sync"].status == ERROR  # 40 % of the contract, below the 50 % error level
    assert res["resyncs"].status == ERROR


def test_sync_without_contract():
    res = by_id(evaluate({}, th(), state(contract_down=0, contract_up=0)))
    assert res["sync"].status == OK and "no contracted speed" in res["sync"].detail
