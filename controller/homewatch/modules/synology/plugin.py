"""Synology DSM: Web API with a dedicated account; logs in, reads, logs out every 10 minutes."""

from __future__ import annotations

from urllib.parse import urlsplit

import httpx

from .. import ERROR, OK, WARNING, CheckDef, CheckResult, DevEvent, booted, level
from ...metrics import lines, num
from ..pinning import verify_for
from ..apibase import SELF_SIGNED, why, ApiError, ApiModule, Field, Table, https_url
from . import snmp

CHECKS = [
    CheckDef("api", "DSM reachable", "api", help="ERROR when homeWatch can't log in to DSM or read it."),
    CheckDef("system", "System", "api", unit="°C", warn=60, error=70, editable=("warn", "error"),
             help="System temperature; ERROR when DSM reports a crashed system."),
    CheckDef("cpu", "CPU", "api", unit="%", warn=85, error=95, editable=("warn", "error")),
    CheckDef("mem", "Memory", "api", unit="%", warn=90, error=95, editable=("warn", "error")),
    CheckDef("disks", "Disks", "api", unit="°C", warn=50, error=60, editable=("warn", "error"),
             help="Hottest disk; ERROR when a disk is not normal, SMART fails or bad sectors appear."),
    CheckDef("storage", "Pools and volumes", "api", unit="%", warn=85, error=95, editable=("warn", "error"),
             help="Fullest volume; ERROR when a pool or volume is degraded or crashed."),
    CheckDef("updates", "DSM updates", "api", unit="days", warn=14, editable=("warn",), todo=True,
             help="A DSM update is available. Listed under To do; WARNING once it has waited this many days."),
    CheckDef("hardware", "Fans and power", "api",
             help="Optional, over SNMPv3: ERROR when the NAS reports a failed fan, power supply or system; "
                  "WARNING when SNMP can't be read. DSM's Web API doesn't report these."),
]
DSM_ERRORS = {400: "wrong user name or password", 401: "account disabled", 402: "permission denied",
              403: "the account needs a 2-step verification code: turn 2-step verification off for it",
              404: "2-step verification failed", 406: "2-step verification is enforced for this account",
              407: "this address is blocked by DSM's auto-block", 408: "the password has expired",
              409: "the password must be changed", 410: "the password must be changed"}
BUSY = ("repairing", "expanding", "migrating", "data_scrubbing", "parity_checking", "consistency_checking")


