"""UPS through NUT: plain-text protocol on port 3493 (LIST UPS / LIST VAR), read-only."""

from __future__ import annotations

import asyncio
import logging
import re
import shlex
import time

from .. import ERROR, OK, WARNING, CheckDef, CheckResult, DevEvent, level
from ..apibase import ApiError, ApiModule, Field, Table
from ...metrics import lines, num as _num

log = logging.getLogger("homewatch.ups")

CHECKS = [
    CheckDef("api", "NUT server reachable", "api", help="ERROR when the NUT server can't be read or its data is stale."),
    CheckDef("power", "Mains power", "api", help="ERROR when the UPS runs on battery or a forced shutdown is under way."),
    CheckDef("battery", "Battery charge", "api", unit="%", warn=50, error=30, editable=("warn", "error"),
             lower_is_worse=True),
    CheckDef("runtime", "Runtime", "api", unit="min", warn=10, error=5, editable=("warn", "error"),
             lower_is_worse=True, help="Estimated minutes on battery at the current load."),
    CheckDef("load", "Load", "api", unit="%", warn=80, error=95, editable=("warn", "error")),
    CheckDef("health", "UPS health", "api",
             help="WARNING when the UPS asks for a battery replacement; ERROR when it reports an overload."),
]
FLAGS = {"OL": "on mains", "OB": "on battery", "LB": "low battery", "RB": "replace battery", "CHRG": "charging",
         "DISCHRG": "discharging", "OVER": "overload", "BYPASS": "on bypass", "CAL": "calibrating", "FSD": "forced shutdown",
         "TRIM": "trimming voltage", "BOOST": "boosting voltage", "OFF": "off"}
_HOST = re.compile(r"^[a-zA-Z0-9.-]+$")
_NAME = re.compile(r"^[a-zA-Z0-9._-]*$")


async def nut(host: str, port: int, commands: list[str], timeout: float = 6) -> list[list[str]]:
    """Send commands; return the answer lines (split like a shell) of each, without BEGIN/END."""
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except (OSError, TimeoutError) as e:
        raise ApiError(f"can't reach the NUT server at {host}:{port} ({type(e).__name__})") from None
    out = []
    try:
        for cmd in commands:
            writer.write(cmd.encode() + b"\n")
            await writer.drain()
            lines = []
            while True:
                raw = await asyncio.wait_for(reader.readline(), timeout)
                if not raw:
                    raise ApiError("the NUT server closed the connection")
                line = raw.decode(errors="replace").strip()
                if line.startswith("ERR "):
                    code = line[4:]
                    raise ApiError({"DATA-STALE": "the UPS data is stale (the NUT driver lost the UPS)",
                                    "UNKNOWN-UPS": "no UPS with that name on this NUT server",
                                    "ACCESS-DENIED": "the NUT server refused access"}.get(code, f"NUT error {code}"))
                if line.startswith("BEGIN "):
                    continue
                if line.startswith("END "):
                    break
                lines.append(shlex.split(line))
                if not cmd.startswith("LIST "):  # GET answers with one line, no BEGIN/END
                    break
            out.append(lines)
        writer.write(b"LOGOUT\n")
    except TimeoutError:
        raise ApiError("the NUT server did not answer in time") from None
    finally:
        writer.close()
    return out


WATCH_S = 10  # s: how often the watcher asks for the UPS status alone (one short query, no login)


