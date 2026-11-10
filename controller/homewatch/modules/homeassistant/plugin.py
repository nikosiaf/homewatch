"""Home Assistant: long-lived token of a dedicated non-admin user; reads /api/config and /api/states.

Home Assistant's own health (core, updates, backups, host disk / memory / CPU) is checked from the start.
Entities are the admin's choice, one by one, and nothing is ticked at first: "Watch" checks one (unavailable,
plus what its kind means: an alarm triggered, a smoke sensor on, a low battery...) and allows a rule on it;
"Show in Grafana" puts it on the Home Assistant dashboard. Either tick stores its values in VictoriaMetrics.
"""

from __future__ import annotations

import asyncio
import json
import re
import ssl
from datetime import datetime
from fnmatch import fnmatchcase

import httpx

from .. import ERROR, OK, WARNING, CheckDef, CheckResult, Thresholds, level
from ...metrics import lines, num
from ..pinning import verify_for
from ..apibase import SELF_SIGNED, why, ApiError, ApiModule, Field, https_url
from . import rules as entity_rules

CHECKS = [
    CheckDef("api", "HA reachable", "api", help="ERROR when homeWatch can't read Home Assistant's API."),
    CheckDef("core", "Core", "api", help="WARNING when Home Assistant is starting, stopping or in safe / recovery mode."),
    CheckDef("unavailable", "Unavailable entities", "api", warn=1, error=10, editable=("warn", "error"),
             help="Watched entities unavailable for 30 minutes or more."),
    CheckDef("silent", "Silent entities", "api", warn=1, editable=("warn",),
             help="Watched entities that stopped reporting: homeWatch learns how often each one reports (its first "
                  "7 days) and names the ones silent for 3 times their usual gap, at least an hour. ERROR when half "
                  "or more of an integration's watched entities go silent together (a hub, bridge or coordinator "
                  "that stopped, while Home Assistant still shows their last values)."),
    CheckDef("alarm", "Alarm panels", "api",
             help="Watched alarm panels: ERROR when one is triggered."),
    CheckDef("safety", "Safety sensors", "api",
             help="Watched smoke, water / moisture, gas, CO, heat or safety sensors: ERROR when one detects something."),
    CheckDef("tamper", "Tamper", "api", help="Watched tamper sensors: ERROR when one reports tampering."),
    CheckDef("batteries", "Batteries", "api", unit="%", warn=25, error=10, editable=("warn", "error"),
             lower_is_worse=True, help="Lowest level among the watched battery sensors."),
    CheckDef("updates", "Updates", "api", unit="days", warn=14, editable=("warn",), todo=True,
             help="Updates Home Assistant can install (skipped versions don't count). Listed under To do; WARNING once it has waited this many days."),
    CheckDef("backups", "Automatic backups", "api", unit="h", warn=26, error=50, editable=("warn", "error"),
             help="Age of the last successful automatic backup; ERROR when the last attempt failed."),
    CheckDef("addons", "Add-ons", "api",
             help="Watched add-on Running sensors: ERROR when the add-on is not running."),
    CheckDef("disk", "Disk", "api", unit="%", warn=80, error=90, editable=("warn", "error"),
             help="Disk use of the Home Assistant host, from the sensor below (System Monitor's by default)."),
    CheckDef("memory", "Memory", "api", unit="%", warn=80, error=90, editable=("warn", "error"),
             help="Memory use of the Home Assistant host, from the sensor below (System Monitor's by default)."),
    CheckDef("cpu", "CPU", "api", unit="%", warn=90, error=98, editable=("warn", "error"),
             help="Processor use of the Home Assistant host, from the sensor below (System Monitor's by default). "
                  "Short peaks are normal, so the limits are high."),
    CheckDef("rules", "Entity rules", "api",
             help="The rules you gave watched entities (Rule column); the worst rule that fires sets the level. "
                  "WARNING when a number rule gets no number."),
]
# checks with nothing to look at until a fitting entity is watched: hidden until then
BY_ENTITY = ("unavailable", "silent", "alarm", "safety", "tamper", "batteries", "addons", "rules")
SETTLE = 30 * 60
FLAP_SHORT = 2 * 3600  # s: unavailable for less than this, then back by itself, is one flap
FLAP_COUNT = 3  # flaps in FLAP_DAYS that make an entity "keeps flapping"
FLAP_DAYS = 7
SILENT_FACTOR = 3  # silent: no report for this many times the entity's usual (95th percentile) gap…
SILENT_MIN = 3600  # …and at least this long
GROUP_FACTOR = 2  # an integration counts as silent sooner: half its entities quiet for twice their usual gap
LEARN_DAYS, LEARN_GAPS, KEEP_GAPS = 7, 20, 50  # judged only after this long and this many gaps seen
FULL_EVERY = 30  # minutes between full state reads, which find new entities and new updates
FULL_MIN, FULL_MAX = 5, 1440
NOT_READY = ("unavailable", "unknown")
MAX_SINGLE = 100  # more entities than this: one bulk read is lighter than this many requests
BACKUP = ("sensor.backup_last_successful_automatic_backup", "sensor.backup_last_attempted_automatic_backup")
SAFETY = {"smoke", "moisture", "gas", "carbon_monoxide", "heat", "safety"}  # binary_sensor device classes
# System Monitor's sensors, used when the disk / memory / CPU sensor fields are left empty
DISK, MEMORY = "sensor.system_monitor_disk_usage", "sensor.system_monitor_memory_usage"
CPU = "sensor.system_monitor_processor_use"
HOST = (("disk", "disk_sensor", "Disk use sensor", DISK), ("memory", "memory_sensor", "Memory use sensor", MEMORY),
        ("cpu", "cpu_sensor", "CPU use sensor", CPU))
