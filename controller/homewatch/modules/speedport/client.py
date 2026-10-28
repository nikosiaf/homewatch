"""Read-only client for Sercomm-based Telekom Speedport modems (e.g. Speedport Plus 2, VD4224BDTP).

Login, as the modem's own web page does it over HTTPS:
  csrf_token from login.html -> POST data/reset.json -> cookie login_uid=0.5
  -> POST data/login.json LoginName=<user>&LoginPWD=sha256hex(password); reply "1" = OK.
Values come from the JSON-RPC endpoint data/data.cgi as TR-181 parameters; only GET calls are made.
Logging in ends any other web session on the modem (the router UI allows one), and the modem slows
down further logins after a wrong password, so callers read rarely and back off after a rejection.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from urllib.parse import quote

import httpx

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


async def read(host: str, user: str, password: str, timeout: float = 20) -> Reading:
    base = f"https://{host}/"
    async with httpx.AsyncClient(verify=False, timeout=timeout, follow_redirects=True,  # noqa: S501 - self-signed
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
