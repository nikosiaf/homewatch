"""Likely causes of an incident, from what the devices did around it (step 3 of incidents).

Fixed rules, no guessing beyond them: each looks at the device events (reboots, line resyncs, UPS on
battery...) from a little before the incident until a while after it, and names the most likely cause
with the events that show it. No rule fits: no cause is given.
"""

from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

BEFORE = 10 * 60  # s: device events this long before the incident's first sign still count
AFTER = 20 * 60  # s: and this long after its end (a line re-syncs a while after the power is back)
INTERNET = ("internet.", "speedport.", "opnsense.gateways")


def _hm(ts: float, zone: ZoneInfo) -> str:
    return datetime.fromtimestamp(ts, zone).strftime("%H:%M")


def window(start: float, end: float | None) -> tuple[float, float]:
    return start - BEFORE, (end or start) + AFTER


def _parent_event(db, site: str, evs, nodes: list[str]):
    """A device the incident's problems sit behind (on the map) that restarted or lost link in the window:
    (event, its name, how many of the problem's devices are behind it), or None."""
    from . import topology

    if not nodes:
        return None
    m = topology.load(db)
    for e in evs:
        if e["kind"] not in topology.EVENT_KINDS:
            continue
        en = topology.event_node(m, e["module"], e["device"])
        if not en:
            continue
        under = [n for n in nodes if en in topology.ancestors(m, n)]
        if under:
            return e, m[en]["name"], len(under)
    return None


def explain(db, site: str, start: float, end: float | None, keys: list[str], zone: ZoneInfo,
            nodes: list[str] | None = None) -> tuple[str, list]:
    """(likely cause or '', evidence [(ts, module, device, detail, exact)]) for an incident."""
    lo, hi = window(start, end)
    evs = db.all("SELECT * FROM events WHERE site=? AND ts BETWEEN ? AND ? ORDER BY ts, id", (site, lo, hi))
    evidence = [(e["ts"], e["module"], e["device"], e["detail"], bool(e["exact"])) for e in evs]

    def first(kind, module=None, device=None):
        return next((e for e in evs if e["kind"] == kind and (module is None or e["module"] == module)
                     and (device is None or e["device"] == device)), None)

    internet = any(k.startswith(INTERNET) for k in keys)
    battery, mains = first("on_battery"), first("on_mains")
    modem = first("reboot", "speedport")
    line = first("line_resync") or first("line_down")
    firewall, host = first("reboot", "opnsense"), first("reboot", "proxmox")
    t = lambda e: ("~" if not e["exact"] else "") + _hm(e["ts"], zone)  # noqa: E731
    if battery:
        span = f"from {t(battery)}" + (f" to {t(mains)}" if mains and mains["ts"] >= battery["ts"] else "")
        if modem:
            cause = (f"Power cut at home: the UPS ran on battery {span}, and the modem restarted at {t(modem)} "
                     "(is the modem on the UPS?)")
        elif internet and line:
            cause = (f"Power cut in the area: the UPS ran on battery {span}; the modem stayed on, but the DSL line "
                     f"dropped and re-synced at {t(line)}, so the street cabinet most likely lost power too")
        else:
            cause = f"Power cut: the UPS ran on battery {span}"
    elif modem:
        cause = f"The modem restarted at {t(modem)} (no power cut at home)"
    elif firewall or host:
        e = min((x for x in (firewall, host) if x), key=lambda x: x["ts"])
        cause = f"{e['detail']} at {t(e)}: a local cause, your own equipment"
    elif internet and line:
        cause = f"The DSL line dropped and re-synced at {t(line)} with no power cut at home: a line or ISP-side problem"
    elif internet and _line_watched(db, site, start, end):
        cause = "The internet stopped answering while the DSL line stayed up: a problem in the ISP's network"
    elif (parent := _parent_event(db, site, evs, nodes)):
        e, name, n = parent
        cause = (f"{name}: {e['detail'].lower()} at {t(e)}, and {n} device{'s' if n != 1 else ''} behind it "
                 "went with it")
    elif (other := first("reboot")):
        cause = f"{other['detail']} at {t(other)}"
    else:
        cause = ""
    return cause, evidence


def _line_watched(db, site: str, start: float, end: float | None) -> bool:
    """The modem was read through the whole incident (and saw no resync): the line really stayed up."""
    row = db.one("SELECT 1 FROM coverage WHERE site=? AND module='speedport' AND start<=? AND end>=?",
                 (site, start, end or start))
    return row is not None


def dumps(evidence: list) -> str:
    return json.dumps(evidence)


def loads(text: str) -> list:
    try:
        return [tuple(x) for x in json.loads(text or "[]")]
    except ValueError:
        return []
