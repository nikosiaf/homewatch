"""The Speedport module: settings, background reader, checks."""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from ...auth import audit
from ...metrics import MetricsError, line
from .. import DevEvent, Module, booted
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
    icon = "modem"
    headline = ("snr", "SNR margin")
    description = ("Reads the DSL line from your provider's modem: sync rate, attainable rate, SNR margin, attenuation, "
                   "line uptime and resyncs. Values are only read, never changed.")
    setup = [
        ("text", "The modem has only its admin login, so homeWatch needs that password. It is stored encrypted and never "
                 "shown again."),
        ("text", "Each reading logs in, which ends any open browser session on the modem's web page. Keep the interval at "
                 "15 minutes or more."),
        ("text", "After a rejected login the modem slows down further attempts, so homeWatch waits 6 hours (or until you "
                 "save new credentials)."),
        ("text", "The modem's certificate is self-signed, so homeWatch trusts the one it sees at the first reading and "
                 "logs in only to that one. If it changes, homeWatch stops logging in until you accept the new one."),
        ("text", "This host must be able to reach the modem's address. Behind a separate router or firewall that may need "
                 "a rule there."),
    ]
    checks = checks.CHECKS

    def load(self, rt) -> dict:
        return {**DEFAULTS, **(rt.db.get(KEY) or {})}

    def is_set_up(self, rt) -> bool:
        return rt.db.get(KEY) is not None

    def transport(self, rt):
        if self.state(rt).get("cert"):
            return "HTTPS, certificate pinned", True
        return "HTTPS, certificate not pinned yet", False

    def reading_ts(self, rt) -> float | None:
        return self.state(rt).get("last_ok")

    def refresh_soon(self, rt) -> None:
        rt.db.set(STATE, {**self.state(rt), "last_try": 0})  # the reader loop looks every 30 s

    def device_events(self, rt, prev):
        st = self.state(rt)
        ts, v, status = st.get("last_ok"), st.get("values") or {}, st.get("status") or {}
        out = []
        if not ts:
            return out
        booted(prev, "modem", ts - v["modem_uptime_s"] if v.get("modem_uptime_s") else None, "modem", "The modem", out)
        link = status.get("link_up")
        if link == 0 and prev.get("link", 1):
            out.append(DevEvent(ts, "line_down", "modem", "DSL line down", exact=False))
        if link is not None:
            prev["link"] = link
        if link and v.get("line_up_s") is not None:
            since, old = ts - v["line_up_s"], prev.get("line")
            if old is not None and since - old > 120:
                down, up = v.get("sync_down_kbps"), v.get("sync_up_kbps")
                speed = f" at {down / 1000:.0f}/{up / 1000:.0f} Mbit/s" if down and up else ""
                out.append(DevEvent(since, "line_resync", "modem", f"DSL line re-synced{speed}"))
            if old is None or abs(since - old) > 120:
                prev["line"] = since
        return out

    def secret_keys(self) -> list[str]:
        return [SECRET]

    def forget_credentials(self, rt) -> None:
        super().forget_credentials(rt)
        st = self.state(rt)
        for k in ("cert", "cert_new", "cert_changed"):  # another device: trust starts over
            st.pop(k, None)
        rt.db.set(STATE, st)

    def imported(self, settings: dict) -> tuple[dict, str]:
        host = settings.get("host")
        if not isinstance(host, str) or not valid_host(host.strip()):
            return settings, "modem address: an IP address or host name"
        if settings.get("model", "speedport-plus-2") not in MODELS:
            return settings, "unknown modem model"
        interval = settings.get("interval_min", 15)
        if isinstance(interval, bool) or not isinstance(interval, int) or not 5 <= interval <= 240:
            return settings, "read interval: 5 to 240 minutes"
        return settings | {"host": host.strip()}, ""

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
                            cert_changed=bool(st.get("cert_changed")),
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
            if not st.get("cert"):
                st["cert"] = await client.fetch_certificate(s["host"])
                audit(rt.db, "homewatch", "speedport", f"trusting the modem's certificate {client.fingerprint(st['cert'])}")
            r = await client.read(s["host"], s["username"], password, st["cert"])
        except client.CertificateRejected as e:
            try:
                st["cert_new"] = await client.fetch_certificate(s["host"])
            except (OSError, ValueError):
                st.pop("cert_new", None)
            st.update(last_error=f"{e}", rejected=False, cert_changed=True)
            rt.db.set(STATE, st)
            log.warning("speedport: the modem's certificate changed, not logging in")
            return f"{e}. homeWatch did not log in."
        except client.LoginRejected as e:
            st.update(last_error=str(e), rejected=True, backoff_until=now + BACKOFF)
            rt.db.set(STATE, st)
            log.warning("speedport: login rejected, next try in 6 h")
            return f"{e}. homeWatch waits 6 hours before trying again (or until you save new credentials)."
        except (httpx.HTTPError, OSError, ValueError) as e:
            msg = f"{type(e).__name__}: {e}"[:200] if str(e) else type(e).__name__
            st.update(last_error=msg, rejected=False)
            rt.db.set(STATE, st)
            return msg
        st.pop("cert_new", None)
        st.update(last_ok=now, last_error="", rejected=False, cert_changed=False, backoff_until=0, values=r.values,
                  status=r.status, info=r.info)
        rt.db.set(STATE, st)
        try:
            await rt.metrics.write([line("hw_dsl", {"site": rt.cfg.site}, {**r.values, **r.status}, now)])
        except MetricsError as e:
            log.warning("%s", e)
        return ""

    def trust_new_certificate(self, rt, fp: str, user) -> str:
        """Accept the changed certificate the admin was shown, and no other."""
        st = self.state(rt)
        new = st.get("cert_new")
        if not new or client.fingerprint(new) != fp:
            return "The modem's certificate changed again, check the new fingerprint"
        st.update(cert=new, cert_changed=False, last_error="", last_try=0)
        del st["cert_new"]
        rt.db.set(STATE, st)
        audit(rt.db, user.username, "speedport", f"trusting the modem's new certificate {fp}")
        rt.wake.set()
        return ""

    def page_ctx(self, rt) -> dict:
        st = self.state(rt)
        return {"st": st, "has_password": rt.secrets.has(SECRET), "models": MODELS,
                "configured": rt.db.get(KEY) is not None,
                "cert_fp": client.fingerprint(st["cert"]) if st.get("cert") else "",
                "cert_new_fp": client.fingerprint(st["cert_new"]) if st.get("cert_new") else ""}

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
        if host != old["host"]:  # another device: trust starts over
            for k in ("cert", "cert_new", "cert_changed"):
                st.pop(k, None)
        if form.get("password"):
            rt.secrets.put(SECRET, form["password"])
            st.update(rejected=False, backoff_until=0, last_try=0)  # new credentials: try right away
        rt.db.set(STATE, st)
        audit(rt.db, user.username, "speedport", f"enabled={new['enabled']} host={host} user={username} "
                                                 f"interval={interval}" + (" password=replaced" if form.get("password") else ""))
        rt.wake.set()
        return ""


plugin = SpeedportModule()
