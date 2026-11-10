"""OPNsense: read-only API key of a user with only the needed status pages."""

from __future__ import annotations

import re
from datetime import datetime, timezone

import httpx

from .. import ERROR, OK, WARNING, CheckDef, DevEvent, booted, level
from ...metrics import lines, num
from ..pinning import verify_for
from ..apibase import SELF_SIGNED, why, ApiError, ApiModule, Choice, Field, Table, https_url

CHECKS = [
    CheckDef("api", "API reachable", "api", help="ERROR when homeWatch can't read the OPNsense API."),
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
    CheckDef("firmware", "Firmware", "api", unit="days", warn=7, editable=("warn",), todo=True,
             help="Updates found by the last update check, or a reboot needed to finish one. Listed under To do; WARNING once it has waited this many days."),
]
GW_LEVEL = {"none": OK, "down": ERROR, "force_down": ERROR, "loss": WARNING, "delay": WARNING,
            "highloss": ERROR, "highdelay": WARNING, "delay+loss": WARNING}
_IDLE = re.compile(r"([\d.]+)% idle")


# the OPNsense 26.x privilege that opens each required API path
PRIVILEGE = {"diagnostics/system/": "Lobby: Dashboard", "routes/gateway/": "System: Gateways"}
NOT_GUI = ("Enter the address of the OPNsense web GUI with its port, e.g. https://192.168.1.1:8443 "
           "(System → Settings → Administration → TCP port).")


CPU_WINDOW = 600  # seconds the CPU check averages over


