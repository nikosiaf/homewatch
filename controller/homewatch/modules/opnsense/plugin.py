"""OPNsense: read-only API key of a user with only the needed status pages."""

from __future__ import annotations

import re

import httpx

from .. import ERROR, OK, WARNING, CheckDef, level
from ..pinning import verify_for
from ..apibase import SELF_SIGNED, why, ApiError, ApiModule, Choice, Field, Table, https_url

CHECKS = [
    CheckDef("api", "API reachable", "api", help="ERROR when homewatch can't read the OPNsense API."),
    CheckDef("gateways", "Gateways", "api", help="ERROR when a gateway is down; WARNING on packet loss or delay."),
    CheckDef("cpu", "CPU", "api", unit="%", warn=85, error=95, editable=("warn", "error"),
             help="Average of the readings in the last 10 minutes, so short bursts stay quiet."),
    CheckDef("mem", "Memory", "api", unit="%", warn=90, error=95, editable=("warn", "error"),
             help="Used memory without the ZFS cache (ARC), which is freed when needed."),
    CheckDef("disk", "Root disk", "api", unit="%", warn=85, error=95, editable=("warn", "error")),
    CheckDef("services", "Services", "api", help="ERROR when a service you chose is not running."),
    CheckDef("haproxy", "HAProxy servers", "api", help="ERROR when a health-checked HAProxy server is down."),
    CheckDef("certs", "Certificates", "api", unit="days", warn=20, error=7, editable=("warn", "error"),
             lower_is_worse=True, help="Days until the first certificate in use expires."),
    CheckDef("firmware", "Firmware", "api",
             help="WARNING when the last update check found updates, or a reboot is needed."),
]
GW_LEVEL = {"none": OK, "down": ERROR, "force_down": ERROR, "loss": WARNING, "delay": WARNING,
            "highloss": ERROR, "highdelay": WARNING, "delay+loss": WARNING}
_IDLE = re.compile(r"([\d.]+)% idle")


# the OPNsense 26.x privilege that opens each required API path
PRIVILEGE = {"diagnostics/system/": "Lobby: Dashboard", "routes/gateway/": "System: Gateways"}
NOT_GUI = ("Enter the address of the OPNsense web GUI with its port, e.g. https://192.168.1.1:8443 "
           "(System → Settings → Administration → TCP port).")


CPU_WINDOW = 600  # seconds the CPU check averages over


