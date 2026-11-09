"""MikroTik: one read-only user on every device; reads /rest over www (or www-ssl)."""

from __future__ import annotations

import asyncio
import re
import statistics
import time
from datetime import datetime, timezone

import httpx

from .. import ERROR, OK, WARNING, CheckDef, CheckResult, DevEvent, booted, level
from ...metrics import lines, num
from ..pinning import host_port, verify_for
from ..apibase import SELF_SIGNED, why, ApiError, ApiModule, Choice, Field, Table

ERROR_WINDOW = 3600  # port errors are counted over the last hour
# cpu-load is a ~1 s value: an SNMP walk or a login can pin a small RouterBOARD at 100 % for one second.
# Read it CPU_SAMPLES times CPU_GAP s apart and keep the median so a single-second spike is ignored.
CPU_SAMPLES = 3
CPU_GAP = 2.0
# Optional daily RouterOS update check against MikroTik's public upgrade server (one small text file per channel)
LATEST_URL = "https://upgrade.mikrotik.com/routeros/NEWESTa7.{channel}"
LATEST_EVERY = 86400  # once a day
LATEST_RETRY = 3600  # after a failed lookup
# Optional daily copy of each device's configuration (/export through REST, as the read-only user: no secrets)
BACKUP_EVERY = 86400  # one copy a day
BACKUP_RETRY = 3600  # after a failed copy
BACKUP_STALE = 2 * 86400  # WARNING when the newest good copy is older than this
BACKUP_KEEP = 60  # versions kept per device (a new one is stored only when the configuration changed)
CHANGED_FOR = 86400  # "Configuration changed" stays WARNING this long after a change
_HEADER = re.compile(r"^# .* by RouterOS .*$")
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
    CheckDef("backup", "Configuration copies", "api",
             help="WARNING when homeWatch couldn't save a copy of a device's configuration for 2 days. Only while "
                  "\"Keep a daily copy of the configuration\" is ticked."),
    CheckDef("changed", "Configuration changed", "api", default_on=False,
             help="WARNING for a day after a device's configuration changed (seen in the daily copy), so changes "
                  "nobody announced don't go unnoticed. Off by default; turn it on here."),
    CheckDef("routeros", "RouterOS updates", "api",
             help="WARNING when MikroTik has published a newer RouterOS for a device's update channel. Looked up once "
                  "a day on upgrade.mikrotik.com, only while \"Check daily for RouterOS updates\" is ticked."),
]
_UNITS = {"w": 604800, "d": 86400, "h": 3600, "m": 60, "s": 1}
_HOST = re.compile(r"^[a-zA-Z0-9.-]+$")


def gmt_offset(text) -> float | None:
    """RouterOS /system/clock gmt-offset ("+02:00") in seconds."""
    m = re.fullmatch(r"([+-])(\d{1,2}):(\d{2})", str(text or "").strip())
    if not m:
        return None
    return (1 if m[1] == "+" else -1) * (int(m[2]) * 3600 + int(m[3]) * 60)


def router_ts(text, offset: float | None) -> float | None:
    """A RouterOS local time ("2026-11-09 13:12:27", or 6.x "nov/09/2026 13:12:27") as a timestamp."""
    if not text or offset is None:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%b/%d/%Y %H:%M:%S"):
        try:
            naive = datetime.strptime(str(text).strip().capitalize() if "/" in str(text) else str(text).strip(), fmt)
        except ValueError:
            continue
        return naive.replace(tzinfo=timezone.utc).timestamp() - offset
    return None


def uptime_s(text: str) -> float:
    return sum(int(n) * _UNITS[u] for n, u in re.findall(r"(\d+)([wdhms])", text or ""))


def version_key(v: str) -> tuple:
    """'7.25.1' > '7.25' > '7.25rc3' > '7.25beta2'; anything unparsable sorts lowest."""
    m = re.match(r"\s*(\d+(?:\.\d+)*)(?:(alpha|beta|rc)(\d+))?", v or "")
    if not m:
        return ((),)
    nums = tuple(int(x) for x in m.group(1).split("."))
    nums += (0,) * (4 - len(nums))
    stage = {"alpha": 0, "beta": 1, "rc": 2}.get(m.group(2) or "", 3)
    return nums, stage, int(m.group(3) or 0)


def config_body(text: str) -> str:
    """An export without its dated header line, to tell a real change from a new timestamp."""
    return "\n".join(line for line in (text or "").replace("\r\n", "\n").split("\n")
                     if not _HEADER.match(line)).strip()