class UPSModule(ApiModule):
    id = "ups"
    name = "UPS"
    icon = "battery"
    headline = ("battery", "battery charge")
    every_min = 2  # fast: alarms, safety sensors and power events must be reported quickly
    checks = CHECKS
    description = ("Watches a UPS through a NUT server (Network UPS Tools), for example on your Proxmox host or NAS: "
                   "mains power, battery charge, runtime, load and battery health. Reading needs no login. "
                   "Besides the full reading, the UPS status alone is asked every 10 seconds, so that even a "
                   "short power cut is seen, with its time, and checked at once.")
    setup_title = "Before you connect"
    setup = [
        ("text", "homeWatch reads from an existing NUT server; no account is needed. The NUT server must listen on "
                 "the network, not only on 127.0.0.1. In /etc/nut/upsd.conf on the NUT host, for example:"),
        ("code", "LISTEN 0.0.0.0 3493   # or the host's LAN address\n# then: systemctl restart nut-server"),
        ("text", "Synology DSM's built-in network UPS server only allows the addresses you list under "
                 "Control Panel → Hardware & Power → UPS → Permitted DiskStation Devices: add this homeWatch host."),
    ]
    fields = [
        Field("host", "NUT server address", "192.168.1.10"),
        Field("port", "Port", "3493", kind="number", default=3493),
        Field("ups", "UPS name", "leave empty for the first one", default=""),
    ]

    def tls_hosts(self, settings):
        return []

    def transport(self, rt):
        return "NUT, unencrypted; only reads, with no login", True

    def validate(self, form, old):
        out, err = super().validate(form, old)
        if err:
            return out, err
        if not _HOST.match(out["host"] or ""):
            return out, "NUT server address: an IP address or host name"
        if not _NAME.match(out["ups"] or ""):
            return out, "UPS name: letters, digits, - _ . only"
        if not 1 <= out["port"] <= 65535:
            return out, "Port: 1 to 65535"
        return out | {"port": int(out["port"])}, ""

    async def fetch(self, s, secret, now):
        host, port = s["host"], int(s.get("port") or 3493)
        name = s.get("ups") or ""
        if not name:
            (upss,) = await nut(host, port, ["LIST UPS"])
            if not upss:
                raise ApiError("the NUT server has no UPS")
            name = upss[0][1]
        (rows,) = await nut(host, port, [f"LIST VAR {name}"])
        v = {r[2]: r[3] for r in rows if len(r) >= 4 and r[0] == "VAR"}
        model = " ".join(x for x in (v.get("ups.mfr", ""), v.get("ups.model", v.get("device.model", ""))) if x).strip()
        return {"name": name, "vars": v, "_summary": f"{name}: {model or 'UPS'}"}

    # NUT variable -> metric field
    SERIES = {"battery.charge": "battery_charge_percent", "battery.runtime": "battery_runtime_seconds",
              "battery.voltage": "battery_voltage_volts", "ups.load": "load_percent", "input.voltage": "input_voltage_volts",
              "output.voltage": "output_voltage_volts", "ups.realpower": "power_watts",
              "ups.realpower.nominal": "power_nominal_watts", "ups.temperature": "temp_celsius",
              "input.frequency": "input_frequency_hertz"}

    def device_events(self, rt, prev):
        """A fallback for the watcher: it normally sees the change first, and then a reading adds nothing."""
        if not self.snap or prev.get("ob_ts", 0) >= self.snap_ts:
            return []
        on_battery = "OB" in self.snap["vars"].get("ups.status", "").split()
        old, prev["ob"], prev["ob_ts"] = prev.get("ob"), on_battery, self.snap_ts
        if old is None or old == on_battery:
            return []
        return [power_event(self.snap_ts, on_battery, self.snap["name"])]

    def loops(self, rt) -> list:
        return [self.watch(rt)]

    async def watch(self, rt) -> None:
        """Ask for the UPS status alone every WATCH_S seconds: a power cut of half a minute falls between two full
        readings. A change is an event at once, and the checks run now on a fresh reading."""
        await asyncio.sleep(15)
        while True:
            try:
                s = self.load(rt)
                name = s.get("ups") or (self.snap or {}).get("name")
                if self.enabled(s) and s.get("host") and name:
                    (rows,) = await nut(s["host"], int(s.get("port") or 3493), [f"GET VAR {name} ups.status"], 5)
                    flags = rows[0][3].split() if rows and len(rows[0]) >= 4 and rows[0][0] == "VAR" else []
                    if flags:
                        self.watched(rt, "OB" in flags, name, time.time())
            except ApiError:
                pass  # the full reading reports a NUT server that can't be read
            except Exception:  # noqa: BLE001 - the watcher must survive anything
                log.exception("ups watcher")
            await asyncio.sleep(WATCH_S)

    def watched(self, rt, on_battery: bool, name: str, now: float) -> bool:
        if not rt.note_device_state(self.id, "ob", on_battery, now, lambda: power_event(now, on_battery, name)):
            return False
        self.reread()
        rt.wake.set()
        return True

    def series(self, site: str) -> list[str]:
        snap = self.snap
        if not snap:
            return []
        v = snap["vars"]
        flags = v.get("ups.status", "").split()
        fields = {f: _num(v.get(k)) for k, f in self.SERIES.items()}
        if fields["power_watts"] is None and fields["power_nominal_watts"] and fields["load_percent"] is not None:
            fields["power_watts"] = fields["power_nominal_watts"] * fields["load_percent"] / 100  # estimate
        fields |= {"on_battery": float("OB" in flags), "low_battery": float("LB" in flags),
                   "replace_battery": float("RB" in flags), "online": float("OL" in flags)}
        return lines("hw_ups", [({"site": site, "ups": snap["name"]}, fields)], self.snap_ts)

    def check(self, s, th, snap, now):
        out = []

        def add(cid, status, detail, value=None):
            dd = next(c for c in CHECKS if c.id == cid)
            out.append(CheckResult(f"{self.id}.{cid}", dd.name, status, detail, value, dd.unit))

        v = snap["vars"]
        flags = v.get("ups.status", "").split()
        words = ", ".join(FLAGS.get(f, f) for f in flags) or "unknown"

        def num(key):
            try:
                return float(v[key])
            except (KeyError, ValueError):
                return None

        volt = num("input.voltage")
        if "FSD" in flags:
            add("power", ERROR, f"forced shutdown under way ({words})")
        elif "OB" in flags:
            add("power", ERROR, f"on battery ({words})" + (f", input {volt:.0f} V" if volt is not None else ""))
        else:
            add("power", OK, words + (f", input {volt:.0f} V" if volt is not None else ""))

        for cid, key, unit, scale in (("battery", "battery.charge", "%", 1), ("runtime", "battery.runtime", "min", 60),
                                      ("load", "ups.load", "%", 1)):
            x = num(key)
            if x is None:
                add(cid, OK, "not reported by this UPS")
            else:
                x /= scale
                add(cid, level(x, th[cid], CHECKS[[c.id for c in CHECKS].index(cid)].lower_is_worse),
                    f"{x:.0f} {unit}" + (" (charging)" if cid == "battery" and "CHRG" in flags else ""), x)

        bad = [FLAGS[f] for f in ("RB", "OVER") if f in flags]
        add("health", ERROR if "OVER" in flags else WARNING if bad else OK,
            ", ".join(bad) if bad else "no battery or overload warnings")
        return out

    def tables(self, snap, s):
        v = snap["vars"]
        keys = ("ups.status", "battery.charge", "battery.runtime", "ups.load", "input.voltage", "output.voltage",
                "battery.voltage", "battery.charge.low", "battery.runtime.low", "ups.realpower.nominal",
                "battery.mfr.date", "ups.mfr", "ups.model", "ups.firmware", "driver.name")
        return [Table(f"UPS {snap['name']}", ["Variable", "Value"], [[k, v[k]] for k in keys if k in v])]


plugin = UPSModule()


def power_event(ts: float, on_battery: bool, name: str) -> DevEvent:
    return DevEvent(ts, "on_battery" if on_battery else "on_mains", name,
                    "UPS on battery (mains power lost)" if on_battery else "UPS back on mains power", exact=False)
