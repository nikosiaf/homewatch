"""Home Assistant: long-lived token of a dedicated non-admin user; reads /api/config and /api/states."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime

import httpx

from .. import ERROR, OK, WARNING, CheckDef, CheckResult, level
from ..pinning import verify_for
from ..apibase import SELF_SIGNED, why, ApiError, ApiModule, Choice, Field, Table, https_url
from . import rules as entity_rules

CHECKS = [
    CheckDef("api", "HA reachable", "api", help="ERROR when homewatch can't read Home Assistant's API."),
    CheckDef("core", "Core", "api", help="WARNING when Home Assistant is starting, stopping or in safe / recovery mode."),
    CheckDef("unavailable", "Unavailable entities", "api", warn=1, error=10, editable=("warn", "error"),
             help="Entities unavailable for 30 minutes or more, except ignored ones and domains you don't watch."),
    CheckDef("alarm", "Alarm panels", "api",
             help="ERROR when an alarm panel is triggered; WARNING when it is unavailable."),
    CheckDef("safety", "Safety sensors", "api",
             help="ERROR when a smoke, water / moisture, gas, CO, heat or safety sensor detects something."),
    CheckDef("tamper", "Tamper", "api", help="ERROR when a tamper sensor reports tampering."),
    CheckDef("batteries", "Batteries", "api", unit="%", warn=25, error=10, editable=("warn", "error"),
             lower_is_worse=True, help="Lowest battery level among the watched battery sensors."),
    CheckDef("updates", "Updates", "api", help="WARNING when updates are available (skipped versions don't count)."),
    CheckDef("backups", "Automatic backups", "api", unit="h", warn=26, error=50, editable=("warn", "error"),
             help="Age of the last successful automatic backup; ERROR when the last attempt failed."),
    CheckDef("addons", "Add-ons", "api",
             help="ERROR when a watched add-on is not running; WARNING when its Running sensor is unavailable. "
                  "An add-on is watched when its Running sensor is enabled in Home Assistant."),
    CheckDef("disk", "Disk", "api", unit="%", warn=80, error=90, editable=("warn", "error"),
             help="Disk use of the Home Assistant host, from the disk sensor chosen in the settings."),
    CheckDef("memory", "Memory", "api", unit="%", warn=80, error=90, editable=("warn", "error"),
             help="Memory use of the Home Assistant host, from the memory sensor chosen in the settings."),
    CheckDef("cpu", "CPU", "api", unit="%", warn=90, error=98, editable=("warn", "error"),
             help="Processor use of the Home Assistant host, from the CPU sensor chosen in the settings. "
                  "Short peaks are normal, so the limits are high."),
    CheckDef("rules", "Entity rules", "api",
             help="Your own rules on any entity (see Entity rules in the settings); the worst rule that fires sets "
                  "the level. WARNING when a rule matches no entity or a number rule gets no number."),
]
SETTLE = 30 * 60
FULL_EVERY = 30  # minutes between full state reads, which find essential entities and new updates
FULL_MIN, FULL_MAX = 5, 1440
NOT_READY = ("unavailable", "unknown")
MAX_SINGLE = 100  # more essential entities than this: one bulk read is lighter than this many requests
BACKUP = ("sensor.backup_last_successful_automatic_backup", "sensor.backup_last_attempted_automatic_backup")
NOISE = re.compile(r"^sensor\..*_(rx|tx)$")  # per-client traffic counters; they drop out whenever a client sleeps
SAFETY = {"smoke", "moisture", "gas", "carbon_monoxide", "heat", "safety"}  # binary_sensor device classes
# System Monitor's sensors, used when the disk / memory / CPU sensor fields are left empty
DISK, MEMORY = "sensor.system_monitor_disk_usage", "sensor.system_monitor_memory_usage"
CPU = "sensor.system_monitor_processor_use"
HOST = (("disk", "disk_sensor", "Disk use sensor", DISK), ("memory", "memory_sensor", "Memory use sensor", MEMORY),
        ("cpu", "cpu_sensor", "CPU use sensor", CPU))


def _domain(e) -> str:
    return e["entity_id"].split(".", 1)[0]


def _update(e) -> dict:
    attrs = e.get("attributes", {})
    return {"entity_id": e["entity_id"],
            **{k: attrs.get(k) for k in ("title", "installed_version", "latest_version", "skipped_version")}}


ADDON_ATTRS = {"device_class", "friendly_name", "icon", "entity_picture"}  # all a Supervisor Running sensor has


def _addon(e) -> bool:
    """An add-on's Running sensor (Supervisor integration). Other integrations use the 'running' class too
    (VM status, appliance switches), mostly with ids not ending in _running; the ones that do (Proxmox VE's
    "Node pve Backup running") carry attributes of their own, which the Supervisor's sensors never have."""
    attrs = e.get("attributes", {})
    return (e["entity_id"].startswith("binary_sensor.") and e["entity_id"].endswith("_running")
            and attrs.get("device_class") == "running" and set(attrs) <= ADDON_ATTRS)


