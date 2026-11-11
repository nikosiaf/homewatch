"""UniFi Network (UniFi OS): log in as a local view-only user, read devices and health, log out."""

from __future__ import annotations

import re

import httpx

from .. import ERROR, OK, WARNING, CheckDef, CheckResult, DevEvent, booted, level, reboots
from ...metrics import lines, num
from ..pinning import verify_for
from ..apibase import SELF_SIGNED, why, ApiError, ApiModule, Choice, Field, Table, https_url

CHECKS = [
    CheckDef("api", "Controller reachable", "api", help="ERROR when homeWatch can't log in to UniFi or read it."),
    CheckDef("devices", "Devices online", "api", help="ERROR when a device you chose is disconnected or unreachable."),
    CheckDef("cpu", "CPU", "api", unit="%", warn=90, editable=("warn", "error"),
             help="Busiest watched device's CPU use, as UniFi reports it."),
    CheckDef("wifi", "Wi-Fi", "api", help="WARNING when UniFi reports the WLAN subsystem as not OK."),
    CheckDef("reboots", "Restarts (24 h)", "api", warn=2, editable=("warn",),
             help="Device restarts in the last 24 hours, all watched devices together. Each one is also an event and "
                  "a Grafana marker; WARNING at this many."),
    CheckDef("firmware", "Firmware", "api", unit="days", warn=14, editable=("warn",), todo=True,
             help="A device has a firmware update. Listed under To do; WARNING once it has waited this many days."),
]
STATE = {0: "disconnected", 1: "connected", 2: "pending adoption", 4: "upgrading", 5: "provisioning",
         6: "heartbeat missed", 7: "adopting", 9: "adoption failed", 10: "isolated", 11: "isolated"}
def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


_SITE = re.compile(r"^[a-zA-Z0-9_-]+$")