class SynologyModule(ApiModule):
    id = "synology"
    name = "Synology NAS"
    icon = "drive"
    headline = ("system", "system temperature")
    checks = CHECKS
    every_min = 10  # each read is a DSM login; keep the connection log quiet
    description = ("Watches a Synology NAS through the DSM Web API: system temperature, CPU, memory, disks, storage "
                   "pools and volumes, DSM updates and, over SNMPv3, fans and power supply. Nothing on the NAS is changed.")
    setup_title = "First create a separate account for homewatch"
    setup = [
        ("text", "DSM has no read-only administrator role, and its system and storage information is only "
                 "available to administrators. So create a separate account for homewatch:"),
        ("text", "Control Panel → User & Group → Create: name it homewatch, add it to the administrators group, "
                 "and on the next pages set No access to every shared folder and Deny for every application "
                 "except DSM. If 2-step verification is enforced, exempt this account."),
        ("text", "homeWatch only reads; it logs in at each read (every 10 minutes unless you change it) and logs out right after."),
        ("text", "Optional, fans and power supply: Control Panel → Terminal & SNMP → SNMP → enable the SNMP "
                 "service, tick SNMPv3 and create a user (authentication SHA, privacy AES are the safe choices). "
                 "Then tick SNMPv3 below and enter the same user, protocols and passwords."),
    ]
    # line 1 the DSM login; lines 2 and 3 SNMPv3, their protocols and passwords in the same columns
    layout = [
        ["url", "username", "every_min", "_secret"],
        ["snmp_on", "snmp_user", "snmp_auth_protocol", "snmp_auth"],
        [None, "snmp_priv_on", "snmp_priv_protocol", "snmp_priv"],
    ]
    # tick boxes sit at the right of their cell, next to what they switch on
    layout_cols = "minmax(0, 240px) minmax(0, 1fr) 112px minmax(0, 1fr)"
    fields = [
        Field("url", "NAS address", "https://192.168.1.20:5001"),
        Field("username", "User name", "homewatch", default="homewatch"),
        Field("verify", "Check the HTTPS certificate", kind="check", default=True,
              help=SELF_SIGNED),
        Field("snmp_on", "SNMPv3", kind="check", default=False),
        Field("snmp_user", "SNMPv3 user name", "homewatch", when="snmp_on"),
        Field("snmp_auth_protocol", "Protocol", kind="select", options=snmp.AUTH, default="SHA", when="snmp_on"),
        Field("snmp_auth", "Password", secret=True, optional=True, when="snmp_on"),
        Field("snmp_priv_on", "SNMP privacy", kind="check", default=True, when="snmp_on"),
        Field("snmp_priv_protocol", "Protocol", kind="select", options=snmp.PRIV, default="AES",
              when="snmp_on snmp_priv_on"),
        Field("snmp_priv", "Privacy password", secret=True, optional=True, when="snmp_on snmp_priv_on"),
    ]
    secret_label = "Password"

    def validate(self, form, old):
        out, err = super().validate(form, old)
        if err:
            return out, err
        url = https_url(out["url"], default_port=5001)
        if not url:
            return out, "NAS address: e.g. https://192.168.1.20:5001"
        if not out["username"]:
            return out, "Enter the user name"
        if out.get("snmp_on") and not out.get("snmp_user"):
            return out, "Enter the SNMPv3 user name, or untick SNMPv3"
        return out | {"url": url}, ""

    @staticmethod
    def _upgrade(s: dict, saved: dict | None) -> dict:
        """Settings from before 0.28 had no SNMP boxes: an SNMP user meant SNMPv3 with privacy (AES)."""
        if saved is not None and "snmp_on" not in saved:
            s = s | {"snmp_on": bool(saved.get("snmp_user")), "snmp_priv_on": True}
        return s

    def load(self, rt):
        return self._upgrade(super().load(rt), rt.db.get(self.key))

    def imported(self, settings):
        return super().imported(self._upgrade(settings, settings))

    async def fetch(self, s, secret, now):
        entry = f"{s['url']}/webapi/entry.cgi"
        async with httpx.AsyncClient(verify=verify_for(s, s["url"]), timeout=20) as http:  # noqa: S501
            async def call(api: str, method: str, version: int, sid: str | None = None, optional=False, **params):
                p = {"api": api, "method": method, "version": version, **params}
                if sid:
                    p["_sid"] = sid
                try:
                    r = await http.get(entry, params=p)
                except httpx.HTTPError as e:
                    raise ApiError(f"can't reach DSM ({why(e)})") from None
                body = r.json() if r.status_code == 200 else {}
                if not body.get("success"):
                    code = (body.get("error") or {}).get("code")
                    if optional:
                        return None
                    if api == "SYNO.API.Auth":
                        raise ApiError(f"DSM login failed: {DSM_ERRORS.get(code, f'error {code}')}")
                    raise ApiError(f"DSM refused {api} (error {code}); is the account an administrator?")
                return body.get("data") or {}

            # the password goes in the query string, which is how DSM's login works; over HTTPS only
            sid = (await call("SYNO.API.Auth", "login", 6, account=s["username"], passwd=secret,
                              session="homewatch", format="sid"))["sid"]
            try:
                info = await call("SYNO.Core.System", "info", 1, sid)
                status = await call("SYNO.Core.System.Status", "get", 1, sid, optional=True) or {}
                util = await call("SYNO.Core.System.Utilization", "get", 1, sid, optional=True) or {}
                storage = await call("SYNO.Storage.CGI.Storage", "load_info", 1, sid)
                upgrade = await call("SYNO.Core.Upgrade.Server", "check", 2, sid, optional=True)
            finally:
                try:
                    await http.get(entry, params={"api": "SYNO.API.Auth", "method": "logout", "version": 6,
                                                  "session": "homewatch", "_sid": sid})
                except httpx.HTTPError:
                    pass
        snap = {
            "info": {k: info.get(k) for k in ("model", "firmware_ver", "up_time", "sys_temp", "temperature_warning")},
            "crashed": bool(status.get("is_system_crashed")),
            "cpu": util.get("cpu"), "mem": (util.get("memory") or {}).get("real_usage"),
            "disks": [{k: d.get(k) for k in ("id", "name", "model", "status", "smart_status", "temp", "unc",
                                              "below_remain_life_thr", "exceed_bad_sector_thr")}
                      for d in storage.get("disks", [])],
            "pools": [{k: p.get(k) for k in ("id", "status", "device_type")} for p in storage.get("storagePools", [])],
            "volumes": [{"id": v.get("id"), "path": v.get("vol_path"), "status": v.get("status"),
                         "used": (v.get("size") or {}).get("used"), "total": (v.get("size") or {}).get("total")}
                        for v in storage.get("volumes", [])],
            "net": [{"device": n.get("device"), "rx": n.get("rx"), "tx": n.get("tx")} for n in util.get("network") or []],
            "diskio": {k: ((util.get("disk") or {}).get("total") or {}).get(k) for k in ("read_byte", "write_byte",
                                                                                           "utilization")},
            "update": None if upgrade is None else bool((upgrade.get("update") or {}).get("available")),
            "update_version": ((upgrade or {}).get("update") or {}).get("version", ""),
        }
        snap["hw"], snap["hw_error"] = None, ""
        if s.get("snmp_on"):
            priv = (s.get("snmp_priv_protocol") or "AES") if s.get("snmp_priv_on", True) else ""
            if not s.get("snmp_auth"):
                snap["hw_error"] = "enter the SNMPv3 password in the settings"
            elif priv and not s.get("snmp_priv"):
                snap["hw_error"] = "enter the SNMP privacy password in the settings, or untick SNMP privacy"
            else:
                try:
                    snap["hw"] = await snmp.read(urlsplit(s["url"]).hostname, s["snmp_user"], s["snmp_auth"],
                                                 s.get("snmp_priv") or "", s.get("snmp_auth_protocol") or "SHA", priv)
                except snmp.SnmpError as e:
                    snap["hw_error"] = str(e)
        snap["_summary"] = f"{snap['info'].get('model', 'NAS')} {snap['info'].get('firmware_ver', '')}".strip()
        return snap

    def device_events(self, rt, prev):
        out: list[DevEvent] = []
        if not self.snap:
            return out
        up = hms_s((self.snap.get("info") or {}).get("up_time"))
        booted(prev, "boot", self.snap_ts - up if up else None, "NAS", "The NAS", out)
        return out

    def series(self, site: str) -> list[str]:
        snap, ts = self.snap, self.snap_ts
        if not snap:
            return []
        i, cpu = snap["info"], snap.get("cpu") or {}
        load = sum(num(cpu.get(k)) or 0 for k in ("user_load", "system_load", "other_load")) if isinstance(cpu, dict) else num(cpu)
        name = i.get("model") or "NAS"
        hw = {k: float(v == 1) for k, v in (snap.get("hw") or {}).items() if v is not None}  # Synology MIB: 1 = normal
        io = snap.get("diskio") or {}
        out = lines("hw_synology_system", [({"site": site, "nas": name},
                    {"temp_celsius": num(i.get("sys_temp")), "cpu_percent": load, "mem_percent": num(snap.get("mem")),
                     "crashed": float(snap["crashed"]), "update_available": None if snap.get("update") is None
                     else float(snap["update"]), "disk_read_bytes_per_second": num(io.get("read_byte")),
                     "disk_write_bytes_per_second": num(io.get("write_byte")),
                     "disk_utilization_percent": num(io.get("utilization")), **{f"{k}_ok": v for k, v in hw.items()}})], ts)
        out += lines("hw_synology_net", [({"site": site, "nas": name, "interface": n["device"] or ""},
                     {"rx_bytes_per_second": num(n["rx"]), "tx_bytes_per_second": num(n["tx"])})
                     for n in snap.get("net", []) if n["device"] != "total"], ts)
        out += lines("hw_synology_disk", [({"site": site, "nas": name, "disk": d.get("name") or d.get("id") or "",
                                            "model": (d.get("model") or "").strip()},
                     {"temp_celsius": num(d.get("temp")), "bad_sectors": num(d.get("unc")),
                      "healthy": float(d.get("status") == "normal" and d.get("smart_status") in ("normal", None))})
                     for d in snap["disks"]], ts)
        out += lines("hw_synology_volume", [({"site": site, "nas": name, "volume": v.get("path") or v.get("id") or ""},
                     {"used_bytes": num(v.get("used")), "total_bytes": num(v.get("total")),
                      "healthy": float(v.get("status") == "normal")}) for v in snap["volumes"]], ts)
        return out

    def check(self, s, th, snap, now):
        out = []

        def add(cid, status, detail, value=None):
            d = next(c for c in CHECKS if c.id == cid)
            out.append(CheckResult(f"{self.id}.{cid}", d.name, status, detail, value, d.unit))

        i = snap["info"]
        t = i.get("sys_temp")
        if snap["crashed"]:
            add("system", ERROR, "DSM reports the system as crashed")
        elif t is not None:
            lvl = max(level(float(t), th["system"]), WARNING if i.get("temperature_warning") else OK)
            add("system", lvl, f"{t} °C, up {i.get('up_time', '?')}", float(t))
        else:
            add("system", OK, f"up {i.get('up_time', '?')}")

        cpu = snap.get("cpu")
        if cpu:
            v = float(cpu.get("user_load", 0)) + float(cpu.get("system_load", 0)) + float(cpu.get("other_load", 0))
            add("cpu", level(v, th["cpu"]), f"CPU {v:.0f} %", v)
        else:
            add("cpu", OK, "not reported")
        if snap.get("mem") is not None:
            add("mem", level(float(snap["mem"]), th["mem"]), f"memory {snap['mem']} %", float(snap["mem"]))
        else:
            add("mem", OK, "not reported")

        disks = snap["disks"]
        bad = []
        for d in disks:
            if d.get("status") not in ("normal", None) or d.get("smart_status") not in ("normal", None):
                bad.append(f"{d.get('name')} {d.get('status')}/{d.get('smart_status')}")
            elif (d.get("unc") or 0) > 0 or d.get("exceed_bad_sector_thr") or d.get("below_remain_life_thr"):
                bad.append(f"{d.get('name')} bad sectors {d.get('unc')}")
        temps = [(float(d["temp"]), d.get("name")) for d in disks if d.get("temp") is not None]
        if bad:
            add("disks", ERROR, "; ".join(bad))
        elif temps:
            v, name = max(temps)
            add("disks", level(v, th["disks"]), f"{len(disks)} disks normal, hottest {name} {v:.0f} °C", v)
        else:
            add("disks", OK, f"{len(disks)} disks normal" if disks else "No disks reported")

        problems, lvl = [], OK
        for x in snap["pools"] + snap["volumes"]:
            st = str(x.get("status") or "")
            if st in ("degraded", "crashed", "attention", "disabled") or "crash" in st or "degrad" in st:
                problems.append(f"{x.get('path') or x.get('id')} {st}")
                lvl = ERROR
            elif any(b in st for b in BUSY):
                problems.append(f"{x.get('path') or x.get('id')} {st}")
                lvl = max(lvl, WARNING)
        fills = []
        for v in snap["volumes"]:
            try:
                fills.append((100 * float(v["used"]) / float(v["total"]), v.get("path") or v.get("id")))
            except (TypeError, ValueError, ZeroDivisionError, KeyError):
                pass
        if problems:
            add("storage", lvl, "; ".join(problems))
        elif fills:
            p, name = max(fills)
            add("storage", level(p, th["storage"]), f"{len(snap['pools'])} pool(s) normal, fullest {name} {p:.0f} %", p)
        else:
            add("storage", OK, "No volumes reported")

        if snap.get("update") is None:
            add("updates", OK, "not readable")
        else:
            add("updates", WARNING if snap["update"] else OK,
                f"update available {snap.get('update_version') or ''}".strip() if snap["update"]
                else f"{i.get('firmware_ver', 'DSM')} is up to date")

        if not s.get("snmp_on"):
            add("hardware", OK, "not watched (SNMPv3 is off in the settings)")
        elif snap.get("hw_error"):
            add("hardware", WARNING, snap["hw_error"])
        elif snap.get("hw") is not None:
            state, texts = snmp.evaluate(snap["hw"])
            add("hardware", ERROR if state == "failed" else OK, "; ".join(texts))
        else:
            add("hardware", OK, "not read yet")
        return out

    def tables(self, snap, s):
        return [Table("Disks", ["Disk", "Model", "Status", "SMART", "Temperature", "Bad sectors"],
                      [[d.get("name"), d.get("model"), d.get("status"), d.get("smart_status"),
                        f"{d.get('temp')} °C" if d.get("temp") is not None else None, d.get("unc")] for d in snap["disks"]]),
                Table("Volumes", ["Volume", "Status", "Used"],
                      [[v.get("path"), v.get("status"),
                        f"{100 * float(v['used']) / float(v['total']):.0f} % of {float(v['total']) / 1e12:.1f} TB"
                        if v.get("used") and v.get("total") else None] for v in snap["volumes"]])]


plugin = SynologyModule()


def hms_s(text) -> float | None:
    """DSM's up_time ("442:22:37", hours:minutes:seconds) in seconds."""
    parts = str(text or "").strip().split(":")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return None
    h, m, sec = map(int, parts)
    return float(h * 3600 + m * 60 + sec)
