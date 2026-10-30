"""The Speedport module: settings, background reader, checks."""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from ...auth import audit
from ...metrics import MetricsError, line
from .. import Module
from ..internet import load as load_internet
from ..internet import valid_host
from . import checks, client

log = logging.getLogger("homewatch.speedport")

KEY = "module.speedport"
STATE = "speedport.state"
SECRET = "module.speedport.password"
BACKOFF = 6 * 3600  # after a rejected login: the modem slows down further attempts
DEFAULTS = {"enabled": False, "model": "speedport-plus-2", "host": "192.168.1.1", "username": "admin",
            "interval_min": 15}
# supported modems; all use the Sercomm login and TR-181 JSON-RPC reads in client.py
MODELS = {
    "speedport-plus-2": {
        "name": "Telekom Speedport Plus 2 (Sercomm VD4224BDTP / VD4224BDT)",
        "about": "The VD4224BDTP (or VD4224BDT) model is the Speedport Plus 2 broadband modem-router by Sercomm, "
                 "provided by TELEKOM (formerly COSMOTE) for VDSL and FTTH (Fiber to the Home) connections in Greece.",
    },
}


class SpeedportModule(Module):
    id = checks.MODULE
    name = "ISP modem"
    description = ("Reads the DSL line from your provider's modem: sync rate, attainable rate, SNR margin, attenuation, "
                   "line uptime and resyncs. Values are only read, never changed.")
    setup = [
        ("text", "The modem has only its admin login, so homewatch needs that password. It is stored encrypted and never "
                 "shown again."),
        ("text", "Each reading logs in, which ends any open browser session on the modem's web page. Keep the interval at "
                 "15 minutes or more."),
        ("text", "After a rejected login the modem slows down further attempts, so homewatch waits 6 hours (or until you "
                 "save new credentials)."),
        ("text", "This host must be able to reach the modem's address. Behind a separate router or firewall that may need "
                 "a rule there."),
    ]
    checks = checks.CHECKS

    def load(self, rt) -> dict:
        return {**DEFAULTS, **(rt.db.get(KEY) or {})}

    def state(self, rt) -> dict:
        return rt.db.get(STATE) or {}

    async def readings(self, rt, settings, now):
        st = self.state(rt)
        inet = load_internet(rt.db)["line"]
        resyncs = None
        if st.get("last_ok"):
            res = await rt.metrics.query(f'resets(hw_dsl_line_up_s{{site="{rt.cfg.site}"}}[24h])', at=now)
            resyncs = res[0][2] if res else None
        return checks.State(now=now, interval=settings["interval_min"] * 60, last_ok=st.get("last_ok"),
                            last_error=st.get("last_error", ""), rejected=st.get("rejected", False),
                            values=st.get("values", {}), status=st.get("status", {}), resyncs_24h=resyncs,
                            contract_down=float(inet.get("contract_down") or 0),
                            contract_up=float(inet.get("contract_up") or 0))

    def evaluate(self, settings, thresholds, readings):
        return checks.evaluate(settings, thresholds, readings)

    def loops(self, rt) -> list:
        return [self.loop(rt)]

    async def loop(self, rt) -> None:
        await asyncio.sleep(20)
        while True:
            s = self.load(rt)
            st = self.state(rt)
            now = time.time()
            due = now - st.get("last_try", 0) >= s["interval_min"] * 60 and now >= st.get("backoff_until", 0)
            if s["enabled"] and due:
                await self.read_now(rt)
            await asyncio.sleep(30)

    async def read_now(self, rt) -> str:
        """Read the modem once; return '' or an error message."""
        s = self.load(rt)
        password = rt.secrets.get(SECRET) or ""
        st = self.state(rt)
        now = time.time()
        st["last_try"] = now
        if not password:
            st.update(last_error="no password set", rejected=False)
            rt.db.set(STATE, st)
            return "No password set"
        try:
            r = await client.read(s["host"], s["username"], password)
        except client.LoginRejected as e:
            st.update(last_error=str(e), rejected=True, backoff_until=now + BACKOFF)
            rt.db.set(STATE, st)
            log.warning("speedport: login rejected, next try in 6 h")
            return f"{e}. homewatch waits 6 hours before trying again (or until you save new credentials)."
        except (httpx.HTTPError, OSError, ValueError) as e:
            msg = f"{type(e).__name__}: {e}"[:200] if str(e) else type(e).__name__
            st.update(last_error=msg, rejected=False)
            rt.db.set(STATE, st)
            return msg
        st.update(last_ok=now, last_error="", rejected=False, backoff_until=0, values=r.values, status=r.status,
                  info=r.info)
        rt.db.set(STATE, st)
        try:
            await rt.metrics.write([line("hw_dsl", {"site": rt.cfg.site}, {**r.values, **r.status}, now)])
        except MetricsError as e:
            log.warning("%s", e)
        return ""

    def page_ctx(self, rt) -> dict:
        return {"st": self.state(rt), "has_password": rt.secrets.has(SECRET), "models": MODELS,
                "configured": rt.db.get(KEY) is not None}

    def save(self, rt, form: dict, user) -> str:
        old = self.load(rt)
        # greyed-out fields (module off, no JavaScript) are not sent: keep what was there
        form = {**{k: str(old[k]) for k in ("model", "host", "username", "interval_min")}, **form}
        if not form.get("model"):
            if rt.db.get(KEY) is None:
                return "Choose your modem first"
            # "None": forget the modem: settings, password and last reading; its checks close silently
            rt.db.run("DELETE FROM settings WHERE key IN (?, ?)", (KEY, STATE))
            rt.secrets.delete(SECRET)
            audit(rt.db, user.username, "speedport", "modem removed")
            rt.wake.set()
            return ""
        host = form.get("host", "").strip()
        if not valid_host(host):
            return "Modem address: an IP address or host name"
        try:
            interval = int(form.get("interval_min", "15"))
        except ValueError:
            return "Read interval: whole minutes"
        if not 5 <= interval <= 240:
            return "Read interval: 5 to 240 minutes"
        username = form.get("username", "").strip()[:64]
        model = form.get("model", "speedport-plus-2")
        if model not in MODELS:
            return "Unknown modem model"
        new = {"enabled": form.get("enabled") == "on", "model": model, "host": host, "username": username,
               "interval_min": interval}
        if new["enabled"] and not (form.get("password") or rt.secrets.has(SECRET)):
            return "Enter the modem password to switch the module on"
        rt.db.set(KEY, new)
        st = self.state(rt)
        if form.get("password"):
            rt.secrets.put(SECRET, form["password"])
            st.update(rejected=False, backoff_until=0, last_try=0)  # new credentials: try right away
        rt.db.set(STATE, st)
        audit(rt.db, user.username, "speedport", f"enabled={new['enabled']} host={host} user={username} "
                                                 f"interval={interval}" + (" password=replaced" if form.get("password") else ""))
        rt.wake.set()
        return ""


plugin = SpeedportModule()