class UniFiModule(ApiModule):
    id = "unifi"
    name = "UniFi"
    icon = "wifi"
    headline = ("cpu", "CPU")
    checks = CHECKS
    description = ("Watches the devices of a UniFi Network application (UniFi OS Server or a UniFi console) with a "
                   "view-only local user: devices online, Wi-Fi health, restarts and firmware updates.")
    setup = [
        ("text", "In UniFi: Admins & Users → Create New Admin. Name it homewatch, tick \"Restrict to local access "
                 "only\", give it the View Only role for Network and no access to other applications."),
        ("text", "Use the address you open UniFi with (UniFi OS Server usually uses port 11443)."),
    ]
    fields = [
        Field("url", "UniFi address", "https://192.168.1.10:11443"),
        Field("site", "Site", "default", default="default"),
        Field("username", "User name", "homewatch", default="homewatch"),
        Field("verify", "Check the HTTPS certificate", kind="check", default=True,
              help=SELF_SIGNED),
    ]
    secret_label = "Password"
    choices_title = "Devices to watch"
    choices_help = "homeWatch alerts when one of these disconnects. All adopted devices are ticked when you connect."
    more_title = "Additional configuration"
    more_help = "After connecting: the devices homeWatch watches."
    list_search = False

    def validate(self, form, old):
        out, err = super().validate(form, old)
        if err:
            return out, err
        url = https_url(out["url"])
        if not url:
            return out, "UniFi address: e.g. https://192.168.1.10:11443"
        if not _SITE.match(out["site"] or ""):
            return out, "Site: the site's short name, usually default"
        if not out["username"]:
            return out, "Enter the user name"
        return out | {"url": url}, ""

    async def fetch(self, s, secret, now):
        async with httpx.AsyncClient(verify=verify_for(s, s["url"]), timeout=20) as http:  # noqa: S501
            try:
                r = await http.post(f"{s['url']}/api/auth/login",
                                    json={"username": s["username"], "password": secret, "rememberMe": False})
            except httpx.HTTPError as e:
                raise ApiError(f"can't reach UniFi ({why(e)})") from None
            if r.status_code in (400, 401, 403):
                raise ApiError("UniFi rejected the user name or password")
            if r.status_code >= 300:
                raise ApiError(f"UniFi login answered HTTP {r.status_code}")
            csrf = r.headers.get("x-csrf-token") or r.headers.get("x-updated-csrf-token") or ""
            headers = {"X-CSRF-Token": csrf} if csrf else {}
            base = f"{s['url']}/proxy/network/api/s/{s['site']}"
            try:
                devices = await self._get(http, f"{base}/stat/device", headers)
                health = await self._get(http, f"{base}/stat/health", headers)
                try:  # the clients: which access point each one uses (for the map)
                    stations = await self._get(http, f"{base}/stat/sta", headers)
                except ApiError:
                    stations = []
                try:  # every client UniFi knows, with its last access point, address and time (for the map)
                    known = await self._get(http, f"{base}/stat/alluser", headers)
                except ApiError:
                    known = []
            finally:
                try:
                    await http.post(f"{s['url']}/api/auth/logout", headers=headers)
                except httpx.HTTPError:
                    pass
        snap = {"devices": [{"mac": d.get("mac", ""), "name": d.get("name") or d.get("model") or d.get("mac"), "ip": d.get("ip") or "",
                             "model": d.get("model"), "type": d.get("type"), "state": d.get("state"),
                             "version": d.get("version"), "upgradable": bool(d.get("upgradable")),
                             "upgrade_to": d.get("upgrade_to_firmware"), "uptime": d.get("uptime") or 0,
                             "clients": d.get("num_sta") or 0, "adopted": d.get("adopted", True),
                             "cpu": _num((d.get("system-stats") or {}).get("cpu")),
                             "mem": _num((d.get("system-stats") or {}).get("mem")),
                             "rx_bytes": _num(d.get("rx_bytes")), "tx_bytes": _num(d.get("tx_bytes")),
                             "satisfaction": _num(d.get("satisfaction")),
                             "radios": [{"radio": r.get("radio", ""), "channel": _num(r.get("channel")),
                                         "clients": _num(r.get("num_sta")), "use": _num(r.get("cu_total"))}
                                        for r in d.get("radio_table_stats") or []]} for d in devices],
                "health": [{"subsystem": h.get("subsystem"), "status": h.get("status"), "users": h.get("num_user")}
                           for h in health],
                "clients": [{"mac": c.get("mac", ""), "name": c.get("name") or c.get("hostname") or "",
                             "ap_mac": c.get("ap_mac", ""), "wired": bool(c.get("is_wired")), "ip": c.get("ip") or "",
                             "essid": c.get("essid") or "", "guest": bool(c.get("is_guest")),
                             "authorized": c.get("authorized")} for c in stations],
                "history": [{"mac": c.get("mac", ""), "name": c.get("name") or c.get("hostname") or "",
                             "ap_mac": c.get("last_uplink_mac") or "", "ip": c.get("last_ip") or "",
                             "last_seen": c.get("last_seen") or 0, "wired": bool(c.get("is_wired"))}
                            for c in known if c.get("mac") and c.get("last_seen")]}
        wlan = next((h for h in snap["health"] if h["subsystem"] == "wlan"), {})
        snap["_summary"] = f"{len(snap['devices'])} devices, {wlan.get('users') or 0} Wi-Fi clients"
        return snap

    def device_events(self, rt, prev):
        out: list[DevEvent] = []
        if not self.snap:
            return out
        for d in self.snap["devices"]:
            if d.get("state") == 1 and d.get("uptime"):
                booted(prev, d.get("mac") or d["name"], self.snap_ts - float(d["uptime"]), d["name"], d["name"], out)
        return out

    def series(self, site: str) -> list[str]:
        snap, ts = self.snap, self.snap_ts
        if not snap:
            return []
        out = lines("hw_unifi_device", [
            ({"site": site, "device": d["name"] or "", "mac": d["mac"], "model": d.get("model") or "", "type": d.get("type") or ""},
             {"online": float(d.get("state") == 1), "clients": num(d.get("clients")), "cpu_percent": d.get("cpu"),
              "mem_percent": d.get("mem"), "uptime_seconds": num(d.get("uptime")), "rx_bytes": d.get("rx_bytes"),
              "tx_bytes": d.get("tx_bytes"), "satisfaction_percent": d.get("satisfaction"),
              "upgradable": float(d.get("upgradable", False))}) for d in snap["devices"]], ts)
        out += lines("hw_unifi_radio", [
            ({"site": site, "device": d["name"] or "", "radio": r["radio"]},
             {"clients": r["clients"], "channel": r["channel"], "channel_use_percent": r["use"]})
            for d in snap["devices"] for r in d.get("radios", [])], ts)
        # "unknown": a part this site doesn't have (no UniFi gateway: wan, www, lan, vpn) - left out
        out += lines("hw_unifi_health", [({"site": site, "subsystem": h["subsystem"] or ""},
                     {"ok": float(h.get("status") == "ok"), "users": num(h.get("users"))}) for h in snap["health"]
                     if h.get("status") in ("ok", "warning", "error")], ts)
        return out

    @staticmethod
    async def _get(http, url, headers):
        try:
            r = await http.get(url, headers=headers)
        except httpx.HTTPError as e:
            raise ApiError(f"can't read UniFi ({why(e)})") from None
        if r.status_code == 401:
            raise ApiError("UniFi session not accepted")
        if r.status_code == 404:
            raise ApiError("site not found: check the Site field")
        if r.status_code >= 300:
            raise ApiError(f"UniFi answered HTTP {r.status_code}")
        return r.json().get("data", [])

    def default_chosen(self, snap):
        return sorted(d["mac"] for d in snap["devices"] if d["adopted"])

    def choices(self, snap):
        return [Choice(d["mac"], d["name"], f"{d['model']} · {STATE.get(d['state'], d['state'])}")
                for d in sorted(snap["devices"], key=lambda d: d["name"])]

    def check(self, s, th, snap, now):
        out = []

        def add(cid, status, detail, value=None):
            dd = next(c for c in CHECKS if c.id == cid)
            out.append(CheckResult(f"{self.id}.{cid}", dd.name, status, detail, value, dd.unit))

        def on_map(devs):  # the access points with the problem, as nodes of the map (topology.py)
            out[-1].extra["nodes"] = [f"mac:{d['mac'].upper()}" for d in devs if d.get("mac")]

        chosen = set(s.get("chosen", []))
        watched = [d for d in snap["devices"] if d["mac"] in chosen]
        bad = [f"{d['name']} {STATE.get(d['state'], d['state'])}" for d in watched if d["state"] not in (1, 4, 5)]
        busy = [f"{d['name']} {STATE[d['state']]}" for d in watched if d["state"] in (4, 5)]
        if bad:
            add("devices", ERROR, "; ".join(bad))
            on_map([d for d in watched if d["state"] not in (1, 4, 5)])
        elif busy:
            add("devices", WARNING, "; ".join(busy))
            on_map([d for d in watched if d["state"] in (4, 5)])
        else:
            add("devices", OK, f"{len(watched)} watched devices connected")

        cpus = [(d["cpu"], d["name"]) for d in watched if d.get("cpu") is not None and d["state"] == 1]
        if cpus:
            c, name = max(cpus)
            add("cpu", level(c, th["cpu"]), f"busiest {name} {c:.0f} %", c)
        else:
            add("cpu", OK, "not reported")

        wlan = next((h for h in snap["health"] if h["subsystem"] == "wlan"), None)
        if wlan is None or wlan["status"] in ("unknown", None):
            add("wifi", OK, "no Wi-Fi subsystem")
        else:
            add("wifi", OK if wlan["status"] == "ok" else WARNING, f"WLAN {wlan['status']}, {wlan.get('users') or 0} clients")

        names = {d["name"] for d in watched}
        add("reboots", *reboots([e for e in self.events_24h if e["device"] in names],
                                [(d["uptime"], d["name"]) for d in watched if d["state"] == 1 and d["uptime"]],
                                th["reboots"], now, "restart"))

        upd = [f"{d['name']} {d['version']} → {d['upgrade_to'] or 'newer'}" for d in watched if d["upgradable"]]
        add("firmware", WARNING if upd else OK, "; ".join(upd) if upd else "all watched devices up to date")
        return out

    def tables(self, snap, s):
        return [Table("Devices", ["Device", "Model", "State", "Firmware", "Clients", "Uptime"],
                      [[d["name"], d["model"], STATE.get(d["state"], d["state"]),
                        d["version"] + (f" → {d['upgrade_to']}" if d["upgradable"] and d["upgrade_to"] else ""),
                        d["clients"], f"{d['uptime'] / 86400:.1f} days"] for d in snap["devices"]])]


plugin = UniFiModule()
