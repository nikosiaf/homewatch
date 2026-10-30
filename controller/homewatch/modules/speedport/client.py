"""Read-only client for Sercomm-based Telekom Speedport modems (e.g. Speedport Plus 2, VD4224BDTP).

Login, as the modem's own web page does it over HTTPS:
  csrf_token from login.html -> POST data/reset.json -> cookie login_uid=0.5
  -> POST data/login.json LoginName=<user>&LoginPWD=sha256hex(password); reply "1" = OK.
Values come from the JSON-RPC endpoint data/data.cgi as TR-181 parameters; only GET calls are made.
Logging in ends any other web session on the modem (the router UI allows one), and the modem slows
down further logins after a wrong password, so callers read rarely and back off after a rejection.

The login hash is reusable, so it only ever goes to the certificate the caller pinned: the modem's is
self-signed and can't be checked any other way. Redirects are refused, so the login stays on this host.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from urllib.parse import quote

import httpx

from ..pinning import fetch_certificate, fingerprint, pinned_context  # noqa: F401 - the module uses these through client

LINE, CHAN = "Device.DSL.Line.1.", "Device.DSL.Channel.1."
# TR-181 parameter -> (field, scale). Rates in kbit/s; margins, attenuation and power in 0.1 dB(m).
NUMERIC = {
    CHAN + "DownstreamCurrRate": ("sync_down_kbps", 1), CHAN + "UpstreamCurrRate": ("sync_up_kbps", 1),
    LINE + "DownstreamMaxBitRate": ("attainable_down_kbps", 1), LINE + "UpstreamMaxBitRate": ("attainable_up_kbps", 1),
    LINE + "DownstreamNoiseMargin": ("snr_down_db", 0.1), LINE + "UpstreamNoiseMargin": ("snr_up_db", 0.1),
    LINE + "DownstreamAttenuation": ("atten_down_db", 0.1), LINE + "UpstreamAttenuation": ("atten_up_db", 0.1),
    LINE + "DownstreamPower": ("power_down_dbm", 0.1), LINE + "UpstreamPower": ("power_up_dbm", 0.1),
    LINE + "LastChange": ("line_up_s", 1), "Device.DeviceInfo.UpTime": ("modem_uptime_s", 1),
}
STATUS = {LINE + "LinkStatus": "link_up", "Device.PPP.Interface.1.Status": "ppp_up"}
INFO = {LINE + "CurrentProfile": "profile", LINE + "StandardUsed": "standard",
        "Device.DeviceInfo.SoftwareVersion": "firmware", "Device.DeviceInfo.ModelName": "model"}


class LoginRejected(Exception):
    pass


class CertificateRejected(Exception):
    """The modem did not present the pinned certificate; nothing was sent to it."""


class RedirectRefused(httpx.HTTPError):
    pass


@dataclass
class Reading:
    values: dict[str, float] = field(default_factory=dict)  # numeric fields, scaled
    status: dict[str, int] = field(default_factory=dict)  # link_up / ppp_up: 1 or 0
    info: dict[str, str] = field(default_factory=dict)


def _csrf(html: str) -> str:
    m = re.search(r"csrf_token\s*=\s*'([^']*)'", html)
    return m.group(1) if m else ""


def parse(results: dict) -> Reading:
    r = Reading()
    for k, (name, scale) in NUMERIC.items():
        try:
            r.values[name] = float(results[k]) * scale
        except (KeyError, TypeError, ValueError):
            pass
    for k, name in STATUS.items():
        if k in results and results[k] not in (None, "", "N/A"):
            r.status[name] = 1 if str(results[k]).lower() == "up" else 0
    for k, name in INFO.items():
        if results.get(k) not in (None, "", "N/A"):
            r.info[name] = str(results[k])[:60]
    if not r.values and not r.status:
        raise ValueError("the modem answered without any DSL values")
    return r


async def _refuse_redirect(response: httpx.Response) -> None:
    if response.is_redirect:
        raise RedirectRefused(f"the modem answered {response.url.path} with a redirect")


async def read(host: str, user: str, password: str, cert: str, timeout: float = 20) -> Reading:
    """Log in and read, only over a connection that presents `cert` (PEM)."""
    try:
        return await _read(host, user, password, cert, timeout)
    except httpx.ConnectError as e:
        if "CERTIFICATE_VERIFY_FAILED" in str(e):
            raise CertificateRejected("the modem's certificate is not the trusted one") from e
        raise


async def _read(host: str, user: str, password: str, cert: str, timeout: float) -> Reading:
    base = f"https://{host}/"
    async with httpx.AsyncClient(verify=pinned_context(cert), timeout=timeout, follow_redirects=False,
                                 event_hooks={"response": [_refuse_redirect]},
                                 headers={"User-Agent": "homewatch", "Referer": base + "login.html"}) as http:
        tok = _csrf((await http.get(base + "login.html")).text)

        def q() -> str:
            return f"?_={int(time.time() * 1000)}&csrf_token={tok}"

        form = {"Content-Type": "application/x-www-form-urlencoded"}
        await http.post(base + "data/reset.json" + q(), content=b"chk_sys_busy=", headers=form)
        http.cookies.set("login_uid", "0.5", domain=host, path="/")
        body = f"LoginName={quote(user)}&LoginPWD={hashlib.sha256(password.encode()).hexdigest()}"
        r = await http.post(base + "data/login.json" + q(), content=body.encode(), headers=form)
        if not r.text.strip().startswith(('"1"', "[ ]", "1")):
            raise LoginRejected("the modem rejected the login")
        try:
            tok = _csrf((await http.get(base + "overview.html")).text) or tok
            names = list(NUMERIC) + list(STATUS) + list(INFO)
            req = [{"jsonrpc": "2.0", "method": "GET", "params": n, "id": i + 1} for i, n in enumerate(names)]
            resp = await http.post(base + "data/data.cgi" + q(), content=json.dumps(req).encode(),
                                   headers={"Content-Type": "application/json;charset=UTF-8"})
            results: dict = {}
            for item in resp.json():
                results.update(item.get("result") or {})
            return parse(results)
        finally:
            try:
                await http.post(base + "data/logout.json" + q(), content=b"logout=1", headers=form)
            except httpx.HTTPError:
                pass