class OPNsenseModule(ApiModule):
    id = "opnsense"
    name = "OPNsense"
    icon = "shield"
    headline = ("cpu", "CPU")
    checks = CHECKS
    description = ("Watches an OPNsense firewall through a read-only API key: gateways, CPU, memory, disk, "
                   "services, HAProxy servers, certificates and firmware updates. Nothing on OPNsense is changed.")
    setup = [
        ("text", "In OPNsense: System → Access → Users → Add. Name it homewatch, tick \"Generate a scrambled "
                 "password\", and give it only these privileges (Effective privileges): "),
        ("code", "Lobby: Dashboard\nSystem: Gateways\nDiagnostics: System Activity\nStatus: Services\n"
                 "System: Firmware\nSystem: Trust: Certificates\nServices: HAProxy (only if you use it)"),
        ("text", "Lobby: Dashboard is what lets homewatch read CPU, memory, disk, uptime and the version."),
        ("text", "Save, then press \"+\" under API keys to create a key. OPNsense downloads a file with the key "
                 "and secret: paste them below."),
    ]
    fields = [
        Field("url", "OPNsense address", "https://192.168.1.1", default=""),
        Field("api_key", "API key", "the key= line of the downloaded file", secret=True),
        Field("verify", "Check the HTTPS certificate", kind="check", default=True,
              help=SELF_SIGNED),
    ]
    secret_label = "API secret"
    secret_placeholder = "the secret= line of the downloaded file"
    more_title = "Additional configuration"
    more_help = "After connecting: the services that must run and the HAProxy servers to watch."
    filters_first = False
    choices_title = "Services that must run"
    choices_help = "homewatch alerts when one of these stops. The ones running when you connected are ticked."
    filter_title = "HAProxy servers to watch"
    filter_help = ("Untick a server that is down on purpose (e.g. a device that is often off); it stays in the "
                   "table below but no longer raises the HAProxy check.")

    def __init__(self):
        super().__init__()
        self.cpu_hist: list[tuple[float, float]] = []  # (reading time, CPU %) for the 10-minute average

    def validate(self, form, old):
        out, err = super().validate(form, old)
        if err:
            return out, err
        url = https_url(out["url"])
        if not url:
            return out, "OPNsense address: e.g. https://192.168.1.1 (add :port when the GUI uses another one)"
        return out | {"url": url}, ""

    async def fetch(self, s, secret, now):
        async with httpx.AsyncClient(verify=verify_for(s, s["url"]), timeout=15,  # noqa: S501 - self-signed GUI
                                     auth=(s["api_key"], secret)) as http:
            async def get(path: str, optional: bool = False):
                try:
                    r = await http.get(f"{s['url']}/api/{path}")
                except httpx.HTTPError as e:
                    raise ApiError(f"can't reach OPNsense ({why(e)})") from None
                if r.status_code == 401:
                    raise ApiError("OPNsense rejected the API key")
                if r.status_code in (403, 404) and optional:
                    return None
                if r.status_code == 403:
                    need = next((v for k, v in PRIVILEGE.items() if path.startswith(k)), "")
                    raise ApiError(f"the key's user lacks the privilege {need or 'for ' + path}"
                                   + (" (System → Access → Users → homewatch)" if need else ""))
                is_json = "json" in r.headers.get("content-type", "")
                if r.status_code in (301, 302, 307, 308, 502, 503) or (r.status_code < 300 and not is_json):
                    # e.g. HAProxy or another web server on port 443 while the GUI listens on another port
                    raise ApiError(f"{s['url']} answered HTTP {r.status_code}, not like the OPNsense API. {NOT_GUI}")
                if r.status_code >= 300:
                    raise ApiError(f"OPNsense answered HTTP {r.status_code} for {path}")
                return r.json()

            snap = {
                "gateways": (await get("routes/gateway/status")).get("items", []),
                "resources": await get("diagnostics/system/system_resources"),
                "disk": await get("diagnostics/system/system_disk"),
                "time": await get("diagnostics/system/system_time"),
                "activity": await get("diagnostics/activity/getActivity", optional=True),
                "services": (await get("core/service/search", optional=True) or {}).get("rows"),
                # the counters list also holds nested lists (stick tables): keep the proxy/server entries
                "haproxy": [x for x in (await get("haproxy/statistics/counters", optional=True) or [])
                            if isinstance(x, dict)] or None,
                "certs": [{k: c.get(k) for k in ("descr", "commonname", "valid_to", "in_use")}
                          for c in (await get("trust/cert/search", optional=True) or {}).get("rows", [])] or None,
                "firmware": await get("core/firmware/status", optional=True),
                "info": await get("diagnostics/system/system_information", optional=True),
            }
        name = (snap["info"] or {}).get("name", "OPNsense")
        version = next((v for v in (snap["info"] or {}).get("versions", []) if v.startswith("OPNsense")), "")
        snap["_summary"] = f"{name} {version.replace('OPNsense ', '')}, up {snap['time'].get('uptime', '?')}".strip()
        return snap

    def default_chosen(self, snap):
        return [r["id"] for r in snap.get("services") or [] if r.get("running")]

    def choices(self, snap):
        return [Choice(r["id"], r.get("description") or r["id"], "" if r.get("running") else "stopped")
                for r in snap.get("services") or []]

    def filters(self, snap):
        return [Choice(_server_key(x), x["pxname"] if x.get("svname") == x["pxname"] else f"{x['pxname']} / {x.get('svname')}",
                       str(x.get("status") or ""))
                for x in _servers(snap.get("haproxy"))]

    def check(self, s, th, snap, now):
        out = []

        def add(cid, status, detail, value=None):
            d = next(c for c in CHECKS if c.id == cid)
            from .. import CheckResult
            out.append(CheckResult(f"{self.id}.{cid}", d.name, status, detail, value, d.unit))

        gws = snap["gateways"]
        worst, notes = OK, []
        for g in gws:
            lv = GW_LEVEL.get(g.get("status", "none"), WARNING)
            worst = max(worst, lv)
            if lv:
                notes.append(f"{g.get('name')} {g.get('status_translated', g.get('status'))} "
                             f"(loss {g.get('loss', '?')}, delay {g.get('delay', '?')})")
        add("gateways", worst, "; ".join(notes) if notes else
            ", ".join(f"{g.get('name')} online, {g.get('delay', '?')}" for g in gws) or "No gateways")

        idle = None
        for line in (snap.get("activity") or {}).get("headers", []):
            if m := _IDLE.search(line):
                idle = float(m.group(1))
        if idle is None:
            add("cpu", OK, "not readable (add the privilege Diagnostics: System Activity)")
        else:
            cpu = 100 - idle
            ts = self.snap_ts or now
            if self.cpu_hist and self.cpu_hist[-1][0] == ts:
                self.cpu_hist[-1] = (ts, cpu)  # the same reading evaluated again
            else:
                self.cpu_hist.append((ts, cpu))
            self.cpu_hist = [x for x in self.cpu_hist if x[0] > now - CPU_WINDOW]
            avg = sum(c for _, c in self.cpu_hist) / len(self.cpu_hist)
            add("cpu", level(avg, th["cpu"]), f"CPU {avg:.0f} % (10-min average; now {cpu:.0f} %), "
                f"load {snap['time'].get('loadavg', '?')}", avg)

        mem = snap["resources"].get("memory", {})
        try:
            used = (float(mem["used"]) - float(mem.get("arc") or 0)) / float(mem["total"]) * 100
            add("mem", level(used, th["mem"]), f"memory {used:.0f} % (without ZFS cache)", used)
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            add("mem", OK, "not reported")

        root = next((d for d in snap["disk"].get("devices", []) if d.get("mountpoint") == "/"), None)
        if root:
            pct = float(root.get("used_pct", 0))
            add("disk", level(pct, th["disk"]), f"root disk {pct:.0f} % ({root.get('used')} of {root.get('total')})", pct)
        else:
            add("disk", OK, "not reported")

        services = snap.get("services")
        if services is None:
            add("services", OK, "not readable (add the privilege Status: Services)")
        else:
            chosen = set(s.get("chosen", []))
            down = [r.get("description") or r["id"] for r in services if r["id"] in chosen and not r.get("running")]
            add("services", ERROR if down else OK,
                f"Not running: {', '.join(down)}" if down else f"{len(chosen)} chosen services running")

        hap = snap.get("haproxy")
        if hap is None:
            add("haproxy", OK, "HAProxy not installed or not readable")
        else:
            skipped = set(s.get("skipped", []))
            servers = [x for x in _servers(hap) if _server_key(x) not in skipped]
            down = [f"{x['pxname']}" for x in servers if str(x.get("status", "")).startswith("DOWN")]
            checked = [x for x in servers if x.get("status") not in ("no check", "")]
            ignored = len(_servers(hap)) - len(servers)
            add("haproxy", ERROR if down else OK, f"Down: {', '.join(down)}" if down else
                f"{len(checked)} health-checked servers up" + (f", {len(servers) - len(checked)} without a check"
                                                                if len(servers) > len(checked) else "")
                + (f", {ignored} not watched" if ignored else ""))

        certs = [c for c in snap.get("certs") or [] if str(c.get("in_use") or "") not in ("", "0", "False")]
        if snap.get("certs") is None:
            add("certs", OK, "not readable (add the privilege System: Trust: Certificates)")
        elif not certs:
            add("certs", OK, "No certificate in use")
        else:
            days, c = min(((float(c["valid_to"]) - now) / 86400, c) for c in certs if c.get("valid_to"))
            name = c.get("descr") or c.get("commonname")
            if days <= 0:
                add("certs", ERROR, f"{name} expired {-days:.0f} days ago", days)
            else:
                add("certs", level(days, th["certs"], True), f"{name} expires in {days:.0f} days", days)

        fw = snap.get("firmware")
        if fw is None:
            add("firmware", OK, "not readable (add the privilege System: Firmware)")
        else:
            n = len(fw.get("upgrade_packages") or []) + len(fw.get("new_packages") or [])
            reboot = str(fw.get("needs_reboot", "0")) == "1"
            version = fw.get("product_version", "?")
            if reboot:
                add("firmware", WARNING, f"{version}: a reboot is needed to finish an update")
            elif n or fw.get("status") in ("update", "upgrade"):
                add("firmware", WARNING, f"{version}: updates available ({n} packages); last check {fw.get('last_check', '?')}")
            else:
                add("firmware", OK, f"{version}, up to date at the last check ({fw.get('last_check', 'never')})")
        return out

    def tables(self, snap, s):
        t = [Table("Gateways", ["Gateway", "Status", "Loss", "Delay", "Monitor"],
                   [[g.get("name"), g.get("status_translated"), g.get("loss"), g.get("delay"), g.get("monitor")]
                    for g in snap["gateways"]])]
        if snap.get("haproxy"):
            skipped = set(s.get("skipped", []))
            t.append(Table("HAProxy servers", ["Backend", "Status", "Health check", "Watched"],
                           [[x["pxname"], x.get("status"), x.get("check_status") or "—",
                             "no" if _server_key(x) in skipped else "yes"] for x in _servers(snap["haproxy"])]))
        if snap.get("certs"):
            import time as _t
            t.append(Table("Certificates", ["Certificate", "In use", "Expires"],
                           [[c.get("descr"), "yes" if str(c.get("in_use") or "") not in ("", "0") else "",
                             _t.strftime("%d %b %Y", _t.localtime(float(c["valid_to"]))) if c.get("valid_to") else ""]
                            for c in snap["certs"]]))
        return t


plugin = OPNsenseModule()


def _servers(hap) -> list[dict]:
    return [x for x in hap or [] if x.get("svname") not in ("FRONTEND", "BACKEND")]


def _server_key(x: dict) -> str:
    return f"{x['pxname']}/{x.get('svname')}"
