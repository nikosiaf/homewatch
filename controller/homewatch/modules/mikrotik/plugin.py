"""MikroTik: one read-only user on every device; reads /rest over www (or www-ssl)."""

from __future__ import annotations

import re

import httpx

from .. import ERROR, OK, WARNING, CheckDef, CheckResult, level
from ..pinning import host_port, verify_for
from ..apibase import SELF_SIGNED, why, ApiError, ApiModule, Choice, Field, Table

ERROR_WINDOW = 3600  # port errors are counted over the last hour
CHECKS = [
    CheckDef("api", "Devices reachable", "api", help="ERROR when none of the devices can be read."),
    CheckDef("each", "Every device read", "api", help="ERROR when one of several devices can't be read."),
    CheckDef("cpu", "CPU", "api", unit="%", warn=85, error=95, editable=("warn", "error")),
    CheckDef("mem", "Memory", "api", unit="%", warn=90, error=95, editable=("warn", "error")),
    CheckDef("temp", "Temperature", "api", unit="°C", warn=60, error=70, editable=("warn", "error")),
    CheckDef("ports", "Ports up", "api", help="ERROR when a port you chose is not running."),
    CheckDef("errors", "Port errors", "api", warn=10, error=1000, editable=("warn", "error"),
             help="Receive/transmit errors on all ports together in the last hour."),
    CheckDef("uptime", "Uptime", "api", unit="min", warn=60, editable=("warn",), lower_is_worse=True,
             help="WARNING after a reboot, for this many minutes."),
    CheckDef("firmware", "RouterBOOT", "api",
             help="WARNING when the RouterBOOT firmware is older than the installed RouterOS."),
]
_UNITS = {"w": 604800, "d": 86400, "h": 3600, "m": 60, "s": 1}
_HOST = re.compile(r"^[a-zA-Z0-9.-]+$")


def uptime_s(text: str) -> float:
    return sum(int(n) * _UNITS[u] for n, u in re.findall(r"(\d+)([wdhms])", text or ""))


def hosts(value: str) -> list[str]:
    return [h for h in re.split(r"[\s,]+", value or "") if h]