def hosts(value: str) -> list[str]:
    return [h for h in re.split(r"[\s,]+", value or "") if h]


class MikroTikModule(ApiModule):
    id = "mikrotik"
    name = "MikroTik"
    icon = "switch"
    headline = ("temp", "hottest device")
    checks = CHECKS
    description = ("Watches MikroTik switches and routers (RouterOS 7) through the REST API with a read-only user: "
                   "CPU, memory, temperature, ports, port errors, reboots, RouterBOOT firmware and, if you want, "
                   "new RouterOS versions and a daily copy of the configuration.")
    setup = [
        ("text", "On every device (WinBox → New Terminal), create a read-only user and allow this homeWatch host "
                 "to reach the web service the REST API runs on:"),
        ("code", "/user add name=homewatch group=read password=\"a-long-password\"\n"
                 "# HTTPS for the REST API, with the device's own self-signed certificate:\n"
                 "/certificate add name=www common-name=<device address> key-usage=key-cert-sign,tls-server\n"
                 "/certificate sign www\n"
                 "/ip service set www-ssl certificate=www disabled=no\n"
                 "# if www-ssl has an 'available-from' list, add this homeWatch host's address to it:\n"
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
        Field("os_updates", "Check daily for RouterOS updates", kind="check", default=False,
              help="Once a day homeWatch reads the newest RouterOS version for each device's update channel "
                   "(stable, long-term…) from MikroTik's public site upgrade.mikrotik.com and warns when a device "
                   "is behind. Needs internet access from this host; nothing is installed or changed on the devices."),
        Field("config_backup", "Keep a daily copy of the configuration", kind="check", default=False,
              help="Once a day homeWatch saves each device's configuration (the text of /export) and keeps a new "
                   "copy whenever it changed, with what changed and a download to restore from. The read-only user "
                   "can't see passwords and keys, so they are left out of the copies: re-enter them after a restore."),
    ]
    secret_label = "Password"
    choices_title = "Ports that should be up"
    choices_help = "homeWatch alerts when one of these goes down. The ports running when you connected are ticked."
    more_title = "Additional configuration"
    more_help = "After connecting: the ports homeWatch watches."
    list_search = False
    extra_partial = "_mikrotik_configs.html"  # the configuration copies, under the last reading

    def __init__(self):
        super().__init__()
        self.err_hist: dict[str, list[tuple[float, int]]] = {}  # per port: (time, error counter), last hour
        self.latest: dict[str, tuple[str, float]] = {}  # update channel -> (newest RouterOS, when it was read)
        self.latest_tried: dict[str, float] = {}  # update channel -> last lookup attempt
        self.latest_error: dict[str, str] = {}  # update channel -> why the last lookup failed
        self.backup_due: set[str] = set()  # hosts to copy at the next read
        self.backup_tried: dict[str, float] = {}  # host -> last copy attempt
        self.backup_error: dict[str, str] = {}  # host -> why the last copy failed

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

    def hidden_checks(self, settings):
        return (set() if settings.get("os_updates") else {"routeros"}) | (
            set() if settings.get("config_backup") else {"backup", "changed"})

    def tls_hosts(self, settings):
        return [host_port(h) for h in hosts(settings.get("devices", ""))] if settings.get("https") else []

    async def fetch(self, s, secret, now):
        scheme = "https" if s.get("https") else "http"
        devices, errors = [], {}
        for host in hosts(s["devices"]):  # one connection each: every device has its own certificate
            async with httpx.AsyncClient(verify=verify_for(s, host), timeout=10, auth=(s["username"], secret)) as http:
                try:
                    devices.append(await self._device(http, f"{scheme}://{host}/rest", host,
                                                      export=bool(s.get("config_backup")) and host in self.backup_due))
                except ApiError as e:
                    errors[host] = str(e)
        if not devices:
            raise ApiError("; ".join(f"{h}: {e}" for h, e in errors.items()) or "no devices")
        names = ", ".join(d["name"] for d in devices)
        snap = {"devices": devices, "errors": errors, "_summary": f"{len(devices)} read: {names}"}
        if s.get("os_updates"):
            await self._newest({d["channel"] or "stable" for d in devices}, now)
            snap["latest"] = {ch: v for ch, (v, _) in self.latest.items()}
            snap["latest_error"] = dict(self.latest_error)
        return snap

    async def _newest(self, channels: set[str], now: float) -> None:
        """Newest RouterOS per channel from MikroTik's site, at most once a day (once an hour after a failure)."""
        due = [ch for ch in sorted(channels) if re.fullmatch(r"[a-z-]+", ch)
               and (ch not in self.latest or now - self.latest[ch][1] >= LATEST_EVERY)
               and (ch not in self.latest_tried or now - self.latest_tried[ch] >= LATEST_RETRY)]
        if not due:
            return
        async with httpx.AsyncClient(timeout=10) as http:
            for ch in due:
                self.latest_tried[ch] = now
                try:
                    r = await http.get(LATEST_URL.format(channel=ch))
                    r.raise_for_status()
                    version = r.text.split()[0] if r.text.split() else ""
                    if not re.match(r"\d+\.\d+", version):
                        raise ValueError(f"unexpected answer {r.text[:40]!r}")
                    self.latest[ch] = (version, now)
                    self.latest_error.pop(ch, None)
                except (httpx.HTTPError, ValueError) as e:
                    self.latest_error[ch] = why(e) if isinstance(e, httpx.HTTPError) else str(e)

    async def _device(self, http, base: str, host: str, export: bool = False) -> dict:
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
        try:  # the update channel (stable, long-term…); reading it doesn't start an update check on the device
            channel = (await get("system/package/update")).get("channel") or ""
        except ApiError:
            channel = ""
        ifs = await get("interface")
        try:  # the router's clock: its port times are local time
            offset = gmt_offset((await get("system/clock")).get("gmt-offset"))
        except ApiError:
            offset = None
        copy, copy_error = None, ""
        if export:  # the configuration as text; read-only, and the read group gets it without secrets
            self.backup_tried[host] = time.time()
            try:
                r = await http.post(f"{base}/execute", json={"script": "/export", "as-string": ""}, timeout=60)
                if r.status_code >= 300:
                    raise ApiError(f"HTTP {r.status_code}: {r.text[:120]}")
                copy = (r.json() or {}).get("ret") or ""
                if "/system identity" not in copy:
                    raise ApiError("the export came back incomplete")
            except (httpx.HTTPError, ValueError, ApiError) as e:
                copy, copy_error = None, why(e) if isinstance(e, httpx.HTTPError) else str(e)
        cpus = [float(res.get("cpu-load", 0))]
        for _ in range(CPU_SAMPLES - 1):
            await asyncio.sleep(CPU_GAP)
            cpus.append(float((await get("system/resource")).get("cpu-load", 0)))
        return {
            "host": host, "name": ident.get("name") or host, "export": copy, "export_error": copy_error,
            "model": res.get("board-name"), "version": res.get("version"), "channel": channel,
            "cpu": statistics.median(cpus),
            "mem": 100 * (1 - float(res.get("free-memory", 0)) / float(res.get("total-memory", 1))),
            "uptime": uptime_s(res.get("uptime", "")),
            "temp": next((float(h["value"]) for h in health if "temperature" in h.get("name", "")), None),
            "voltage": next((float(h["value"]) for h in health if h.get("name") == "voltage"), None),
            "boot": board.get("current-firmware"), "boot_upgrade": board.get("upgrade-firmware"),
            "ports": [{"name": i["name"], "comment": i.get("comment") or "",
                       "running": str(i.get("running")) == "true", "disabled": str(i.get("disabled")) == "true",
                       "errors": int(i.get("rx-error", 0) or 0) + int(i.get("tx-error", 0) or 0),
                       "rx_bytes": num(i.get("rx-byte")), "tx_bytes": num(i.get("tx-byte")),
                       "rx_drops": num(i.get("rx-drop")), "tx_drops": num(i.get("tx-drop")),
                       "last_down": router_ts(i.get("last-link-down-time"), offset),
                       "last_up": router_ts(i.get("last-link-up-time"), offset)}
                      for i in ifs if i.get("type") in ("ether", "sfp", "combo", "sfp-sfpplus", "sfpplus")],
        }

    async def readings(self, rt, settings, now):
        on = bool(settings.get("config_backup"))
        last = rt.db.get(f"{self.id}.backup_ok", {}) if on else {}
        self.backup_due = {h for h in hosts(settings.get("devices", "")) if on
                           and (h not in last or now - last[h] >= BACKUP_EVERY)
                           and (h not in self.backup_tried or now - self.backup_tried[h] >= BACKUP_RETRY)}
        before = self.snap_ts
        out = await super().readings(rt, settings, now)
        if self.snap is not None and on:
            if self.snap_ts != before:
                self.store_copies(rt, self.snap, now)
            self.snap["backup"] = self.backup_state(rt)
        return out

    def device_events(self, rt, prev):
        out = []
        if not self.snap:
            return out
        for d in self.snap["devices"]:
            booted(prev, f"{d['host']}:boot", self.snap_ts - d["uptime"] if d.get("uptime") else None,
                   d["name"], d["name"], out)
            for p in d["ports"]:
                key, down, up = f"{d['host']}:{p['name']}:down", p.get("last_down"), p.get("last_up")
                old = prev.get(key)
                if down and old is not None and down > old + 1:
                    port = f"{d['name']} {p['name']}" + (f" ({p['comment']})" if p["comment"] else "")
                    back = f", back after {up - down:.0f} s" if up and up >= down else ", still down"
                    out.append(DevEvent(down, "port_down", port, f"Port lost link{back}"))
                if down:
                    prev[key] = down
                elif old is None:
                    prev[key] = 0  # never went down since boot: the first down is news
        return out

    def store_copies(self, rt, snap, now):
        """Save the exports of this read: a new row only when the configuration changed; keep BACKUP_KEEP each."""
        ok = rt.db.get(f"{self.id}.backup_ok", {})
        for d in snap["devices"]:
            text, err = d.pop("export", None), d.pop("export_error", "")
            if err:
                self.backup_error[d["host"]] = err
            if text is None:
                continue
            self.backup_error.pop(d["host"], None)
            ok[d["host"]] = now
            last = rt.db.one("SELECT text FROM device_configs WHERE site=? AND module=? AND device=? ORDER BY ts DESC LIMIT 1",
                             (rt.cfg.site, self.id, d["host"]))
            if last is None or config_body(last["text"]) != config_body(text):
                rt.db.run("INSERT INTO device_configs(site,module,device,name,ts,text) VALUES(?,?,?,?,?,?)",
                          (rt.cfg.site, self.id, d["host"], d["name"], now, text.replace("\r\n", "\n")))
                rt.db.run("DELETE FROM device_configs WHERE site=? AND module=? AND device=? AND id NOT IN "
                          "(SELECT id FROM device_configs WHERE site=? AND module=? AND device=? ORDER BY ts DESC LIMIT ?)",
                          (rt.cfg.site, self.id, d["host"], rt.cfg.site, self.id, d["host"], BACKUP_KEEP))
        rt.db.set(f"{self.id}.backup_ok", ok)

    def backup_state(self, rt) -> dict:
        """Per host: last good copy, newest version and how many versions, the last error."""
        ok = rt.db.get(f"{self.id}.backup_ok", {})
        rows = rt.db.all("SELECT device, MAX(ts) newest, COUNT(*) n FROM device_configs WHERE site=? AND module=? "
                         "GROUP BY device", (rt.cfg.site, self.id))
        by = {r["device"]: r for r in rows}
        return {h: {"ok": ok.get(h), "newest": by[h]["newest"] if h in by else None, "versions": by[h]["n"] if h in by else 0,
                    "error": self.backup_error.get(h, "")} for h in set(ok) | set(by) | set(self.backup_error)}

    def page_ctx(self, rt) -> dict:
        ctx = super().page_ctx(rt)
        rows = rt.db.all("SELECT id, device, name, ts, LENGTH(text) size FROM device_configs WHERE site=? AND module=? "
                         "ORDER BY name, device, ts DESC", (rt.cfg.site, self.id))
        groups: dict[str, list] = {}
        for r in rows:
            groups.setdefault(r["device"], []).append(dict(r))
        ctx["config_copies"] = [{"device": h, "name": v[0]["name"] or h, "rows": v,
                                 "error": self.backup_error.get(h, "")} for h, v in groups.items()]
        return ctx

    def series(self, site: str) -> list[str]:
        if not self.snap:
            return []
        devs, ts = self.snap["devices"], self.snap_ts
        out = lines("hw_mikrotik_device", [
            ({"site": site, "device": d["name"], "host": d["host"], "model": d.get("model") or ""},
             {"cpu_percent": d.get("cpu"), "mem_percent": d.get("mem"), "temp_celsius": d.get("temp"),
              "voltage_volts": d.get("voltage"), "uptime_seconds": d.get("uptime")}) for d in devs], ts)
        out += lines("hw_mikrotik_port", [
            ({"site": site, "device": d["name"], "port": p["name"], "comment": p["comment"]},
             {"up": float(p["running"]), "disabled": float(p["disabled"]), "errors": float(p["errors"]),
              "rx_bytes": p.get("rx_bytes"), "tx_bytes": p.get("tx_bytes"),
              "rx_drops": p.get("rx_drops"), "tx_drops": p.get("tx_drops")})
            for d in devs for p in d["ports"]], ts)
        return out

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
            ordered = sorted(vals, key=lambda x: x[0], reverse=not lower)  # the worst device first
            add(cid, level(v, th[cid], lower), " | ".join(f"{n} {x:.0f} {unit}" for x, n in ordered), v)

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

        if s.get("config_backup"):
            st = snap.get("backup") or {}
            bad, waiting, good, changed = [], [], [], []
            for d in devs:
                b = st.get(d["host"]) or {}
                if b.get("ok") is None or now - b["ok"] > BACKUP_STALE:
                    if b.get("error") or b.get("ok") is not None:
                        age = f"none for {(now - b['ok']) / 86400:.0f} days" if b.get("ok") else "no copy yet"
                        bad.append(f"{d['name']} ({age}" + (f": {b['error']}" if b.get("error") else "") + ")")
                    else:
                        waiting.append(d["name"])
                else:
                    good.append(d["name"])
                if b.get("versions", 0) > 1 and b.get("newest") and now - b["newest"] < CHANGED_FOR:
                    changed.append(f"{d['name']} at {time.strftime('%d %b %H:%M', time.localtime(b['newest']))}")
            if bad:
                add("backup", WARNING, "No configuration copy of " + ", ".join(bad))
            elif waiting:
                out.append(CheckResult(f"{self.id}.backup", "Configuration copies", OK,
                                       "first copy at the next read: " + ", ".join(waiting), unknown=True))
            else:
                add("backup", OK, "daily copies of " + ", ".join(good))
            add("changed", WARNING if changed else OK,
                "Configuration changed: " + ", ".join(changed) + " - see the copies on the MikroTik page"
                if changed else "no change in the last 24 h")

        if s.get("os_updates"):
            latest, failed = snap.get("latest") or {}, snap.get("latest_error") or {}
            behind, unknown = [], []
            for d in devs:
                ch = d.get("channel") or "stable"
                have = str(d.get("version") or "").split()[0]
                if ch not in latest:
                    unknown.append(ch)
                elif version_key(latest[ch]) > version_key(have):
                    behind.append(f"{d['name']} {have} → {latest[ch]} ({ch})")
            if behind:
                add("routeros", WARNING, "RouterOS update available: " + ", ".join(behind)
                    + " - System → Packages → Check For Updates → Download & Install (the device reboots)")
            elif unknown:
                why_not = "; ".join(f"{c}: {failed[c]}" for c in sorted(set(unknown)) if c in failed)
                r = CheckResult(f"{self.id}.routeros", "RouterOS updates", OK,
                                "newest version not known yet" + (f" - upgrade.mikrotik.com: {why_not}" if why_not else ""),
                                unknown=True)
                out.append(r)
            else:
                add("routeros", OK, "all devices on the newest RouterOS of their channel ("
                    + ", ".join(f"{c} {v}" for c, v in sorted(latest.items())) + ")")
        return out

    def tables(self, snap, s):
        latest = snap.get("latest") if s.get("os_updates") else None
        t = [Table("Devices", ["Device", "Model", "RouterOS"] + (["Newest"] if latest is not None else [])
                   + ["RouterBOOT", "CPU", "Memory", "Temp", "Voltage", "Uptime"],
                   [[d["name"], d["model"], d["version"]]
                    + ([f"{latest.get(d.get('channel') or 'stable') or '?'} ({d.get('channel') or 'stable'})"]
                       if latest is not None else [])
                    + [d["boot"], f"{d['cpu']:.0f} %", f"{d['mem']:.0f} %",
                     f"{d['temp']:.0f} °C" if d["temp"] is not None else None,
                     f"{d['voltage']:.1f} V" if d["voltage"] is not None else None,
                     f"{d['uptime'] / 86400:.1f} days"] for d in snap["devices"]])]
        for d in snap["devices"]:
            t.append(Table(f"Ports on {d['name']}", ["Port", "Comment", "State", "Errors"],
                           [[p["name"], p["comment"], "disabled" if p["disabled"] else ("up" if p["running"] else "down"),
                             p["errors"]] for p in d["ports"]], widths=["20%", "50%", "15%", "15%"]))
        return t


plugin = MikroTikModule()