def addresses(arp, leases) -> list[dict]:
    """[{mac, ip, name}] from the ARP table and the DHCP leases (either may be missing: no privilege)."""
    out: dict[str, dict] = {}
    for row in (arp if isinstance(arp, list) else (arp or {}).get("rows") or []):
        m, ip = str(row.get("mac") or "").upper(), row.get("ip") or ""
        if m and ip and m != "(INCOMPLETE)":
            out.setdefault(m, {"mac": m, "ip": ip, "name": row.get("hostname") or ""})
    for row in (leases or {}).get("rows") or [] if isinstance(leases, dict) else []:
        m = str(row.get("hwaddr") or row.get("mac") or "").upper()
        ip = row.get("address") or row.get("ip") or ""
        if m and ip:
            e = out.setdefault(m, {"mac": m, "ip": ip, "name": ""})
            e["name"] = e["name"] or row.get("hostname") or ""
    return list(out.values()) or None


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
                 "System: Firmware\nSystem: Trust: Certificates\nServices: HAProxy (only if you use it)\n"
                 "Reporting: Traffic (only for the Grafana traffic graphs)\n"
                 "Diagnostics: Logs: Gateways (optional: exact gateway down / up times)\n"
                 "Diagnostics: Logs: System (optional: exact PPPoE disconnect / reconnect times)\n"
                 "Diagnostics: ARP Table (optional: IP addresses on the Map)\n"
                 "Services: Dnsmasq DNS/DHCP: Settings (optional: names and addresses from the DHCP leases on the Map)"),
        ("text", "Services: Dnsmasq DNS/DHCP: Settings is the only privilege that opens the leases (ui/dnsmasq/* and "
                 "api/dnsmasq/*); it would also allow changing Dnsmasq's settings. homeWatch only reads the leases."),
        ("text", "Lobby: Dashboard is what lets homeWatch read CPU, memory, disk, uptime and the version."),
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
    choices_help = "homeWatch alerts when one of these stops. The ones running when you connected are ticked."
    filter_title = "HAProxy servers to watch"
    filter_help = ("Untick a server that is down on purpose (e.g. a device that is often off); it stays in the "
                   "table below but no longer raises the HAProxy check.")

    skipped: set[str] = set()

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

            async def log(name: str, phrase: str, rows: int = 40):
                """Recent lines of an OPNsense log that contain phrase; None without the privilege to read it."""
                try:
                    r = await http.post(f"{s['url']}/api/diagnostics/log/core/{name}",
                                        json={"current": 1, "rowCount": rows, "searchPhrase": phrase})
                except httpx.HTTPError:
                    return None
                if r.status_code >= 300 or "json" not in r.headers.get("content-type", ""):
                    return None
                return [{"ts": x.get("timestamp", ""), "line": x.get("line", "")} for x in r.json().get("rows", [])]

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
                # for the map only: which IP each MAC has (privileges Diagnostics: ARP Table and
                # Services: Dnsmasq DNS/DHCP: Settings, which opens the leases)
                "addresses": addresses(await get("diagnostics/interface/get_arp", optional=True),
                                       await get("dnsmasq/leases/search", optional=True)),
                # for the Grafana traffic panels only; needs the privilege Reporting: Traffic
                "traffic": (await get("diagnostics/traffic/interface", optional=True) or {}).get("interfaces"),
                # optional, for exact times (privileges Diagnostics: Logs: Gateways / System)
                "logs": {"monitor": await log("gateways", "MONITOR"),
                         "linkdown": await log("system", "ppp-linkdown", 20),
                         "linkup": await log("system", "ppp-linkup", 20),
                         "noecho": await log("system", "peer not responding", 20),
                         "newip": await log("system", "IP renewal starting", 20)},
            }
            snap["tz_offset"] = tz_offset((snap["time"] or {}).get("datetime"), now)
        name = (snap["info"] or {}).get("name", "OPNsense")
        version = next((v for v in (snap["info"] or {}).get("versions", []) if v.startswith("OPNsense")), "")
        snap["_summary"] = f"{name} {version.replace('OPNsense ', '')}, up {snap['time'].get('uptime', '?')}".strip()
        return snap

    def device_events(self, rt, prev):
        out = []
        if not self.snap:
            return out
        name = (self.snap.get("info") or {}).get("name") or "OPNsense"
        up = uptime_s((self.snap.get("time") or {}).get("uptime"))
        booted(prev, "boot", self.snap_ts - up if up else None, name, f"Firewall {name}", out)
        logs = self.snap.get("logs") or {}
        out += log_events(logs, self.snap.get("tz_offset"), name, prev, self.snap_ts)
        gws = prev.setdefault("gateways", {})
        exact_gw = logs.get("monitor") is not None  # the gateway log tells the exact times: no guessed ones
        for g in self.snap["gateways"]:
            gname, online = g.get("name") or "?", g.get("status_translated") == "Online"
            old = gws.get(gname)
            gws[gname] = online
            if old is not None and old != online and not exact_gw:
                out.append(DevEvent(self.snap_ts, "gateway_up" if online else "gateway_down", f"{name} {gname}",
                                    f"Gateway {gname} {'back online' if online else 'offline'}", exact=False))
        return out

    def series(self, site: str) -> list[str]:
        snap, ts = self.snap, self.snap_ts
        if not snap:
            return []
        name = (snap.get("info") or {}).get("name") or "OPNsense"
        idle = next((float(m.group(1)) for line in (snap.get("activity") or {}).get("headers", [])
                     if (m := _IDLE.search(line))), None)
        mem = (snap.get("resources") or {}).get("memory") or {}
        ms = lambda v: num(str(v or "").split()[0]) if v else None  # noqa: E731 - "8.6 ms", "0.0 %"
        out = lines("hw_opnsense_system", [({"site": site, "firewall": name},
                    {"cpu_percent": None if idle is None else 100 - idle, "mem_used_bytes": num(mem.get("used")),
                     "mem_total_bytes": num(mem.get("total")), "arc_bytes": num(mem.get("arc")),
                     "uptime_seconds": uptime_s((snap.get("time") or {}).get("uptime"))})], ts)
        out += lines("hw_opnsense_disk", [({"site": site, "firewall": name, "mount": d.get("mountpoint") or ""},
                     {"used_bytes": num(d.get("used_bytes")), "total_bytes": num(d.get("total_bytes"))})
                     for d in (snap.get("disk") or {}).get("devices", []) if d.get("mountpoint") == "/"], ts)
        out += lines("hw_opnsense_gateway", [({"site": site, "firewall": name, "gateway": g.get("name") or ""},
                     {"online": float(g.get("status_translated") == "Online"), "delay_ms": ms(g.get("delay")),
                      "stddev_ms": ms(g.get("stddev")), "loss_percent": ms(g.get("loss"))}) for g in snap["gateways"]], ts)
        out += lines("hw_opnsense_interface", [({"site": site, "firewall": name, "interface": v.get("name") or k,
                                                 "device": v.get("device") or ""},
                     {"rx_bytes": num(v.get("bytes received")), "tx_bytes": num(v.get("bytes transmitted"))})
                     for k, v in (snap.get("traffic") or {}).items()], ts)
        out += lines("hw_opnsense_haproxy", [({"site": site, "firewall": name, "backend": x.get("pxname") or "",
                                               "server": x.get("svname") or ""},
                     {"up": float(str(x.get("status", "")).upper().startswith("UP")),
                      "down": float(str(x.get("status", "")).upper().startswith("DOWN")),
                      "watched": float(_server_key(x) not in self.skipped),
                      "sessions": num(x.get("scur")), "bytes_in": num(x.get("bin")), "bytes_out": num(x.get("bout"))})
                     for x in _servers(snap.get("haproxy"))], ts)
        out += lines("hw_opnsense_service", [({"site": site, "firewall": name, "service": r.get("name") or r.get("id") or ""},
                     {"running": float(bool(r.get("running")))}) for r in snap.get("services") or []], ts)
        return out

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
        self.skipped = set(s.get("skipped", []))  # for series(), which runs after the check: unwatched HAProxy servers
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


