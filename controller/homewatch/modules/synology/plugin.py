"""Synology DSM: Web API with a dedicated account; logs in, reads, logs out every 10 minutes."""

from __future__ import annotations

from urllib.parse import urlsplit

import httpx

from .. import ERROR, OK, WARNING, CheckDef, CheckResult, level
from ..pinning import verify_for
from ..apibase import SELF_SIGNED, why, ApiError, ApiModule, Field, Table, https_url
from . import snmp

CHECKS = [
    CheckDef("api", "DSM reachable", "api", help="ERROR when homewatch can't log in to DSM or read it."),
    CheckDef("system", "System", "api", unit="°C", warn=60, error=70, editable=("warn", "error"),
             help="System temperature; ERROR when DSM reports a crashed system."),
    CheckDef("cpu", "CPU", "api", unit="%", warn=85, error=95, editable=("warn", "error")),
    CheckDef("mem", "Memory", "api", unit="%", warn=90, error=95, editable=("warn", "error")),
    CheckDef("disks", "Disks", "api", unit="°C", warn=50, error=60, editable=("warn", "error"),
             help="Hottest disk; ERROR when a disk is not normal, SMART fails or bad sectors appear."),
    CheckDef("storage", "Pools and volumes", "api", unit="%", warn=85, error=95, editable=("warn", "error"),
             help="Fullest volume; ERROR when a pool or volume is degraded or crashed."),
    CheckDef("updates", "DSM updates", "api", help="WARNING when a DSM update is available."),
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
    checks = CHECKS
    every = 600  # each read is a DSM login; keep the connection log quiet
    description = ("Watches a Synology NAS through the DSM Web API: system temperature, CPU, memory, disks, storage "
                   "pools and volumes, DSM updates and, over SNMPv3, fans and power supply. Nothing on the NAS is changed.")
    setup_title = "First create a separate account for homewatch"
    setup = [
        ("text", "DSM has no read-only administrator role, and its system and storage information is only "
                 "available to administrators. So create a separate account for homewatch:"),
        ("text", "Control Panel → User & Group → Create: name it homewatch, add it to the administrators group, "
                 "and on the next pages set No access to every shared folder and Deny for every application "
                 "except DSM. If 2-step verification is enforced, exempt this account."),
        ("text", "homewatch only reads; it logs in every 10 minutes and logs out right after."),
        ("text", "Optional, fans and power supply: Control Panel → Terminal & SNMP → SNMP → enable the SNMP "
                 "service, tick SNMPv3, and create a user with authentication SHA and privacy AES. Enter that "
                 "user and both passwords below. Leave the SNMP user empty to skip this check."),
    ]
    fields = [
        Field("url", "NAS address", "https://192.168.1.20:5001"),
        Field("username", "User name", "homewatch", default="homewatch"),
        Field("verify", "Check the HTTPS certificate", kind="check", default=True,
              help=SELF_SIGNED),
        Field("snmp_user", "SNMPv3 user (optional)", "homewatch",
              help="For the fans and power check. Empty: that check is skipped."),
        Field("snmp_auth_protocol", "SNMPv3 authentication", "SHA", default="SHA",
              help="SHA or SHA256, as set for the user in DSM. Privacy is always AES."),
        Field("snmp_auth", "SNMPv3 authentication password", secret=True, optional=True),
        Field("snmp_priv", "SNMPv3 privacy password", secret=True, optional=True),
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
        proto = (out.get("snmp_auth_protocol") or "SHA").upper()
        if proto not in snmp.AUTH:
            return out, "SNMPv3 authentication: SHA or SHA256"
        return out | {"url": url, "snmp_auth_protocol": proto}, ""

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
            "update": None if upgrade is None else bool((upgrade.get("update") or {}).get("available")),
            "update_version": ((upgrade or {}).get("update") or {}).get("version", ""),
        }
        snap["hw"], snap["hw_error"] = None, ""
        if s.get("snmp_user"):
            if not (s.get("snmp_auth") and s.get("snmp_priv")):
                snap["hw_error"] = "enter both SNMPv3 passwords in the settings"
            else:
                try:
                    snap["hw"] = await snmp.read(urlsplit(s["url"]).hostname, s["snmp_user"], s["snmp_auth"],
                                                 s["snmp_priv"], s.get("snmp_auth_protocol") or "SHA")
                except snmp.SnmpError as e:
                    snap["hw_error"] = str(e)
        snap["_summary"] = f"{snap['info'].get('model', 'NAS')} {snap['info'].get('firmware_ver', '')}".strip()
        return snap

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

        if not s.get("snmp_user"):
            add("hardware", OK, "not watched (no SNMPv3 user in the settings)")
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
