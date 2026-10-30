"""The Internet module as seen by the runtime and the web pages."""

from __future__ import annotations

import json
import os

from ...auth import audit
from .. import Module
from . import checks, collect, load, save, speedtest, valid_host


def _float(v, default=None):
    try:
        return float(v) if v not in (None, "") else default
    except ValueError:
        return default


class InternetModule(Module):
    id = checks.MODULE
    name = "Internet line"
    checks = checks.CHECKS
    description = ("Watches your internet line: public ping, gateway ping, DNS lookup and an optional speed test, so a "
                   "router or LAN problem can be told apart from the line being down.")
    setup = [
        ("text", "Nothing to create. Enter your contracted speed, tick the probes you want and check the targets."),
        ("text", "The speed test is off by default and uses Cloudflare, which needs nothing extra. To use Ookla, pick it as "
                 "the engine, accept its EULA and privacy policy, and press Save: homewatch then downloads Ookla's "
                 "program, which it isn't allowed to include."),
    ]

    def load(self, rt) -> dict:
        return load(rt.db, rt.cfg.gateway_hint, rt.cfg.dns_hint)

    def enabled(self, settings: dict) -> bool:
        return bool(settings["enabled"])

    async def readings(self, rt, settings, now):
        return await collect.collect(rt.metrics, rt.db, rt.cfg.site, now)

    def evaluate(self, settings, thresholds, readings):
        return checks.evaluate(settings, thresholds, readings)

    def loops(self, rt) -> list:
        return [rt.speed_loop()]

    def page_ctx(self, rt) -> dict:
        tests = rt.db.all("SELECT * FROM speedtests WHERE site=? ORDER BY ts DESC LIMIT 10", (rt.cfg.site,))
        return {"tests": tests, "ookla_found": os.access(speedtest.ookla_path(rt.cfg.ookla_dir), os.X_OK),
                "ookla_dir": rt.cfg.ookla_dir, "running": rt.speed_running}

    def save(self, rt, form: dict, user) -> str:
        old = self.load(rt)
        switched = "_switch" in form  # the wizard has no switch; turning the module off greys out (and so omits) the rest
        if switched and form.get("enabled") != "on":
            save(rt.db, old | {"enabled": False})
            rt.sync_telegraf()
            audit(rt.db, user.username, "internet", json.dumps({"enabled": False}))
            rt.wake.set()
            return ""
        targets = [t.strip() for t in form.get("public.targets", "").replace(",", " ").split() if t.strip()]
        if not 1 <= len(targets) <= 4 or not all(valid_host(t) for t in targets):
            return "Public targets: 1 to 4 IP addresses or host names"
        gw, dns_server, domain = (form.get("gateway.target", "").strip(), form.get("dns.server", "").strip(),
                                  form.get("dns.domain", "").strip())
        for label, v in (("Gateway", gw), ("DNS server", dns_server), ("DNS name", domain)):
            if v and not valid_host(v):
                return f"{label}: not a valid address or name"
        engine = form.get("speed.engine", "cloudflare")
        if engine not in ("cloudflare", "ookla"):
            return "Unknown speed test engine"
        interval = _float(form.get("speed.interval_h"), 3)
        if not 0.5 <= interval <= 48:
            return "Speed test interval: 0.5 to 48 hours"
        new = {
            "enabled": True if switched else old["enabled"],
            "public": {"enabled": form.get("public.enabled") == "on", "targets": targets},
            "gateway": {"enabled": form.get("gateway.enabled") == "on" and bool(gw), "target": gw},
            "dns": {"enabled": form.get("dns.enabled") == "on" and bool(dns_server), "server": dns_server,
                    "domain": domain or "cloudflare.com"},
            "speed": {"enabled": form.get("speed.enabled") == "on", "engine": engine, "interval_h": interval,
                      "ookla_accepted": form.get("speed.ookla_accepted") == "on"},
            "line": {"contract_down": max(_float(form.get("line.contract_down"), 0) or 0, 0),
                     "contract_up": max(_float(form.get("line.contract_up"), 0) or 0, 0)},
        }
        save(rt.db, new)
        rt.sync_telegraf()
        audit(rt.db, user.username, "internet", json.dumps({k: new[k] for k in new if new[k] != old.get(k)}))
        rt.wake.set()
        return ""


plugin = InternetModule()