# domains with nothing worth watching or graphing: actions, services, or states that say little
HIDDEN = {"automation", "script", "scene", "button", "input_button", "event", "update", "conversation", "stt", "tts",
          "wake_word", "notify", "image", "camera", "tag", "calendar", "todo", "sun"}
NOISE = re.compile(r"^sensor\..*_(rx|tx)$")  # per-client traffic counters, never watched by "watch every entity"
ENTITY_ID = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
LEGACY = ("record", "watch_all", "skipped", "chosen", "rules")  # settings before the Entities table (0.35)
KNOWN_KEY = "ha.known_entities"  # entity ids at the last save of the Entities box: newer ones get a "new" badge

# states that mean on (1) or off (0) when an entity's values are stored
ON = {"on", "open", "home", "true", "heating", "cooling", "heat", "cool", "heat_cool", "auto", "dry", "fan_only", "playing", "armed_home", "armed_away", "armed_night",
      "armed_vacation", "armed_custom_bypass", "triggered", "unlocked", "detected", "wet", "problem"}
OFF = {"off", "closed", "not_home", "false", "idle", "paused", "standby", "disarmed", "locked", "clear", "dry", "ok"}
CLIMATE_ATTRS = {"current_temperature": "current_temperature", "temperature": "target_temperature",
                 "current_humidity": "current_humidity"}

RULE_HELP = (
    "A rule makes homeWatch warn about a watched entity's state. Write: is | is not | below | above, a value, "
    "then warning or error. Several rules for one entity: put ; between them; the worst one that fires counts. "
    "Examples: below 200 warning (an oil tank's litres) · below 200 warning; below 100 error (two levels) · "
    "above 65 warning (a disk's temperature) · is on error (a trouble or problem sensor) · "
    "is not home warning (a person or tracker) · is not online error (a connection status). "
    "\"is\" compares the state as Home Assistant shows it under Developer tools → States, ignoring capitals. "
    "A rule needs Watch: without it the rule is kept but not checked.")


def flaps(db, site: str, unavailable: set[str], now: float) -> set[str]:
    """Count unavailable spells per entity (one row from going unavailable to coming back) and return the
    entities that keep flapping: FLAP_COUNT short spells (back by itself within FLAP_SHORT) in FLAP_DAYS."""
    with db.tx() as t:
        open_ = {r["entity_id"]: r["id"] for r in t.all("SELECT id, entity_id FROM flaps WHERE site=? AND end IS NULL",
                                                       (site,))}
        for eid in unavailable - set(open_):
            t.run("INSERT INTO flaps(site,entity_id,start) VALUES(?,?,?)", (site, eid, now))
        for eid, rid in open_.items():
            if eid not in unavailable:
                t.run("UPDATE flaps SET end=? WHERE id=?", (now, rid))
        t.run("DELETE FROM flaps WHERE site=? AND end IS NOT NULL AND end<?", (site, now - 30 * 86400))
        rows = t.all("SELECT entity_id FROM flaps WHERE site=? AND start>? AND end IS NOT NULL AND end-start<? "
                     "GROUP BY entity_id HAVING COUNT(*)>=?", (site, now - FLAP_DAYS * 86400, FLAP_SHORT, FLAP_COUNT))
    return {r["entity_id"] for r in rows}


def learn(db, site: str, watched: list[dict], now: float) -> dict[str, dict]:
    """Note each watched entity's last report and the gaps between its reports; return entity id ->
    {age, usual (95th percentile gap), learned}. Entities no longer watched are forgotten."""
    out = {}
    with db.tx() as t:
        rows = {r["entity_id"]: r for r in t.all("SELECT * FROM entity_seen WHERE site=?", (site,))}
        for e in watched:
            eid, lr = e["entity_id"], e.get("lr")
            if lr is None:
                continue
            r = rows.pop(eid, None)
            if r is None:
                t.run("INSERT INTO entity_seen(site,entity_id,first,last) VALUES(?,?,?,?)", (site, eid, now, lr))
                first, gaps, last = now, [], lr
            else:
                first, gaps, last = r["first"], json.loads(r["gaps"]), r["last"]
                if lr > last:
                    gaps = (gaps + [round(lr - last, 1)])[-KEEP_GAPS:]
                    last = lr
                    t.run("UPDATE entity_seen SET last=?, gaps=? WHERE site=? AND entity_id=?",
                          (last, json.dumps(gaps), site, eid))
            usual = sorted(gaps)[max(0, int(len(gaps) * 0.95) - 1)] if gaps else None
            out[eid] = {"age": max(0.0, now - last), "usual": usual,
                        "learned": len(gaps) >= LEARN_GAPS and now - first >= LEARN_DAYS * 86400}
        for eid in rows:
            t.run("DELETE FROM entity_seen WHERE site=? AND entity_id=?", (site, eid))
    return out


def _short(seconds: float) -> str:
    if seconds < 5400:
        return f"{seconds / 60:.0f} min"
    if seconds < 172800:
        return f"{seconds / 3600:.1f}".removesuffix(".0") + " h"
    return f"{seconds / 86400:.0f} days"