class MikroTikModule(ApiModule):
    id = "mikrotik"
    name = "MikroTik"
    checks = CHECKS
    description = ("Watches MikroTik switches and routers (RouterOS 7) through the REST API with a read-only user: "
                   "CPU, memory, temperature, ports, port errors, reboots and RouterBOOT firmware.")
    setup = [
        ("text", "On every device (WinBox → New Terminal), create a read-only user and allow this homewatch host "
                 "to reach the web service the REST API runs on:"),
        ("code", "/user add name=homewatch group=read password=\"a-long-password\"\n"
                 "# HTTPS for the REST API, with the device's own self-signed certificate:\n"
                 "/certificate add name=www common-name=<device address> key-usage=key-cert-sign,tls-server\n"
                 "/certificate sign www\n"
                 "/ip service set www-ssl certificate=www disabled=no\n"
                 "# if www-ssl has an 'available-from' list, add this homewatch host's address to it:\n"
                 "/ip service set www-ssl available-from=<existing list>,<homewatch ip>/32"),
        ("text", "Use the same user and password on every device, then list their addresses below. That certificate is "
                 "self-signed, so untick \"Check the HTTPS certificate\" unless you installed a trusted one."),
    ]
    fields = [
        Field("devices", "Device address(es)", "192.168.88.1, 192.168.88.2",
              help="One or more addresses, separated by commas or spaces."),
        Field("username", "User name", "homewatch", default="homewatch"),
        Field("https", "Use HTTPS (www-ssl)", kind="check", default=True,
              help="Without it the password crosses your network unencrypted at every read."),
        Field("verify", "Check the HTTPS certificate", kind="check", default=True, help=SELF_SIGNED, when="https"),
    ]
    secret_label = "Password"
    choices_title = "Ports that should be up"
    choices_help = "homewatch alerts when one of these goes down. The ports running when you connected are ticked."
    more_title = "Additional configuration"
    more_help = "After connecting: the ports homewatch watches."
    list_search = False

    def __init__(self):
        super().__init__()
        self.err_hist: dict[str, list[tuple[float, int]]] = {}  # per port: (time, error counter), last hour

    def validate(self, form, old):
        out, err = super().validate(form, old)
        if err:
            return out, err
        hs = hosts(out["devices"])
        if not hs or not all(_HOST.match(h) for h in hs):
            return out, "Device addresses: IP addresses or host names, separated by commas"
        if not out["username"]:
            return out, "Enter the user name"
        return out | {"devices": ", ".join(hs)}, ""

    def notice(self, settings):
        if not settings.get("https"):
            return "Plain HTTP: the password crosses your network unencrypted at every read. Enable www-ssl and tick Use HTTPS."
        return ""

    def tls_hosts(self, settings):
        return [host_port(h) for h in hosts(settings.get("devices", ""))] if settings.get("https") else []

    async def fetch(self, s, secret, now):
        scheme = "https" if s.get("https") else "http"
        devices, errors = [], {}
        for host in hosts(s["devices"]):  # one connection each: every device has its own certificate
            async with httpx.AsyncClient(verify=verify_for(s, host), timeout=10, auth=(s["username"], secret)) as http:
                try:
                    devices.append(await self._device(http, f"{scheme}://{host}/rest", host))
                except ApiError as e:
                    errors[host] = str(e)
        if not devices:
            raise ApiError("; ".join(f"{h}: {e}" for h, e in errors.items()) or "no devices")
        names = ", ".join(d["name"] for d in devices)
        return {"devices": devices, "errors": errors, "_summary": f"{len(devices)} read: {names}"}

    async def _device(self, http, base: str, host: str) -> dict:
        async def get(path):
            try:
                r = await http.get(f"{base}/{path}")
            except httpx.HTTPError as e:
                raise ApiError(f"can't reach the REST API ({why(e)}); is the web service enabled and this host "
                               "in its available-from list?") from None
            if r.status_code == 401:
                raise ApiError("user name or password rejected")
            if r.status_code >= 300:
                raise ApiError(f"HTTP {r.status_code} for {path}")
            return r.json()

        res = await get("system/resource")
        ident = await get("system/identity")
        health = await get("system/health")
        board = await get("system/routerboard")
        ifs = await get("interface")
        return {
            "host": host, "name": ident.get("name") or host,
            "model": res.get("board-name"), "version": res.get("version"),
            "cpu": float(res.get("cpu-load", 0)),
            "mem": 100 * (1 - float(res.get("free-memory", 0)) / float(res.get("total-memory", 1))),
            "uptime": uptime_s(res.get("uptime", "")),
            "temp": next((float(h["value"]) for h in health if "temperature" in h.get("name", "")), None),
            "voltage": next((float(h["value"]) for h in health if h.get("name") == "voltage"), None),
            "boot": board.get("current-firmware"), "boot_upgrade": board.get("upgrade-firmware"),
            "ports": [{"name": i["name"], "comment": i.get("comment") or "",
                       "running": str(i.get("running")) == "true", "disabled": str(i.get("disabled")) == "true",
                       "errors": int(i.get("rx-error", 0) or 0) + int(i.get("tx-error", 0) or 0)}
                      for i in ifs if i.get("type") in ("ether", "sfp", "combo", "sfp-sfpplus", "sfpplus")],
        }

    @staticmethod
    def _key(d, p):
        return f"{d['host']}/{p['name']}"

    def default_chosen(self, snap):
        return [self._key(d, p) for d in snap["devices"] for p in d["ports"] if p["running"] and not p["disabled"]]

    def choices(self, snap):
        return [Choice(self._key(d, p), p["name"], " · ".join(x for x in (p["comment"], "" if p["running"] else "down") if x),
                       group=d["name"])
                for d in snap["devices"] for p in d["ports"] if not p["disabled"]]

    def check(self, s, th, snap, now):
        out = []

        def add(cid, status, detail, value=None):
            dd = next(c for c in CHECKS if c.id == cid)
            out.append(CheckResult(f"{self.id}.{cid}", dd.name, status, detail, value, dd.unit))

        devs = snap["devices"]
        if snap["errors"]:
            add("each", ERROR, "Can't read " + "; ".join(f"{h}: {e}" for h, e in snap["errors"].items()))
        else:
            add("each", OK, f"all {len(devs)} read")

        def worst(cid, attr, unit, lower=False):
            vals = [(d[attr], d["name"]) for d in devs if d.get(attr) is not None]
            if not vals:
                add(cid, OK, "not reported")
                return
            v, name = min(vals) if lower else max(vals)
            add(cid, level(v, th[cid], lower), f"{name} {v:.0f} {unit}" + (
                f" (others: {', '.join(f'{n} {x:.0f}' for x, n in vals if n != name)})" if len(vals) > 1 else ""), v)

        worst("cpu", "cpu", "%")
        worst("mem", "mem", "%")
        worst("temp", "temp", "°C")

        chosen = set(s.get("chosen", []))
        down = [f"{d['name']} {p['name']}" + (f" ({p['comment']})" if p["comment"] else "")
                for d in devs for p in d["ports"] if self._key(d, p) in chosen and not p["running"]]
        add("ports", ERROR if down else OK, f"Down: {', '.join(down)}" if down else f"{len(chosen)} chosen ports up")

        new_errors, worst_port, worst_n = 0, "", 0
        for d in devs:
            for p in d["ports"]:
                hist = self.err_hist.setdefault(self._key(d, p), [])
                if hist and p["errors"] < hist[-1][1]:
                    hist.clear()  # the counter restarted (reboot): count from here
                hist.append((now, p["errors"]))
                while len(hist) > 1 and hist[1][0] <= now - ERROR_WINDOW:
                    hist.pop(0)  # keep one sample at or before the start of the window as the baseline
                n = p["errors"] - hist[0][1]
                new_errors += n
                if n > worst_n:
                    worst_port, worst_n = f"{d['name']} {p['name']}", n
        add("errors", level(new_errors, th["errors"]) if new_errors else OK,
            f"{new_errors} errors in the last hour, most on {worst_port}" if new_errors else "no errors in the last hour",
            new_errors)

        recent = [(d["uptime"] / 60, d["name"]) for d in devs if d["uptime"]]
        if recent:
            m, name = min(recent)
            add("uptime", level(m, th["uptime"], True), f"{name} rebooted {m:.0f} min ago" if level(m, th["uptime"], True)
                else f"shortest uptime {name} {m / 1440:.1f} days", m)
        else:
            add("uptime", OK, "not reported")

        old = [f"{d['name']} {d['boot']} (RouterOS {str(d['version']).split()[0]})" for d in devs
               if d.get("boot") and d.get("boot_upgrade") and d["boot"] != d["boot_upgrade"]]
        add("firmware", WARNING if old else OK,
            "RouterBOOT older than RouterOS: " + ", ".join(old) + " - System → RouterBOARD → Upgrade, then reboot"
            if old else "RouterBOOT matches RouterOS")
        return out

    def tables(self, snap, s):
        t = [Table("Devices", ["Device", "Model", "RouterOS", "RouterBOOT", "CPU", "Memory", "Temp", "Voltage", "Uptime"],
                   [[d["name"], d["model"], d["version"], d["boot"], f"{d['cpu']:.0f} %", f"{d['mem']:.0f} %",
                     f"{d['temp']:.0f} °C" if d["temp"] is not None else None,
                     f"{d['voltage']:.1f} V" if d["voltage"] is not None else None,
                     f"{d['uptime'] / 86400:.1f} days"] for d in snap["devices"]])]
        for d in snap["devices"]:
            t.append(Table(f"Ports on {d['name']}", ["Port", "Comment", "State", "Errors"],
                           [[p["name"], p["comment"], "disabled" if p["disabled"] else ("up" if p["running"] else "down"),
                             p["errors"]] for p in d["ports"]]))
        return t


plugin = MikroTikModule()