def _special(e) -> bool:
    """States the alarm / safety / tamper / battery / add-on checks look at."""
    dom, dc = e["entity_id"].split(".", 1)[0], e.get("attributes", {}).get("device_class")
    return (dom == "alarm_control_panel" or (dom == "binary_sensor" and (dc in SAFETY or dc == "tamper"))
            or (dom == "sensor" and dc == "battery" and e.get("attributes", {}).get("unit_of_measurement") == "%")
            or _addon(e))


def _sensors(s) -> dict:
    """Host sensors to read: the chosen ones, else System Monitor's."""
    return {cid: s.get(key) or default for cid, key, _, default in HOST}


def _ts(value) -> float | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class HomeAssistantModule(ApiModule):
    id = "homeassistant"
    name = "Home Assistant"
    icon = "house"
    headline = ("cpu", "host CPU")
    every_min = 2  # fast: alarms, safety sensors and power events must be reported quickly
    checks = CHECKS
    description = ("Watches Home Assistant through the token of a non-admin user: core state, alarm panels, safety and "
                   "tamper sensors, batteries, available updates, automatic backups, add-ons and the host's disk, "
                   "memory and CPU, plus your own rules on any entity. Only those entities are read "
                   "at each check; the full list is read every 30 minutes (you can change this) to find new ones and new updates, and isn't kept. "
                   "Nothing in Home Assistant is changed.")
    setup_title = "First create an account and token for homewatch"
    setup = [
        ("text", "In Home Assistant: Settings → People → Users → Add user. Name it homewatch and leave "
                 "\"Administrator\" off."),
        ("text", "Log in as homewatch (a private browser window is easiest), open its profile → Security → "
                 "Long-lived access tokens → Create token, and paste the token below."),
        ("text", "Optional, add-ons: Settings → Devices & services → Home Assistant Supervisor → open an add-on's "
                 "device and enable its \"Running\" sensor (disabled by default). Enable it for each add-on that must "
                 "always run; homewatch reports it when it stops."),
        ("text", "Optional, disk, memory and CPU: add the System Monitor integration with its disk use, memory use "
                 "and processor use sensors, or name your own sensors after connecting."),
    ]
    fields = [
        Field("url", "Home Assistant address", "http://homeassistant.local:8123", width="wide"),
        Field("verify", "Check the HTTPS certificate", kind="check", default=True,
              help=SELF_SIGNED),
        Field("full_every", "Full read every (minutes)", "30", kind="number", default=FULL_EVERY,
              width="narrow", own_line=True,
              help=f"How often the whole entity list is read to find new entities and updates ({FULL_MIN} to {FULL_MAX})."),
        Field("disk_sensor", "Disk use sensor", DISK, section="more",
              help="Host disk use in %. Empty: System Monitor's."),
        Field("memory_sensor", "Memory use sensor", MEMORY, section="more",
              help="Host memory use in %. Empty: System Monitor's."),
        Field("cpu_sensor", "CPU use sensor", CPU, section="more",
              help="Host processor use in %. Empty: System Monitor's."),
        Field("rules", "Entity rules", "binary_sensor.alarm_panel_trouble_* is on warning\nsensor.heating_oil below 200 warning",
              kind="lines", section="more",
              help="One rule per line: <entity or pattern> is | is not | below | above <value> warning | error. "
                   "* and ? match any text in an entity id. Lines starting with # are notes. Examples: "
                   "binary_sensor.panel_zone_*_tamper is on error · sensor.panel_link is not online error · "
                   "sensor.oil_level below 100 error."),
        Field("watch_all", "Watch every entity", kind="check", default=False, section="more",
              help="Off: only the essential entities (alarm, safety, tamper, battery, add-on, backup, host and rule "
                   "entities) are read and listed. On: all of them are read at every check, so any one that becomes "
                   "unavailable can be reported (network traffic RX/TX sensors never are)."),
    ]
    secret_label = "Long-lived access token"
    secret_width = "wide"
    more_title = "Additional configuration"
    more_help = "Which entities to watch, which unavailable ones to ignore, the host sensors and your own rules."
    more_fields_title = "Host sensors"
    choices_empty = "Nothing is unavailable right now, so there is nothing to ignore."
    secret_placeholder = "eyJ0eXAiOiJKV1Qi…"
    filter_title = "Entities to watch"
    filter_help = ("Unticked entities are not checked or reported. New ones are watched, except in a domain where you "
                   "untick everything.")
    choices_title = "Unavailable entities to ignore"
    choices_help = ("These are unavailable now, grouped by domain. Ticked ones are ignored (on purpose, or long gone). "
                    "Entities that become unavailable later are reported; tick them here if that's expected.")

    def __init__(self):
        super().__init__()
        self._ids: list[str] | None = None  # essential entities found by the last full read
        self._updates: list[dict] = []
        self._total = 0
        self._found_ts = 0.0

    def reread(self):
        super().reread()
        self._ids = None  # the rules or sensors may name other entities: a full read finds them

    def validate(self, form, old):
        out, err = super().validate(form, old)
        if err:
            return out, err
        url = https_url(out["url"], allow_http=True)
        if not url:
            return out, "Home Assistant address: e.g. http://homeassistant.local:8123"
        if not FULL_MIN <= out["full_every"] <= FULL_MAX:
            return out, f"Full read every: from {FULL_MIN} to {FULL_MAX} minutes"
        for _, k, label, _ in HOST:
            v = (out.get(k) or "").strip()
            if v and not re.fullmatch(r"sensor\.[a-z0-9_]+", v):
                return out, f"{label}: an entity id like sensor.disk_use_percent"
            out[k] = v
        _, problem = entity_rules.parse(out.get("rules", ""))
        if problem:
            return out, problem
        return out | {"url": url, "full_every": round(out["full_every"])}, ""

    def notice(self, settings):
        if settings.get("url", "").startswith("http://"):
            return "Plain HTTP: the access token crosses your network unencrypted at every read. Use an https address if you can."
        return ""

    async def fetch(self, s, secret, now):
        async with httpx.AsyncClient(verify=verify_for(s, s["url"]), timeout=20,  # noqa: S501
                                     headers={"Authorization": f"Bearer {secret}"}) as http:
            async def get(path, missing_ok=False):
                try:
                    r = await http.get(f"{s['url']}/api/{path}")
                except httpx.HTTPError as e:
                    raise ApiError(f"can't reach Home Assistant ({why(e)})") from None
                if r.status_code == 401:
                    raise ApiError("Home Assistant rejected the token")
                if missing_ok and r.status_code == 404:
                    return None
                if r.status_code >= 300:
                    raise ApiError(f"Home Assistant answered HTTP {r.status_code} for /api/{path}")
                return r.json()

            config = await get("config")
            rules, _ = entity_rules.parse(s.get("rules", ""))
            everything = bool(s.get("watch_all"))
            full = (everything or self._ids is None or self.snap_ts == float("-inf")
                    or now - self._found_ts >= s.get("full_every", FULL_EVERY) * 60 or len(self._ids) > MAX_SINGLE)
            if full:
                states = await get("states")
                host = set(_sensors(s).values())
                self._ids = [e["entity_id"] for e in states
                             if _special(e) or e["entity_id"] in BACKUP or e["entity_id"] in host
                             or entity_rules.wanted(rules, e["entity_id"])]
                self._updates = [_update(e) for e in states
                                 if e["entity_id"].startswith("update.") and e.get("state") == "on"]
                self._total, self._found_ts = len(states), now
                if not everything:
                    states = [e for e in states if e["entity_id"] in set(self._ids)]
            else:
                gate = asyncio.Semaphore(8)

                async def one(eid):
                    async with gate:
                        return await get(f"states/{eid}", missing_ok=True)

                states = [e for e in await asyncio.gather(*(one(i) for i in self._ids)) if e]
                # Pending updates are re-read each run, so an installed one clears now, not at the next full
                # read. Unavailable keeps the old record: a restart of Home Assistant installs nothing.
                fresh = await asyncio.gather(*(one(u["entity_id"]) for u in self._updates))
                self._updates = [old if e.get("state") in NOT_READY else _update(e)
                                 for old, e in zip(self._updates, fresh, strict=True)
                                 if e and e.get("state") != "off"]
        states = [e for e in states if not NOISE.match(e["entity_id"])]
        keep = ("entity_id", "state", "last_changed")
        snap = {"config": {k: config.get(k) for k in ("version", "state", "safe_mode", "recovery_mode", "location_name")},
                "unavailable": [{k: e.get(k) for k in keep} | {"name": e.get("attributes", {}).get("friendly_name", "")}
                                for e in states if e.get("state") == "unavailable"],
                "updates": self._updates,
                "backup": {e["entity_id"].removeprefix("sensor.backup_"): e.get("state") for e in states
                           if e["entity_id"] in BACKUP},
                "entities": self._total,
                "host": {k: next(([e.get("state"), e.get("attributes", {}).get("unit_of_measurement")]
                                  for e in states if e["entity_id"] == eid), None)
                         for k, eid in _sensors(s).items()},
                "watch": [{"entity_id": e["entity_id"], "state": e.get("state"),
                           "dc": e.get("attributes", {}).get("device_class"),
                           "name": e.get("attributes", {}).get("friendly_name") or ""}
                          for e in states if _special(e)],
                "ruled": [{"entity_id": e["entity_id"], "state": e.get("state"),
                           "unit": e.get("attributes", {}).get("unit_of_measurement") or "",
                           "name": e.get("attributes", {}).get("friendly_name") or ""}
                          for e in states if entity_rules.wanted(rules, e["entity_id"])],
                "all": sorted([e["entity_id"], e.get("attributes", {}).get("friendly_name") or ""] for e in states
                              if everything or (e["entity_id"] not in BACKUP
                                                and e["entity_id"] not in set(_sensors(s).values())))}
        snap["_summary"] = (f"Home Assistant {config.get('version', '?')}, "
                            + (f"{len(states)} entities" if everything else f"{len(states)} of {self._total} entities read"))
        return snap

    def default_chosen(self, snap):
        return sorted(e["entity_id"] for e in snap["unavailable"])

    def choices(self, snap):
        return [Choice(e["entity_id"], e.get("name") or e["entity_id"].split(".", 1)[1],
                       e["entity_id"] if e.get("name") else "", group=_domain(e))
                for e in sorted(snap["unavailable"], key=lambda e: e["entity_id"])]

    def filters(self, snap):
        return [Choice(eid, name or eid.split(".", 1)[1], eid if name else "", group=eid.split(".", 1)[0])
                for eid, name in snap.get("all") or []]

    @staticmethod
    def _reported(s, snap):
        """Unavailable entities that count: not ignored and in a watched domain."""
        ignored, skipped = set(s.get("chosen", [])), set(s.get("skipped", []))
        return [e for e in snap["unavailable"] if not NOISE.match(e["entity_id"])
                and e["entity_id"] not in ignored and e["entity_id"] not in skipped
                and f"group:{_domain(e)}" not in skipped]

    def check(self, s, th, snap, now):
        out = []

        def add(cid, status, detail, value=None):
            d = next(c for c in CHECKS if c.id == cid)
            out.append(CheckResult(f"{self.id}.{cid}", d.name, status, detail, value, d.unit))

        c = snap["config"]
        if c.get("safe_mode") or c.get("recovery_mode"):
            add("core", WARNING, f"{c.get('version')} is running in {'recovery' if c.get('recovery_mode') else 'safe'} mode")
        elif c.get("state") not in (None, "RUNNING"):
            add("core", WARNING, f"{c.get('version')} is {str(c.get('state')).lower()}")
        else:
            add("core", OK, f"{c.get('version')} running")

        new = [e for e in self._reported(s, snap) if (now - (_ts(e.get("last_changed")) or now)) >= SETTLE]
        n = len(new)
        names = ", ".join((e.get("name") or e["entity_id"]) for e in new)  # all of them; the page shows the first part
        quiet = len(snap["unavailable"]) - len(self._reported(s, snap))
        add("unavailable", level(n, th["unavailable"]) if n else OK,
            f"{n} unavailable: {names}" if n else f"none (ignoring {quiet})", n)

        self._safety(s, th, snap, add)

        ups = [u for u in snap["updates"] if not u.get("skipped_version") or u["skipped_version"] != u.get("latest_version")]
        add("updates", WARNING if ups else OK,
            "; ".join(f"{u.get('title') or u['entity_id']} {u.get('installed_version')} → {u.get('latest_version')}"
                      for u in ups) if ups else "up to date",
            len(ups))

        ok_ts = _ts(snap["backup"].get("last_successful_automatic_backup"))
        try_ts = _ts(snap["backup"].get("last_attempted_automatic_backup"))
        if ok_ts is None and try_ts is None:
            add("backups", OK, "No automatic backups set up (no backup sensors)")
        elif try_ts and (ok_ts is None or try_ts - ok_ts > 3600):
            add("backups", ERROR, "The last automatic backup attempt failed"
                + (f"; last success {(now - ok_ts) / 3600:.0f} h ago" if ok_ts else ""))
        else:
            age = (now - ok_ts) / 3600
            add("backups", level(age, th["backups"]), f"last automatic backup {age:.0f} h ago", age)

        for cid, what in (("disk", "disk"), ("memory", "memory"), ("cpu", "CPU")):
            st, unit = (snap.get("host") or {}).get(cid) or (None, None)
            try:
                v = float(st)
            except (TypeError, ValueError):
                v = None
            if st is None:
                add(cid, OK, f"no {what} sensor (see the settings)")
            elif v is None or unit != "%":
                add(cid, WARNING, f"{what} sensor reads {st}{' ' + unit if unit else ''}, not a percentage")
            else:
                add(cid, level(v, th[cid]), f"{what} {v:.0f} % used", v)

        rules, problem = entity_rules.parse(s.get("rules", ""))
        if problem:
            add("rules", WARNING, problem)
        else:
            st, detail, n = entity_rules.evaluate(rules, snap.get("ruled", []))
            add("rules", st, detail, n)
        return out

    def _safety(self, s, th, snap, add):
        skipped = set(s.get("skipped", []))
        mine = [e for e in snap.get("watch", [])
                if e["entity_id"] not in skipped and f"group:{e['entity_id'].split('.', 1)[0]}" not in skipped]

        def name(e):
            return e["name"] or e["entity_id"]

        panels = [e for e in mine if e["entity_id"].startswith("alarm_control_panel.")]
        trig = [name(e) for e in panels if e["state"] == "triggered"]
        gone = [name(e) for e in panels if e["state"] in ("unavailable", "unknown", None)]
        if trig:
            add("alarm", ERROR, "TRIGGERED: " + ", ".join(trig))
        elif gone:
            add("alarm", WARNING, "unavailable: " + ", ".join(gone))
        else:
            add("alarm", OK, "; ".join(f"{name(e)} {str(e['state']).replace('_', ' ')}" for e in panels)
                or "no alarm panels")

        for cid, classes, what in (("safety", SAFETY, "safety sensors"), ("tamper", {"tamper"}, "tamper sensors")):
            group = [e for e in mine if e["entity_id"].startswith("binary_sensor.") and e["dc"] in classes]
            on = [f"{name(e)} ({e['dc'].replace('_', ' ')})" for e in group if e["state"] == "on"]
            add(cid, ERROR if on else OK, ", ".join(on) if on else f"{len(group)} {what} clear" if group else f"no {what}")

        addons = [e for e in mine if e["entity_id"].startswith("binary_sensor.") and e["dc"] == "running"
                  and e["entity_id"].endswith("_running")]

        def addon(e):  # "Node-RED Running" -> "Node-RED"; translated names stay as they are
            n = name(e)
            return n[:-8] if n.endswith(" Running") else n

        off = [addon(e) for e in addons if e["state"] == "off"]
        lost = [addon(e) for e in addons if e["state"] not in ("on", "off")]
        if off:
            add("addons", ERROR, "not running: " + ", ".join(off) + (f"; unavailable: {', '.join(lost)}" if lost else ""),
                len(off))
        elif lost:
            add("addons", WARNING, "unavailable: " + ", ".join(lost), 0)
        else:
            add("addons", OK, f"{len(addons)} watched add-ons running" if addons
                else "no add-ons watched (enable their Running sensors)", 0)

        levels = []
        for e in mine:
            if e["entity_id"].startswith("sensor.") and e["dc"] == "battery":
                try:
                    v = float(e["state"])
                except (TypeError, ValueError):
                    continue
                if 0 <= v <= 100:  # e.g. a watch that isn't connected reports -1
                    levels.append((v, name(e)))
        if levels:
            v, who = min(levels)
            add("batteries", level(v, th["batteries"], True), f"lowest {who} {v:.0f} % ({len(levels)} batteries)", v)
        else:
            add("batteries", OK, "no battery sensors")

    def tables(self, snap, s):
        return [Table("Updates available", ["Update", "Installed", "Latest"],
                      [[u.get("title") or u["entity_id"], u.get("installed_version"), u.get("latest_version")]
                       for u in snap["updates"]]),
                Table("Unavailable now, not ignored", ["Entity", "Name", "Since"],
                      [[e["entity_id"], e.get("name"), str(e.get("last_changed", ""))[:16].replace("T", " ")]
                       for e in self._reported(s, snap)])]


plugin = HomeAssistantModule()