_UPTIME = re.compile(r"(?:(\d+)\s+days?,?\s*)?(?:(\d+):(\d+)(?::(\d+))?)?(?:\s*(\d+)\s+mins?)?(?:\s*(\d+)\s+secs?)?$")


def uptime_s(text) -> float | None:
    """OPNsense's uptime text ("7 days, 21:07:59", "1 day", "03:12", "5 mins") in seconds."""
    m = _UPTIME.match(str(text or "").strip())
    if not text or not m or not any(m.groups()):
        return None
    d, h, mi, sec, mins, secs = (int(g or 0) for g in m.groups())
    return float(d * 86400 + h * 3600 + mi * 60 + sec + mins * 60 + secs)


def _servers(hap) -> list[dict]:
    return [x for x in hap or [] if x.get("svname") not in ("FRONTEND", "BACKEND")]


def _server_key(x: dict) -> str:
    return f"{x['pxname']}/{x.get('svname')}"


_MONITOR = re.compile(r"MONITOR: (\S+) \(Addr: (\S+) Alarm: (\w+) -> (\w+).*?Loss: ([\d.]+) ?%")
_NEWIP = re.compile(r"new: ([\d.a-fA-F:]+), old: ([\d.a-fA-F:]+)")
LOG_BACK = 24 * 3600  # s: the first reading also takes this much of the logs, so a recent outage gets its times


def tz_offset(text, now: float) -> float | None:
    """The firewall's UTC offset in seconds, from its local time ("Mon Nov 9 15:18:40 EET 2026") read at now."""
    parts = str(text or "").split()
    if len(parts) < 6:
        return None
    try:
        local = datetime.strptime(" ".join([*parts[1:4], parts[-1]]), "%b %d %H:%M:%S %Y")
    except ValueError:
        return None
    return round((local.replace(tzinfo=timezone.utc).timestamp() - now) / 900) * 900


def log_ts(text: str, offset: float | None) -> float | None:
    """A log line's local time ("2026-11-09T13:12:46") as a timestamp."""
    if offset is None:
        return None
    try:
        return datetime.strptime(text[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp() - offset
    except ValueError:
        return None


def log_events(logs: dict, offset: float | None, name: str, prev: dict, now: float) -> list[DevEvent]:
    """Exact gateway and PPPoE events from the OPNsense logs, each line once (prev["log_ts"])."""
    if offset is None or not any(v for v in logs.values()):
        return []
    since = prev.get("log_ts", now - LOG_BACK)

    def rows(key):
        return sorted((t, x["line"]) for x in logs.get(key) or [] if (t := log_ts(x["ts"], offset)) and t > since)

    out = []
    for t, text in rows("monitor"):
        m = _MONITOR.search(text)
        if not m or m[3] == m[4]:
            continue
        gw, addr, old, new, loss = m.groups()
        if new == "down":
            out.append(DevEvent(t, "gateway_down", f"{name} {gw}", f"Gateway {gw} down ({addr} not answering)"))
        elif new == "none" and old == "down":
            out.append(DevEvent(t, "gateway_up", f"{name} {gw}", f"Gateway {gw} back up"))
        elif new == "loss" and old == "none":
            out.append(DevEvent(t, "gateway_loss", f"{name} {gw}", f"Gateway {gw}: packet loss {loss} % to {addr}"))
    noecho = [t for t, _ in rows("noecho")]
    for t, _text in rows("linkdown"):
        why = " (the ISP stopped answering)" if any(0 <= t - n <= 60 for n in noecho) else ""
        out.append(DevEvent(t, "ppp_down", f"{name} PPPoE", f"PPPoE session dropped{why}"))
    newip = [(t, m) for t, text in rows("newip") if (m := _NEWIP.search(text))]
    for t, _text in rows("linkup"):
        ip = next((m for n, m in newip if 0 <= n - t <= 60 and m[1] != m[2]), None)
        out.append(DevEvent(t, "ppp_up", f"{name} PPPoE",
                            "PPPoE session up" + (f", new public IP {ip[1]}" if ip else "")))
    stamps = [t for key in logs for t, _ in rows(key)]
    if stamps:
        prev["log_ts"] = max(stamps)
    elif "log_ts" not in prev:
        prev["log_ts"] = now - LOG_BACK
    return sorted(out, key=lambda e: e.ts)