def silent_check(watched: list[dict], seen: dict[str, dict], t: Thresholds):
    """(status, detail, value) for the Silent entities check."""
    alive = [e for e in watched if e.get("state") not in NOT_READY and e["entity_id"] in seen]
    learned = [e for e in alive if seen[e["entity_id"]]["learned"]]
    if not learned:
        return OK, (f"learning how often the watched entities report ({LEARN_DAYS} days, {LEARN_GAPS} reports each)"
                    if alive else "no watched entity reports a time"), 0

    def quiet(e, factor):
        s = seen[e["entity_id"]]
        return s["age"] > max(factor * (s["usual"] or 0), SILENT_MIN)

    silent = [e for e in learned if quiet(e, SILENT_FACTOR)]
    groups = {}
    for e in learned:
        if e.get("integ"):
            groups.setdefault(e["integ"], []).append(e)
    down = [(g, ents, [e for e in ents if quiet(e, GROUP_FACTOR)]) for g, ents in sorted(groups.items())]
    down = [(g, ents, q) for g, ents, q in down if len(ents) >= 2 and 2 * len(q) >= len(ents)]
    name = lambda e: e.get("name") or e["entity_id"]  # noqa: E731
    if down:
        parts = [f"{g}: {len(q)} of {len(ents)} watched entities silent, longest "
                 f"{_short(max(seen[e['entity_id']]['age'] for e in q))}" for g, ents, q in down]
        return ERROR, "; ".join(parts), len({e["entity_id"] for _, _, q in down for e in q} | {e["entity_id"] for e in silent})
    if silent:
        silent.sort(key=lambda e: -seen[e["entity_id"]]["age"])
        what = ", ".join(f"{name(e)} ({_short(seen[e['entity_id']]['age'])}, usually within "
                         f"{_short(seen[e['entity_id']]['usual'] or 0)})" for e in silent[:5])
        return level(len(silent), t), f"{len(silent)} silent: {what}" + (" …" if len(silent) > 5 else ""), len(silent)
    return OK, f"{len(learned)} of {len(alive)} watched entities learned, none silent", 0


def _domain(eid: str) -> str:
    return eid.split(".", 1)[0]


def entity_values(e: dict) -> dict[str, float]:
    """What an entity's state means as numbers: its value, or 1/0 for on/off-like states, plus a few attributes."""
    st, a, out = str(e.get("state", "")), e.get("attributes") or {}, {}
    v = num(st)
    if v is None and st.lower() in ON | OFF:
        v = float(st.lower() in ON)
    if v is not None and st not in ("nan", "inf", "-inf"):
        out["value"] = v
    dom = _domain(e["entity_id"])
    if dom == "climate":
        out |= {f: num(a.get(k)) for k, f in CLIMATE_ATTRS.items() if num(a.get(k)) is not None}
        if a.get("hvac_action"):
            out["active"] = float(a["hvac_action"] in ("heating", "cooling", "drying"))
    elif dom == "light" and num(a.get("brightness")) is not None:
        out["brightness_percent"] = round(num(a["brightness"]) / 2.55, 1)
    return out


def _update(e) -> dict:
    attrs = e.get("attributes", {})
    return {"entity_id": e["entity_id"],
            **{k: attrs.get(k) for k in ("title", "installed_version", "latest_version", "skipped_version")}}


def _pending(e) -> bool:
    """An update Home Assistant lists under Settings → Updates: on, and one it can install (feature INSTALL).
    Update entities that only report (Proxmox package counts, say) stay off that page, so off this check too."""
    try:
        installable = int(e.get("attributes", {}).get("supported_features") or 0) & 1
    except (TypeError, ValueError):
        installable = 0
    return e.get("state") == "on" and bool(installable)


ADDON_ATTRS = {"device_class", "friendly_name", "icon", "entity_picture"}  # all a Supervisor Running sensor has


def _addon(e) -> bool:
    """An add-on's Running sensor (Supervisor integration). Other integrations use the 'running' class too
    (VM status, appliance switches), mostly with ids not ending in _running; the ones that do (Proxmox VE's
    "Node pve Backup running") carry attributes of their own, which the Supervisor's sensors never have."""
    attrs = e.get("attributes", {})
    return (e["entity_id"].startswith("binary_sensor.") and e["entity_id"].endswith("_running")
            and attrs.get("device_class") == "running" and set(attrs) <= ADDON_ATTRS)


def kind(e: dict) -> str:
    """Which check a watched entity feeds besides "unavailable": alarm, safety, tamper, battery, addon or ''."""
    dom, a = _domain(e["entity_id"]), e.get("attributes") or {}
    dc = a.get("device_class")
    if dom == "alarm_control_panel":
        return "alarm"
    if dom == "binary_sensor" and dc in SAFETY:
        return "safety"
    if dom == "binary_sensor" and dc == "tamper":
        return "tamper"
    if dom == "sensor" and dc == "battery" and a.get("unit_of_measurement") == "%":
        return "battery"
    return "addon" if _addon(e) else ""


KIND_CHECK = {"alarm": "alarm", "safety": "safety", "tamper": "tamper", "battery": "batteries", "addon": "addons"}


def _sensors(s) -> dict:
    """Host sensors to read: the chosen ones, else System Monitor's."""
    return {cid: s.get(key) or default for cid, key, _, default in HOST}


# integrations whose names aren't their domain with capitals
INTEGRATIONS = {"esphome": "ESPHome", "mqtt": "MQTT", "hassio": "Supervisor", "unifi": "UniFi", "wled": "WLED",
                "synology_dsm": "Synology DSM", "opnsense": "OPNsense", "nut": "NUT", "proxmoxve": "Proxmox VE",
                "systemmonitor": "System Monitor", "zha": "ZHA", "zwave_js": "Z-Wave", "homekit": "HomeKit",
                "hue": "Philips Hue", "mobile_app": "Mobile App", "tplink": "TP-Link", "upnp": "UPnP",
                "dlna_dmr": "DLNA", "ipp": "IPP", "lg_webos_tv": "LG webOS TV", "webostv": "LG webOS TV",
                "sun": "Sun", "met": "Met.no", "openweathermap": "OpenWeatherMap", "ookla_speedtest": "Speedtest"}
