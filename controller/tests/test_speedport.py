"""Speedport: reply parsing, the login flow against a mocked modem, and the checks."""

import asyncio
import datetime
import hashlib
import json
import ssl

import httpx
import pytest
import respx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

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


def self_signed():
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "modem")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True).sign(key, hashes.SHA256()))
    return (cert.public_bytes(serialization.Encoding.PEM).decode(),
            key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                              serialization.NoEncryption()).decode())


CERT_A, KEY_A = self_signed()
CERT_B, _ = self_signed()


class FakeModem:
    """A real TLS listener on 127.0.0.1 presenting CERT_A; records everything it receives."""

    def __init__(self, tmp_path, answer):
        self.answer, self.received, self.path = answer, b"", tmp_path / "modem.pem"
        self.path.write_text(CERT_A + KEY_A)

    async def handle(self, reader, writer):
        self.received += await reader.readuntil(b"\r\n\r\n")
        writer.write(self.answer)
        await writer.drain()
        writer.close()

    async def __aenter__(self):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(self.path)
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0, ssl=ctx)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *_):
        self.server.close()


REDIRECT = b"HTTP/1.1 302 Found\r\nLocation: https://elsewhere.test/\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"


async def test_wrong_certificate_gets_no_request(tmp_path):
    async with FakeModem(tmp_path, REDIRECT) as modem:
        with pytest.raises(client.CertificateRejected):
            await client.read(f"127.0.0.1:{modem.port}", "admin", "pw", CERT_B)
    assert modem.received == b""


async def test_redirect_is_refused_before_the_login(tmp_path):
    async with FakeModem(tmp_path, REDIRECT) as modem:
        with pytest.raises(client.RedirectRefused):
            await client.read(f"127.0.0.1:{modem.port}", "admin", "pw", CERT_A)
    assert modem.received.startswith(b"GET /login.html") and b"LoginPWD" not in modem.received


@respx.mock
async def test_redirect_at_login_is_not_followed():
    base = "https://modem.test/"
    respx.get(base + "login.html").mock(return_value=httpx.Response(200, text="csrf_token = 'A';"))
    respx.post(url__startswith=base + "data/reset.json").mock(return_value=httpx.Response(200))
    respx.post(url__startswith=base + "data/login.json").mock(
        return_value=httpx.Response(307, headers={"Location": "https://elsewhere.test/steal"}))
    elsewhere = respx.post("https://elsewhere.test/steal").mock(return_value=httpx.Response(200, text='"1"'))
    with pytest.raises(client.RedirectRefused):
        await client.read("modem.test", "admin", "pw", CERT_A)
    assert not elsewhere.called


def test_fingerprint_format():
    fp = client.fingerprint(CERT_A)
    assert len(fp) == 95 and fp != client.fingerprint(CERT_B)


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
    r = await client.read("modem.test", "admin", "pw", CERT_A)
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
        await client.read("modem.test", "admin", "wrong", CERT_A)


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


def test_changed_certificate_is_an_error_at_once():
    res = by_id(evaluate({}, th(), state(cert_changed=True, last_error="the modem's certificate is not the trusted one")))
    assert res["readable"].status == ERROR and "certificate" in res["readable"].detail
    assert res["link"].suppressed_by == "speedport.readable"


def test_low_snr_sync_and_resyncs():
    v = dict(state().values, snr_down_db=5.2, sync_down_kbps=40000)
    res = by_id(evaluate({}, th(), state(values=v, resyncs_24h=6)))
    assert res["snr"].status == WARNING
    assert res["sync"].status == ERROR  # 40 % of the contract, below the 50 % error level
    assert res["resyncs"].status == ERROR


def test_sync_without_contract():
    res = by_id(evaluate({}, th(), state(contract_down=0, contract_up=0)))
    assert res["sync"].status == OK and res["sync"].detail == "109.9 / 11.0 Mbit/s"
