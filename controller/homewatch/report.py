"""Weekly report: uptime, outages, line quality and speed for the past 7 days."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta

from .db import DB
from .engine import tz
from .metrics import Metrics, MetricsError
from .modules import OK
from .modules.internet import load as load_internet
from .notify import Message, _dur

WEEK = 7 * 24 * 3600


@dataclass
class Availability:
    period: float  # seconds asked about
    observed: float  # seconds of it when the line was really being watched
    down: float  # seconds of the observed time with the line down
    outages: int
    last_observed: float | None

    @property
    def percent(self) -> float | None:
        """Share of the observed time the line was up; None when nothing was observed."""
        return 100 * (1 - self.down / self.observed) if self.observed > 0 else None

    @property
    def coverage(self) -> float:
        return 100 * self.observed / self.period if self.period > 0 else 0


def availability(db: DB, site: str, start: float, end: float, module: str = "internet",
                 key: str = "internet.reachable") -> Availability:
    """Uptime over what was observed. Time before the install or with monitoring down is unknown, not uptime."""
    spans = [(max(r["start"], start), min(r["end"], end)) for r in
             db.all("SELECT start, end FROM coverage WHERE site=? AND module=? AND start<? AND end>?",
                    (site, module, end, start))]
    issues = db.all("SELECT started, resolved FROM issues WHERE site=? AND check_key=? AND started<? "
                    "AND (resolved IS NULL OR resolved>?)", (site, key, end, start))
    down, outages = 0.0, 0
    for i in issues:
        seen = sum(max(0.0, min(i["resolved"] or end, b) - max(i["started"], a)) for a, b in spans)
        down += seen
        outages += seen > 0
    last = db.one("SELECT MAX(end) e FROM coverage WHERE site=? AND module=?", (site, module))
    return Availability(end - start, sum(b - a for a, b in spans), down, outages, last["e"] if last else None)


async def build(db: DB, metrics: Metrics, site: str, now: float | None = None) -> Message:
    now = now or time.time()
    start = now - WEEK
    zone = tz(db)
    home = db.get("home.name") or "Home"
    lines = []

    a = availability(db, site, start, now)
    if a.percent is None:
        lines.append("Internet availability: not monitored this week")
    else:
        lines.append(f"Internet availability {a.percent:.2f} % · {a.outages} outage{'s' if a.outages != 1 else ''}"
                     + (f", {_dur(a.down)} down in total" if a.outages else "")
                     + f" · monitored {a.coverage:.0f} % of the week")

    sel = f'{{site="{site}",role="public"}}'
    quality = []
    for label, metric, unit in (("Latency", "ping_average_response_ms", "ms"),
                                ("Packet loss", "ping_percent_packet_loss", "%"),
                                ("Jitter", "ping_standard_deviation_ms", "ms")):
        try:
            avg = await metrics.query(f"min(avg_over_time({metric}{sel}[7d]))", at=now)
            worst = await metrics.query(f"min(max_over_time(avg_over_time({metric}{sel}[5m])[7d:5m]))", at=now)
        except MetricsError:
            continue
        if avg:
            w = f", worst 5-min {worst[0][2]:.1f} {unit}" if worst else ""
            quality.append(f"{label}: average {avg[0][2]:.1f} {unit}{w}")
    lines += quality

    tests = db.all("SELECT down, up FROM speedtests WHERE site=? AND ts>? AND ok=1", (site, start))
    if tests:
        avg_d = sum(t["down"] for t in tests) / len(tests)
        avg_u = sum(t["up"] for t in tests) / len(tests)
        low = min(tests, key=lambda t: t["down"])
        sp = load_internet(db)["line"]
        contract = (f" (contract {sp['contract_down']:g} / {sp['contract_up']:g})"
                    if sp.get("contract_down") and sp.get("contract_up") else "")
        lines.append(f"Speed: average {avg_d:.1f} / {avg_u:.1f} Mbit/s{contract}, lowest download "
                     f"{low['down']:.1f} Mbit/s, {len(tests)} tests")

    top = db.all("SELECT check_key, worst, started, COALESCE(resolved, ?) - MAX(started, ?) AS dur FROM issues "
                 "WHERE site=? AND started<? AND (resolved IS NULL OR resolved>?) ORDER BY dur DESC LIMIT 3",
                 (now, start, site, now, start))
    if top:
        lines.append("")
        lines.append("Longest issues:")
        for t in top:
            when = datetime.fromtimestamp(t["started"], zone).strftime("%a %d %b %H:%M")
            lines.append(f"  · {t['check_key']}: {_dur(t['dur'])} from {when}")

    first = datetime.fromtimestamp(start, zone).strftime("%d %b")
    last = datetime.fromtimestamp(now, zone).strftime("%d %b")
    link = db.get("home.url") or ""
    body = "\n".join(lines) + (f"\n\n{link}" if link else "")
    return Message(OK, f"📊 {home}: weekly report {first}–{last}", body)


def due(db: DB, now: float) -> bool:
    """Monday 08:00 local time (editable), once per week."""
    cfg = db.get("report.schedule") or {"weekday": 0, "time": "08:00"}
    zone = tz(db)
    local = datetime.fromtimestamp(now, zone)
    h, m = map(int, cfg["time"].split(":"))
    days_back = (local.weekday() - cfg["weekday"]) % 7
    slot = (local - timedelta(days=days_back)).replace(hour=h, minute=m, second=0, microsecond=0)
    if slot > local:
        slot -= timedelta(days=7)
    return db.get("report.last_sent", 0) < slot.timestamp()