# device classes that usually matter: listed first within a device
FIRST = {"battery", "problem", "connectivity", "safety", "smoke", "moisture", "gas", "carbon_monoxide", "heat",
         "tamper", "running", "update"}


def integration_name(domain: str) -> str:
    return INTEGRATIONS.get(domain) or domain.replace("_", " ").capitalize() if domain else ""


def catalog_row(e: dict, reg: dict | None = None) -> dict:
    """One entity as the Entities box shows it (the whole list comes with every full read), with its integration,
    device and area from Home Assistant's registries when they could be read."""
    a = e.get("attributes") or {}
    r = (reg or {}).get(e["entity_id"]) or {}
    dc = a.get("device_class") or ""
    return {"id": e["entity_id"], "name": a.get("friendly_name") or "", "state": str(e.get("state", "")),
            "unit": a.get("unit_of_measurement") or "", "kind": kind(e),
            "integration": r.get("integration", ""), "device": r.get("device", ""), "area": r.get("area", ""),
            "mac": r.get("mac", ""),
            "first": bool(kind(e)) or dc in FIRST}


async def registries(url: str, token: str, verify) -> dict[str, dict]:
    """entity id -> {integration, device, area}, from Home Assistant's websocket API (a non-admin user may read
    the entity, device and area registries). {} when it can't be read: the box then groups by domain."""
    from websockets.asyncio.client import connect

    ws_url = ("wss" + url[5:] if url.startswith("https") else "ws" + url[4:]) + "/api/websocket"
    ctx = None
    if ws_url.startswith("wss"):
        ctx = verify if isinstance(verify, ssl.SSLContext) else ssl.create_default_context()
    async with connect(ws_url, ssl=ctx, max_size=64 * 1024 * 1024, open_timeout=10, close_timeout=2) as ws:
        await ws.recv()  # auth_required
        await ws.send(json.dumps({"type": "auth", "access_token": token}))
        if json.loads(await ws.recv()).get("type") != "auth_ok":
            return {}
        out = {}
        for n, cmd in enumerate(("config/entity_registry/list_for_display", "config/device_registry/list",
                                 "config/area_registry/list"), 1):
            await ws.send(json.dumps({"id": n, "type": cmd}))
            while True:
                r = json.loads(await asyncio.wait_for(ws.recv(), 20))
                if r.get("id") == n:
                    break
            if not r.get("success"):
                return {}
            out[cmd] = r["result"]
    areas = {x["area_id"]: x.get("name") or x["area_id"] for x in out["config/area_registry/list"]}
    devices = {d["id"]: (d.get("name_by_user") or d.get("name") or "", d.get("area_id"),
                         next((c[1] for c in d.get("connections") or [] if len(c) == 2 and c[0] == "mac"), ""))
               for d in out["config/device_registry/list"]}
    reg = {}
    for x in out["config/entity_registry/list_for_display"].get("entities", []):
        dev, dev_area, dev_mac = devices.get(x.get("di"), ("", None, ""))
        reg[x["ei"]] = {"integration": integration_name(x.get("pl") or ""), "device": dev,
                        "area": areas.get(x.get("ai") or dev_area, ""), "mac": dev_mac}
    return reg


def rule_lines(rules: dict, watched) -> str:
    """The rules of watched entities as entity_rules lines ("<entity> below 200 warning")."""
    return "\n".join(f"{eid} {part.strip()}" for eid in sorted(rules) if eid in watched
                     for part in str(rules[eid]).split(";") if part.strip())


def check_rule(eid: str, text: str) -> str:
    """'' or what is wrong with one entity's rule text."""
    for part in (x.strip() for x in str(text).split(";")):
        if part and entity_rules.parse(f"{eid} {part}")[1]:
            return (f"Rule for {eid}: \"{part}\" — write is | is not | below | above, a value, then warning or "
                    f"error, e.g. below 200 warning")
    return ""


