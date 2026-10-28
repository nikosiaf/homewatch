"""Proxmox checks: pure functions from a snapshot to results.

Diagnosis order: API unreachable -> everything suppressed; node offline -> that node's resource
checks suppressed (the node line names the cause).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .. import ERROR, OK, WARNING, CheckDef, CheckResult, Thresholds, level
from .client import Snapshot

MODULE = "proxmox"

CHECKS: list[CheckDef] = [
    CheckDef("api", "API reachable", "api", help="ERROR when homewatch can't read the Proxmox API (down, or token rejected)."),
    CheckDef("node", "Node online", "api", help="ERROR when a cluster node is offline."),
    CheckDef("cpu", "Node CPU", "api", unit="%", warn=85, error=95, editable=("warn", "error"),
             help="Busiest node's CPU use at the check run."),
    CheckDef("mem", "Node memory", "api", unit="%", warn=90, error=95, editable=("warn", "error"),
             help="Highest node memory use."),
    CheckDef("rootfs", "Node root disk", "api", unit="%", warn=85, error=95, editable=("warn", "error"),
             help="Fullest node root filesystem."),
    CheckDef("guests", "Guests running", "api",
             help="ERROR when a guest you chose to watch is not running."),
    CheckDef("storage", "Storage", "api", unit="%", warn=85, error=95, editable=("warn", "error"),
             help="Fullest storage; ERROR also when an enabled storage is unavailable."),
    CheckDef("disks", "Disk health", "api", unit="%", warn=20, error=10, editable=("warn", "error"),
             lower_is_worse=True,
             help="ERROR when SMART health is not PASSED; levels apply to the lowest remaining SSD life."),
    CheckDef("backups", "Backups", "api", unit="h", warn=26, error=50, editable=("warn", "error"),
             help="Age of the newest successful backup of each watched guest that a backup job covers; "
                  "ERROR also when its last backup attempt failed."),
]
CHECK_BY_ID = {c.id: c for c in CHECKS}
HEALTHY = {"PASSED", "OK"}


@dataclass
class State:
    now: float
    snap: Snapshot | None = None
    error: str = ""
    watched: list[int] = field(default_factory=list)


def _pct(used, total) -> float | None:
    try:
        return 100 * float(used) / float(total) if float(total) > 0 else None
    except (TypeError, ValueError):
        return None


def _ago(h: float) -> str:
    return f"{h:.0f} h" if h < 48 else f"{h / 24:.1f} days"


def evaluate(settings: dict, th: dict[str, Thresholds], st: State) -> list[CheckResult]:
    out: dict[str, CheckResult] = {}

    def add(cid, status, detail, value=None, suppressed_by=None):
        d = CHECK_BY_ID[cid]
        out[cid] = CheckResult(f"{MODULE}.{cid}", d.name, status, detail, value, d.unit,
                               f"{MODULE}.{suppressed_by}" if suppressed_by else None)

    def done():
        return [out[c.id] for c in CHECKS if c.id in out and th[c.id].enabled]

    if st.snap is None:
        if st.error:
            add("api", ERROR, f"Can't read Proxmox: {st.error}")
            for c in CHECKS[1:]:
                add(c.id, OK, "no reading", suppressed_by="api")
        else:
            add("api", OK, "No reading yet")
        return done()
    s = st.snap
    add("api", OK, f"{len(s.nodes)} node{'s' if len(s.nodes) != 1 else ''}, {len(s.guests)} guests")

    online = [n for n in s.nodes if n.get("status") == "online"]
    offline = [n["node"] for n in s.nodes if n.get("status") != "online"]
    if offline:
        add("node", ERROR, f"Offline: {', '.join(offline)}")
    else:
        add("node", OK, ", ".join(n["node"] for n in online) + " online")
    if not online:
        for c in ("cpu", "mem", "rootfs", "guests", "storage", "disks", "backups"):
            add(c, OK, "node offline", suppressed_by="node")
        return done()

    def worst(key, used, total, what):
        vals = [(_pct(n.get(used), n.get(total)) if total else float(n.get(used, 0)) * 100, n["node"]) for n in online]
        vals = [v for v in vals if v[0] is not None]
        if not vals:
            add(key, OK, "not reported")
            return
        v, name = max(vals)
        add(key, level(v, th[key]), f"{what} {v:.0f} % on {name}", v)

    worst("cpu", "cpu", None, "CPU")
    worst("mem", "mem", "maxmem", "memory")
    worst("rootfs", "disk", "maxdisk", "root disk")

    watched = set(st.watched)
    by_id = {int(g["vmid"]): g for g in s.guests}
    if watched:
        down = [f"{by_id[v].get('name', v)} ({v}) {by_id[v].get('status', '?')}" for v in sorted(watched)
                if v in by_id and by_id[v].get("status") != "running"]
        gone = [str(v) for v in sorted(watched) if v not in by_id]
        if down or gone:
            add("guests", ERROR, "Not running: " + ", ".join(down + [f"{g} (no longer exists)" for g in gone]))
        else:
            add("guests", OK, f"All {len(watched)} watched guests running")
    else:
        add("guests", OK, "No guests chosen to watch")

    unavailable = [x["storage"] for x in s.storages if x.get("status") not in ("available", None)
                   and x.get("node") in {n["node"] for n in online}]
    fills = [(_pct(x.get("disk"), x.get("maxdisk")), x["storage"]) for x in s.storages
             if x.get("status") == "available"]
    fills = [f for f in fills if f[0] is not None]
    if unavailable:
        add("storage", ERROR, f"Unavailable: {', '.join(sorted(set(unavailable)))}")
    elif fills:
        v, name = max(fills)
        add("storage", level(v, th["storage"]), f"fullest: {name} {v:.0f} %", v)
    else:
        add("storage", OK, "No storage reported")

    disks = [d | {"node": n} for n, ds in s.disks.items() for d in ds]
    bad = [f"{d.get('devpath')} {d.get('health')}" for d in disks
           if str(d.get("health", "")).upper() not in HEALTHY | {"UNKNOWN", ""}]
    lives = [(float(d["wearout"]), d.get("devpath", "?")) for d in disks
             if str(d.get("wearout", "")).replace(".", "", 1).isdigit()]
    if bad:
        add("disks", ERROR, f"SMART not passed: {', '.join(bad)}")
    elif lives:
        v, dev = min(lives)
        add("disks", level(v, th["disks"], True), f"{len(disks)} disks PASSED; lowest SSD life left {v:.0f} % ({dev})", v)
    elif disks:
        add("disks", OK, f"{len(disks)} disks PASSED")
    else:
        add("disks", OK, "No disk data")

    covered = [v for v in sorted(watched) if v in by_id and v not in s.not_covered]
    if not covered:
        add("backups", OK, "No watched guest is in a backup job" if watched else "No guests chosen to watch")
    else:
        problems, oldest = [], 0.0
        lvl = OK
        for v in covered:
            b = s.backups.get(v, {})
            name = by_id[v].get("name", v)
            if b.get("failed") and b["failed"] > (b.get("ok") or 0):
                problems.append(f"{name}: last backup failed ({b.get('error', '')})")
                lvl = ERROR
                continue
            if not b.get("ok"):  # a guest added since the last backup run lands here too: warn, not error
                problems.append(f"{name}: no backup in the last 8 days (new guest?)")
                lvl = max(lvl, WARNING)
                continue
            age = (st.now - b["ok"]) / 3600
            oldest = max(oldest, age)
            g = level(age, th["backups"])
            if g:
                problems.append(f"{name}: newest backup {_ago(age)} old")
                lvl = max(lvl, g)
        if problems:
            add("backups", lvl, "; ".join(problems[:4]) + (f" (+{len(problems) - 4} more)" if len(problems) > 4 else ""),
                oldest)
        else:
            add("backups", OK, f"{len(covered)} guests backed up, oldest {_ago(oldest)} ago", oldest)
    return done()