def convert(s: dict, states: list[dict]) -> dict:
    """0.35 settings (patterns, "watch every entity", essential entities watched and recorded by themselves, an
    ignore list) as ticks: what was watched stays watched, what was recorded is shown in Grafana, every rule moves
    to each entity it matches now. Entities in hidden domains are left out."""
    def skipped(eid):
        sk = s.get("skipped") or []
        return eid in sk or f"group:{_domain(eid)}" in sk

    old_rules, _ = entity_rules.parse(s.get("rules", "") if isinstance(s.get("rules"), str) else "")
    patterns = [x.strip() for x in str(s.get("record") or "").splitlines() if x.strip() and not x.strip().startswith("#")]
    extra, ignored = set(s.get("watch") or []), set(s.get("chosen") or [])
    watch, grafana, rules = set(), set(), {}
    for e in states:
        eid = e["entity_id"]
        if _domain(eid) in HIDDEN:
            continue
        mine = [r for r in old_rules if r.matches(eid)]
        if mine:
            rules[eid] = "; ".join(f"{r.text()} {'error' if r.level == ERROR else 'warning'}" for r in mine)
        noise = s.get("watch_all") and NOISE.match(eid) and not kind(e) and not mine  # never reported by 0.35
        if eid in extra or ((s.get("watch_all") and not noise or kind(e) or mine) and not skipped(eid)):
            if eid not in ignored or mine:
                watch.add(eid)
        if kind(e) or any(fnmatchcase(eid, p) for p in patterns):
            grafana.add(eid)
    return {"watch": sorted(watch), "grafana": sorted(grafana), "entity_rules": rules, "entities_v": 2}


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
    description = ("Watches Home Assistant through the token of a non-admin user: core state, available updates, "
                   "automatic backups and the host's disk, memory and CPU. Under Entities you choose entities to "
                   "watch (alarm panels, safety sensors, batteries, add-ons, anything that must not go unavailable, "
                   "with your own rules) and to show in Grafana. Only those are read at each check; the full list "
                   "is read every 30 minutes (you can change this) to find new entities and updates. "
                   "Nothing in Home Assistant is changed.")
    setup_title = "First create an account and token for homewatch"
    setup = [
        ("text", "In Home Assistant: Settings → People → Users → Add user. Name it homewatch and leave "
                 "\"Administrator\" off."),
        ("text", "Log in as homewatch (a private browser window is easiest), open its profile → Security → "
                 "Long-lived access tokens → Create token, and paste the token below."),
        ("text", "Optional, add-ons: Settings → Devices & services → Home Assistant Supervisor → open an add-on's "
                 "device and enable its \"Running\" sensor (disabled by default), then tick Watch for it here."),
        ("text", "Optional, disk, memory and CPU: Home Assistant doesn't tell a non-admin user how its host is doing, "
                 "so homeWatch reads sensors for it. Add the System Monitor integration (Settings → Devices & "
                 "services → Add integration → System Monitor) and enable its disk use, memory use and processor use "
                 "sensors. homeWatch uses those by itself; under Checks and limits you can pick other sensors in %."),
    ]
    fields = [
        Field("url", "Home Assistant address", "http://homeassistant.local:8123", width="wide"),
        Field("verify", "Check the HTTPS certificate", kind="check", default=True,
              help=SELF_SIGNED),
        Field("flap_quiet", "Leave out entities that keep flapping", kind="check", default=True,
              help=f"An entity that went unavailable {FLAP_COUNT} times in {FLAP_DAYS} days and came back by itself "
                   "within 2 hours each time (phone and cloud sensors often do) is not counted by Unavailable "
                   "entities; it is still named there. Untick to count every one."),
        Field("full_every", "Full read every (minutes)", "30", kind="number", default=FULL_EVERY,
              width="narrow", own_line=True,
              help=f"How often the whole entity list is read to find new entities and updates ({FULL_MIN} to {FULL_MAX})."),
        # chosen in the Disk / Memory / CPU rows of "Checks and limits" (limit_fields), not in a box of their own
        Field("disk_sensor", "Disk use sensor", DISK, section="limits"),
        Field("memory_sensor", "Memory use sensor", MEMORY, section="limits"),
        Field("cpu_sensor", "CPU use sensor", CPU, section="limits"),
    ]
    secret_label = "Long-lived access token"
    secret_width = "wide"
    secret_placeholder = "eyJ0eXAiOiJKV1Qi…"
    more_title = "Additional configuration"
    more_help = ("Entities to watch and to show in Grafana. Nothing is ticked at first; values are stored only for "
                 "ticked entities.")
    entity_table = True  # the second box is the Entities box
    last_reading_box = False  # what a reading finds shows in the checks and the Entities box
    read_now_in_settings = True
    read_now_beside = "full_every"  # the Read now button on that field's line
    limits_first = True  # connection, then checks and limits, then the entities

    def __init__(self):
        super().__init__()
        self._ids: list[str] | None = None  # entities read one by one between full reads
        self._flap_ts = None  # the reading the flaps were last counted for
        self._flappy: set[str] = set()
        self._seen: dict[str, dict] = {}  # entity id -> how long since it reported, its usual gap
        self._updates: list[dict] = []
        self._total = 0
        self._found_ts = 0.0
        self._catalog: list[dict] = []  # every entity (hidden domains left out) at the last full read
        self._converted: dict | None = None  # 0.35 settings turned into ticks, saved by readings()
        self._registry_error = ""  # why integrations / devices / areas couldn't be read ('' = they were)

    def reread(self):
        super().reread()
        self._ids = None  # new ticks or sensors: a full read finds them

    # ---- settings --------------------------------------------------------------------------
    def load(self, rt) -> dict:
        s = {"watch": [], "grafana": [], "entity_rules": {}} | super().load(rt)
        if s.get("entities_v") or not s.get("_configured"):
            for k in LEGACY:
                if k != "rules" or not isinstance(s.get("rules"), dict):
                    s.pop(k, None)
        return s

    def imported(self, settings: dict) -> tuple[dict, str]:
        for k in ("watch", "grafana"):
            v = settings.get(k, [])
            if not isinstance(v, list) or not all(isinstance(x, str) and ENTITY_ID.match(x) for x in v):
                return settings, f"{k}: must be a list of entity ids"
        rules = settings.get("entity_rules", {})
        if not isinstance(rules, dict):
            return settings, "entity_rules: must be entity id: rule text"
        for eid, text in rules.items():
            err = "entity_rules: must be entity id: rule text" if not (ENTITY_ID.match(str(eid)) and isinstance(text, str)) \
                else check_rule(eid, text)
            if err:
                return settings, err
        return super().imported(settings)

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
        if "_new" in form:
            out |= {"watch": [], "grafana": [], "entity_rules": {}, "entities_v": 2}
        if form.get("_part") == "more" and "_entities" in form:
            err = self._entities(form, old, out)
            if err:
                return out, err
        return out | {"url": url, "full_every": round(out["full_every"])}, ""

    def _entities(self, form, old, out) -> str:
        """The Entities box into the settings; entities not on the page this time keep theirs."""
        shown = set(json.loads(form.get("_entities") or "[]"))
        watch = {w for w in old.get("watch", []) if w not in shown} | {k[2:] for k in form if k.startswith("w.") and k[2:] in shown}
        grafana = set(old.get("grafana", []))
        if "_gcol" in form:  # the Show in Grafana column was on the page (Grafana is running)
            grafana = {g for g in grafana if g not in shown} | {k[2:] for k in form if k.startswith("g.") and k[2:] in shown}
        rules = {eid: r for eid, r in (old.get("entity_rules") or {}).items() if eid not in shown}
        for k, v in form.items():
            if k.startswith("r.") and k[2:] in shown and str(v).strip():
                eid, text = k[2:], "; ".join(x.strip() for x in str(v).split(";") if x.strip())
                err = check_rule(eid, text)
                if err:
                    return err
                rules[eid] = text
        if sum(len(str(r).split(";")) for r in rules.values()) > entity_rules.MAX_RULES:
            return f"Entity rules: at most {entity_rules.MAX_RULES}"
        out |= {"watch": sorted(watch), "grafana": sorted(grafana), "entity_rules": rules, "entities_v": 2}
        return ""

    async def prepare_save(self, rt, form: dict) -> str:
        old = self.load(rt)
        err = await super().prepare_save(rt, form)
        if err or form.get("_part") != "more":
            return err
        new = json.loads(form.get("_validated", "{}"))
        before = set(old.get("watch", [])) | set(old.get("grafana", []))
        dropped = sorted(before - set(new.get("watch", [])) - set(new.get("grafana", [])))
        if dropped and form.get("_history") == "delete":  # asked on the page; without an answer: kept
            from ...metrics import MetricsError
            ids = "|".join(d.replace(".", "\\\\.") for d in dropped)
            try:
                await rt.metrics.delete(f'{{__name__=~"hw_ha_entity_.*",entity_id=~"{ids}"}}')
            except MetricsError as e:
                return f"Nothing saved: deleting their history failed ({e})"
            form["_deleted"] = json.dumps(dropped)
        return ""

    def save(self, rt, form: dict, user) -> str:
        err = super().save(rt, form, user)
        if err or form.get("_part") != "more":
            return err
        if form.get("_deleted"):
            from ...auth import audit
            audit(rt.db, user.username, self.id, "history deleted: " + ", ".join(json.loads(form["_deleted"])))
        if self._catalog:
            rt.db.set(KNOWN_KEY, sorted(c["id"] for c in self._catalog))  # the "new" badges start over
        return ""

    def audit_detail(self, old: dict, new: dict) -> str:
        """Entity lists as counts and what changed ("watch: 1121 (+2: sensor.a, sensor.b)"), not 1000 ids."""
        def diff(key, show):
            a, b = set(old.get(key) or []), set(new.get(key) or [])
            plus, minus = sorted(b - a), sorted(a - b)
            parts = [f"+{len(plus)}" + (f": {', '.join(plus[:show])}{' …' if len(plus) > show else ''}" if plus else "")] if plus else []
            parts += [f"−{len(minus)}" + (f": {', '.join(minus[:show])}{' …' if len(minus) > show else ''}" if minus else "")] if minus else []
            return f"{len(b)}" + (f" ({'; '.join(parts)})" if parts else "")

        plain = {k: v for k, v in new.items() if k not in ("watch", "grafana", "entity_rules", "chosen", "skipped", "_configured")}
        rules_old, rules_new = old.get("entity_rules") or {}, new.get("entity_rules") or {}
        changed = [f"{e}: {rules_new.get(e) or 'removed'}" for e in sorted(set(rules_old) | set(rules_new))
                   if rules_old.get(e) != rules_new.get(e)]
        return (json.dumps(plain) + f"; watch: {diff('watch', 10)}; show in Grafana: {diff('grafana', 10)}; "
                f"rules: {len(rules_new)}" + (f" ({'; '.join(changed[:10])}{' …' if len(changed) > 10 else ''})" if changed else ""))

    def hidden_checks(self, settings: dict) -> set[str]:
        """Checks with no watched entity to look at: not listed, not run."""
        watched = set(settings.get("watch") or [])
        kinds = {c["kind"] for c in self._catalog if c["id"] in watched}
        if not self._catalog and self.snap:
            kinds = {e.get("kind", "") for e in self.snap.get("watched", [])}
        hidden = {KIND_CHECK[k] for k in KIND_CHECK if k not in kinds}
        if not watched:
            hidden |= {"unavailable", "silent"}
        if not rule_lines(settings.get("entity_rules") or {}, watched):
            hidden.add("rules")
        return hidden

    # ---- reading ---------------------------------------------------------------------------
    async def readings(self, rt, settings, now):
        out = await super().readings(rt, settings, now)
        if self._converted is not None:  # 0.35 settings found: store them as ticks, once
            from ...auth import audit  # also stores the change as a configuration version, so it can be undone
            saved = rt.db.get(self.key) or {}
            new = {k: v for k, v in saved.items() if k not in LEGACY} | self._converted
            rt.db.set(self.key, new)
            audit(rt.db, "system", self.id, f"entities converted to ticks: {len(new['watch'])} watched, "
                                            f"{len(new['grafana'])} shown in Grafana, {len(new['entity_rules'])} with rules")
            self._converted = None
        if self.snap and self.snap_ts != self._flap_ts:
            self._flap_ts = self.snap_ts
            self._flappy = flaps(rt.db, rt.cfg.site, {e["entity_id"] for e in self.snap.get("unavailable", [])}, now)
            self._seen = learn(rt.db, rt.cfg.site, self.snap.get("watched", []), now)
        return out

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
            convert_now = not s.get("entities_v")
            watch, grafana = set(s.get("watch") or []), set(s.get("grafana") or [])
            rules = s.get("entity_rules") if isinstance(s.get("entity_rules"), dict) else {}
            host = set(_sensors(s).values())
            wanted = sorted(watch | grafana | host | set(BACKUP))
            full = (convert_now or self._ids is None or self.snap_ts == float("-inf") or len(wanted) > MAX_SINGLE
                    or now - self._found_ts >= s.get("full_every", FULL_EVERY) * 60)
            if full:
                states = await get("states")
                if convert_now:
                    self._converted = convert(s, states)
                    watch, grafana = set(self._converted["watch"]), set(self._converted["grafana"])
                    rules = self._converted["entity_rules"]
                    wanted = sorted(watch | grafana | host | set(BACKUP))
                try:
                    reg = await asyncio.wait_for(registries(s["url"], secret, verify_for(s, s["url"])), 40)
                    self._registry_error = ""
                except Exception as e:  # noqa: BLE001 - an old Home Assistant, a proxy without websockets...
                    reg, self._registry_error = {}, type(e).__name__
                self._catalog = [catalog_row(e, reg) for e in states if _domain(e["entity_id"]) not in HIDDEN]
                self._updates = [_update(e) for e in states if e["entity_id"].startswith("update.") and _pending(e)]
                self._total, self._found_ts, self._ids = len(states), now, wanted
                keep = set(wanted)
                states = [e for e in states if e["entity_id"] in keep]
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
                                 if e and (e.get("state") in NOT_READY or _pending(e))]

        def about(e):
            a = e.get("attributes") or {}
            return {"entity_id": e["entity_id"], "state": e.get("state"), "name": a.get("friendly_name") or "",
                    "unit": a.get("unit_of_measurement") or "", "dc": a.get("device_class") or ""}

        integ = {c["id"]: c.get("integration", "") for c in self._catalog}
        macs = {c["id"]: c.get("mac", "") for c in self._catalog}
        watched = [about(e) | {"kind": kind(e), "integ": integ.get(e["entity_id"], ""), "mac": macs.get(e["entity_id"], ""),
                               "lr": _ts(e.get("last_reported") or e.get("last_updated"))}
                   for e in states if e["entity_id"] in watch]
        snap = {"config": {k: config.get(k) for k in ("version", "state", "safe_mode", "recovery_mode", "location_name")},
                "unavailable": [{"entity_id": e["entity_id"], "name": e["name"],
                                 "last_changed": next((x.get("last_changed") for x in states if x["entity_id"] == e["entity_id"]), None)}
                                for e in watched if e["state"] == "unavailable"],
                "updates": self._updates,
                "backup": {e["entity_id"].removeprefix("sensor.backup_"): e.get("state") for e in states
                           if e["entity_id"] in BACKUP},
                "entities": self._total,
                "host": {k: next(([e.get("state"), (e.get("attributes") or {}).get("unit_of_measurement")]
                                  for e in states if e["entity_id"] == eid), None)
                         for k, eid in _sensors(s).items()},
                "watched": watched,
                "rules": rule_lines(rules, watch),
                # values kept in VictoriaMetrics: every ticked entity; "recorded" = the ones on the dashboards
                "stored": [{"entity_id": e["entity_id"], "values": entity_values(e)} for e in states
                           if e["entity_id"] in watch | grafana],
                "recorded": [about(e) | {"values": entity_values(e)} for e in states if e["entity_id"] in grafana],
                "catalog": self._catalog, "catalog_ts": self._found_ts,
                # devices with a MAC, for the map (topology.py)
                "devices": list({c["mac"]: {"mac": c["mac"], "name": c.get("device", ""), "integration": c.get("integration", "")}
                                 for c in self._catalog if c.get("mac")}.values())
                + [{"mac": "", "name": "", "integration": i} for i in sorted({e["integ"] for e in watched if e["integ"]})]}
        snap["_summary"] = (f"Home Assistant {config.get('version', '?')}, {len(watch)} entities watched"
                            + (f", {len(grafana)} shown in Grafana" if grafana else ""))
        return snap

    def series(self, site: str) -> list[str]:
        # Only labels that never change: a renamed entity or a new unit keeps its history. Grafana's dashboards
        # look the names and units up in the module's last reading (Runtime.ha_meta).
        if not self.snap:
            return []
        return lines("hw_ha_entity", [({"site": site, "entity_id": e["entity_id"], "domain": _domain(e["entity_id"])},
                                       e["values"]) for e in self.snap.get("stored", []) if e["values"]], self.snap_ts)

    # ---- checks ----------------------------------------------------------------------------
    def check(self, s, th, snap, now):
        out = []

        def add(cid, status, detail, value=None, nodes=None):
            d = next(c for c in CHECKS if c.id == cid)
            out.append(CheckResult(f"{self.id}.{cid}", d.name, status, detail, value, d.unit,
                                   extra={"nodes": nodes} if nodes else {}))

        def nodes_of(ents):
            """The map's nodes these entities hang on: their device (by MAC) and their integration."""
            by_id = {e["entity_id"]: e for e in watched}
            out_ = []
            for x in ents:
                e = by_id.get(x["entity_id"], x)
                for n in ([f"mac:{e['mac'].upper()}"] if e.get("mac") else []) + ([f"integ:{e['integ']}"] if e.get("integ") else []):
                    if n not in out_:
                        out_.append(n)
            return out_

        c = snap["config"]
        if c.get("safe_mode") or c.get("recovery_mode"):
            add("core", WARNING, f"{c.get('version')} is running in {'recovery' if c.get('recovery_mode') else 'safe'} mode")
        elif c.get("state") not in (None, "RUNNING"):
            add("core", WARNING, f"{c.get('version')} is {str(c.get('state')).lower()}")
        else:
            add("core", OK, f"{c.get('version')} running")

        watched = snap.get("watched", [])
        if watched:
            gone = [e for e in snap["unavailable"] if (now - (_ts(e.get("last_changed")) or now)) >= SETTLE]
            quiet = [e for e in gone if e["entity_id"] in self._flappy] if s.get("flap_quiet", True) else []
            gone = [e for e in gone if e not in quiet]
            n = len(gone)
            note = ("; not counted, they keep flapping: " + ", ".join(e.get("name") or e["entity_id"] for e in quiet)
                    if quiet else "")
            add("unavailable", level(n, th["unavailable"]) if n else OK,
                (f"{n} unavailable: " + ", ".join(e.get("name") or e["entity_id"] for e in gone) if n
                 else f"{len(watched)} watched, none unavailable") + note, n, nodes_of(gone))
            if th["silent"].enabled:
                st, det, val = silent_check(watched, self._seen, th["silent"])
                quiet = [e for e in watched if e["entity_id"] in self._seen and st
                         and self._seen[e["entity_id"]]["learned"]
                         and self._seen[e["entity_id"]]["age"] > max(GROUP_FACTOR * (self._seen[e["entity_id"]]["usual"] or 0), SILENT_MIN)]
                add("silent", st, det, val, nodes_of(quiet))
        self._kinds(th, watched, add)

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

        if snap.get("rules"):
            rules, problem = entity_rules.parse(snap["rules"])
            if problem:
                add("rules", WARNING, problem)
            else:
                st, detail, n = entity_rules.evaluate(rules, watched)
                add("rules", st, detail, n)
        return out

    @staticmethod
    def _kinds(th, watched, add):
        """The checks of watched alarm panels, safety and tamper sensors, batteries and add-ons; a check with
        nothing watched gives no result at all (it isn't listed)."""
        def name(e):
            return e["name"] or e["entity_id"]

        panels = [e for e in watched if e["kind"] == "alarm"]
        if panels:
            trig = [name(e) for e in panels if e["state"] == "triggered"]
            if trig:
                add("alarm", ERROR, "TRIGGERED: " + ", ".join(trig))
            else:
                add("alarm", OK, "; ".join(f"{name(e)} {str(e['state']).replace('_', ' ')}" for e in panels))

        for cid, what in (("safety", "safety sensors"), ("tamper", "tamper sensors")):
            group = [e for e in watched if e["kind"] == cid]
            if group:
                on = [f"{name(e)} ({e['dc'].replace('_', ' ')})" for e in group if e["state"] == "on"]
                add(cid, ERROR if on else OK, ", ".join(on) if on else f"{len(group)} {what} clear")

        addons = [e for e in watched if e["kind"] == "addon"]
        if addons:
            def addon(e):  # "Node-RED Running" -> "Node-RED"; translated names stay as they are
                n = name(e)
                return n[:-8] if n.endswith(" Running") else n

            off = [addon(e) for e in addons if e["state"] == "off"]
            add("addons", ERROR if off else OK,
                "not running: " + ", ".join(off) if off else f"{len(addons)} watched add-ons running", len(off))

        bats = [e for e in watched if e["kind"] == "battery"]
        if bats:
            levels = []
            for e in bats:
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
                add("batteries", OK, "no battery reading now")

    # ---- the page --------------------------------------------------------------------------
    def limit_fields(self) -> dict[str, str]:
        return {cid: key for cid, key, _, _ in HOST}

    def limit_sensors(self, settings: dict) -> dict[str, dict]:
        """For the Disk / Memory / CPU rows: the chosen sensor and every sensor in % to choose from."""
        pct = sorted((c for c in self._catalog if c["id"].startswith("sensor.") and c["unit"] == "%"),
                     key=lambda c: (not c["id"].startswith("sensor.system_monitor_"), (c["name"] or c["id"]).lower()))
        ids = {c["id"] for c in pct}
        out = {}
        for cid, key, _, default in HOST:
            now = settings.get(key) or ""
            opts = [("", f"System Monitor's ({default}{'' if default in ids or not ids else ', not found'})")]
            opts += [(c["id"], f"{c['name'] or c['id']} · {c['state']} % · {c['id']}") for c in pct if c["id"] != default]
            if now and now not in ids:
                opts.append((now, f"{now} (not found now)"))
            out[cid] = {"value": now, "options": opts}
        return out

    def page_ctx(self, rt) -> dict:
        ctx = super().page_ctx(rt)
        s = self.load(rt)
        watch, grafana = set(s.get("watch") or []), set(s.get("grafana") or [])
        rules = s.get("entity_rules") or {}
        known = rt.db.get(KNOWN_KEY)
        if known is None and self._catalog:  # the first look: nothing is new yet
            known = [c["id"] for c in self._catalog]
            rt.db.set(KNOWN_KEY, known)
        known = set(known or [])
        rows = [c | {"domain": _domain(c["id"]), "w": c["id"] in watch, "g": c["id"] in grafana,
                     "rule": rules.get(c["id"], ""), "new": c["id"] not in known}
                for c in sorted(self._catalog, key=lambda c: c["id"])]
        groups = {}
        for r in rows:
            groups.setdefault(r["domain"], []).append(r)
        ctx |= {"entity_groups": sorted(groups.items()), "entities_ts": self._found_ts,
                "entity_views": any(r.get("integration") for r in rows), "registry_error": self._registry_error,
                "entities_shown": json.dumps([r["id"] for r in rows]),
                "entity_counts": {"w": sum(r["w"] for r in rows), "g": sum(r["g"] for r in rows),
                                  "new": sum(r["new"] for r in rows), "all": len(rows)},
                "grafana_col": bool(getattr(rt, "grafana_on", False)), "rule_help": RULE_HELP,
                "hidden_checks": self.hidden_checks(s), "limit_sensors": self.limit_sensors(s)}
        return ctx


plugin = HomeAssistantModule()
