"""Reports: uptime, outages and their likely causes, line quality and speed, for the weekly message and for
any period on the report page (/history/report) and its CSV files."""

from __future__ import annotations

import csv
import io
import json
import math
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


def _down_spans(db: DB, site: str, start: float, end: float, module: str, key: str):
    """The observed spans of the period, and each of the key's issues in it with its seconds seen down."""
    spans = [(max(r["start"], start), min(r["end"], end)) for r in
             db.all("SELECT start, end FROM coverage WHERE site=? AND module=? AND start<? AND end>?",
                    (site, module, end, start))]
    issues = db.all("SELECT * FROM issues WHERE site=? AND check_key=? AND started<? "
                    "AND (resolved IS NULL OR resolved>?) ORDER BY started, id", (site, key, end, start))
    seen = [(i, sum(max(0.0, min(i["resolved"] or end, b) - max(i["started"], a)) for a, b in spans)) for i in issues]
    return spans, seen


def availability(db: DB, site: str, start: float, end: float, module: str = "internet",
                 key: str = "internet.reachable") -> Availability:
    """Uptime over what was observed. Time before the install or with monitoring down is unknown, not uptime."""
    spans, seen = _down_spans(db, site, start, end, module, key)
    last = db.one("SELECT MAX(end) e FROM coverage WHERE site=? AND module=?", (site, module))
    return Availability(end - start, sum(b - a for a, b in spans), sum(s for _, s in seen),
                        sum(1 for _, s in seen if s > 0), last["e"] if last else None)


@dataclass
class Outage:
    started: float
    resolved: float | None  # None: still down; may be after the period's end
    down: float  # seconds of it seen down inside the period
    cause: str  # causes.explain's likely cause, '' when no rule fits
    side: str  # causes.ISP | HOME | UNKNOWN


def outages(db: DB, site: str, start: float, end: float, zone) -> list[Outage]:
    """The line's outages in the period (the ones availability counts), each with its likely cause."""
    from . import causes

    out = []
    for i, down in _down_spans(db, site, start, end, "internet", "internet.reachable")[1]:
        if down <= 0:
            continue
        inc = db.one("SELECT * FROM incidents WHERE id=?", (i["incident_id"],)) if i["incident_id"] else None
        if inc:
            rows = db.all("SELECT check_key, nodes FROM issues WHERE incident_id=?", (inc["id"],))
            keys = [r["check_key"] for r in rows]
            nodes = sorted({n for r in rows for n in json.loads(r["nodes"] or "[]")})
            a, b = inc["started"], inc["ended"]
        else:
            keys, nodes, a, b = [i["check_key"]], [], i["started"], i["resolved"]
        cause, side, _ = causes.classify(db, site, a, b, keys, zone, nodes)
        if not cause and inc and inc["cause"]:  # told when it ended; the events behind it are gone now
            cause, side = inc["cause"], causes.side_of(inc["cause"])
        out.append(Outage(i["started"], i["resolved"], down, cause, side))
    return out


@dataclass
class SpeedStats:
    tests: list  # [{ts, engine, down, up, latency}], oldest first
    contract_down: float = 0  # Mbit/s, 0: not set
    contract_up: float = 0
    min_down: float = 0  # the guaranteed minimum, 0: not set
    min_up: float = 0

    def _vals(self, k: str) -> list[float]:
        return [t[k] for t in self.tests if t[k] is not None]

    def stat(self, k: str) -> dict | None:
        """{avg, min, max} of 'down' or 'up' in Mbit/s, None without a value."""
        v = self._vals(k)
        return {"avg": sum(v) / len(v), "min": min(v), "max": max(v)} if v else None

    def share_of_contract(self, k: str) -> float | None:
        """The average as a % of the contracted speed; None when that isn't set."""
        st, c = self.stat(k), self.contract_down if k == "down" else self.contract_up
        return 100 * st["avg"] / c if st and c else None

    def below_minimum(self, k: str) -> tuple[int, float] | None:
        """(tests below the guaranteed minimum, their % of the tests); None when no minimum is set."""
        v, m = self._vals(k), self.min_down if k == "down" else self.min_up
        if not m or not v:
            return None
        n = sum(1 for x in v if x < m)
        return n, 100 * n / len(v)


def speed(db: DB, site: str, start: float, end: float) -> SpeedStats:
    line = load_internet(db)["line"]
    tests = [dict(r) for r in db.all("SELECT ts, engine, down, up, latency FROM speedtests WHERE site=? AND ts>=? "
                                     "AND ts<? AND ok=1 ORDER BY ts", (site, start, end))]
    return SpeedStats(tests, *(float(line.get(k) or 0) for k in ("contract_down", "contract_up", "min_down", "min_up")))


# the modem's line, from the readings in VictoriaMetrics: (label, field of hw_dsl, unit, scale, lower is worse)
LINE_STATS = (("Sync rate down", "sync_down_kbps", "Mbit/s", 0.001), ("Sync rate up", "sync_up_kbps", "Mbit/s", 0.001),
              ("SNR margin down", "snr_down_db", "dB", 1), ("SNR margin up", "snr_up_db", "dB", 1))


async def line_stats(db: DB, metrics: Metrics, site: str, start: float, end: float) -> dict:
    """The modem's line over the period: average and worst of each value (None when the history store can't be
    read), plus the resyncs, drops and restarts the modem module saw."""
    rng = f"[{max(60, int(end - start))}s]"
    values = []
    for label, field, unit, scale in LINE_STATS:
        sel = f'hw_dsl_{field}{{site="{site}"}}'
        try:
            avg = await metrics.query(f"avg_over_time({sel}{rng})", at=end)
            low = await metrics.query(f"min_over_time({sel}{rng})", at=end)
        except MetricsError:
            values = None
            break
        values.append({"label": label, "unit": unit, "avg": avg[0][2] * scale if avg else None,
                       "worst": low[0][2] * scale if low else None})
    counts = {r["kind"]: r["n"] for r in db.all(
        "SELECT kind, COUNT(*) n FROM events WHERE site=? AND module='speedport' AND ts>=? AND ts<? GROUP BY kind",
        (site, start, end))}
    return {"values": values, "resyncs": counts.get("line_resync", 0), "drops": counts.get("line_down", 0),
            "restarts": counts.get("reboot", 0)}


async def quality(metrics: Metrics, site: str, start: float, end: float) -> list[dict]:
    """Latency, loss and jitter of the public ping: average and worst 5 minutes."""
    sel = f'{{site="{site}",role="public"}}'
    rng = f"[{max(300, int(end - start))}s]"
    step = f"{max(300, math.ceil((end - start) / 50000))}s"
    out = []
    for label, metric, unit in (("Latency", "ping_average_response_ms", "ms"),
                                ("Packet loss", "ping_percent_packet_loss", "%"),
                                ("Jitter", "ping_standard_deviation_ms", "ms")):
        try:
            avg = await metrics.query(f"min(avg_over_time({metric}{sel}{rng}))", at=end)
            worst = await metrics.query(
                f"min(max_over_time(avg_over_time({metric}{sel}[5m]){rng[:-1]}:{step}]))", at=end)
        except MetricsError:
            continue
        if avg:
            out.append({"label": label, "unit": unit, "avg": avg[0][2], "worst": worst[0][2] if worst else None})
    return out


def _module_on(db: DB, key: str) -> bool:
    return bool((db.get(key) or {}).get("enabled"))


@dataclass
class Report:
    start: float
    end: float
    home: str
    internet: bool  # the Internet line module is on: availability, outages, quality and speed
    modem: bool  # the ISP modem module is on: line statistics
    availability: Availability
    outages: list[Outage]
    quality: list[dict]
    speed: SpeedStats | None  # None: no speed test is run and none was in the period
    line: dict | None  # None: the modem module is off

    def by_side(self) -> dict[str, dict]:
        """Outage count and seconds down for each side (causes.SIDES), all three always present."""
        from .causes import SIDES

        out = {k: {"label": v, "outages": 0, "down": 0.0} for k, v in SIDES.items()}
        for o in self.outages:
            out[o.side]["outages"] += 1
            out[o.side]["down"] += o.down
        return out


async def gather(db: DB, metrics: Metrics, site: str, start: float, end: float, with_line: bool = True) -> Report:
    """Everything the report shows for any period; read only from what is stored, never from a device."""
    zone = tz(db)
    internet = bool(load_internet(db)["enabled"])
    modem = with_line and _module_on(db, "module.speedport")
    sp = speed(db, site, start, end)
    if not (load_internet(db)["speed"]["enabled"] or sp.tests):
        sp = None
    return Report(start, end, db.get("home.name") or "Home", internet, modem, availability(db, site, start, end),
                  outages(db, site, start, end, zone), await quality(metrics, site, start, end), sp,
                  await line_stats(db, metrics, site, start, end) if modem else None)


async def build(db: DB, metrics: Metrics, site: str, now: float | None = None) -> Message:
    """The weekly report message: the past 7 days."""
    now = now or time.time()
    start = now - WEEK
    zone = tz(db)
    lines = []
    r = await gather(db, metrics, site, start, now, with_line=False)

    a = r.availability
    if a.percent is None:
        lines.append("Internet availability: not monitored this week")
    else:
        lines.append(f"Internet availability {a.percent:.2f} % · {a.outages} outage{'s' if a.outages != 1 else ''}"
                     + (f", {_dur(a.down)} down in total" if a.outages else "")
                     + f" · monitored {a.coverage:.0f} % of the week")

    for q in r.quality:
        w = f", worst 5-min {q['worst']:.1f} {q['unit']}" if q["worst"] is not None else ""
        lines.append(f"{q['label']}: average {q['avg']:.1f} {q['unit']}{w}")

    if r.speed and r.speed.tests:
        sp, d, u = r.speed, r.speed.stat("down"), r.speed.stat("up")
        contract = f" (contract {sp.contract_down:g} / {sp.contract_up:g})" if sp.contract_down and sp.contract_up else ""
        lines.append(f"Speed: average {d['avg'] if d else 0:.1f} / {u['avg'] if u else 0:.1f} Mbit/s{contract}, "
                     f"lowest download {d['min'] if d else 0:.1f} Mbit/s, {len(sp.tests)} tests")

    top = db.all("SELECT check_key, worst, started, COALESCE(resolved, ?) - MAX(started, ?) AS dur FROM issues "
                 "WHERE site=? AND started<? AND (resolved IS NULL OR resolved>?) ORDER BY dur DESC LIMIT 3",
                 (now, start, site, now, start))
    if top:
        lines.append("")
        lines.append("Longest issues:")
        for t in top:
            when = datetime.fromtimestamp(t["started"], zone).strftime("%a %d %b %H:%M")
            lines.append(f"  · {t['check_key']}: {_dur(t['dur'])} from {when}")

    from .todo import days, open_items

    todos = open_items(db, site)
    if todos:
        lines.append("")
        lines.append("To do:")
        for t in todos:
            lines.append(f"  · {t['name'] or t['check_key']}: {t['item']} (waiting {days(now - t['first_seen'])})")

    noisy = db.all("SELECT check_key, COUNT(*) n, SUM(feedback=-1) noise FROM issues WHERE site=? AND started>? "
                   "GROUP BY check_key HAVING n>=3 OR noise>0 ORDER BY noise DESC, n DESC LIMIT 3", (site, start))
    if noisy:
        lines.append("")
        lines.append("Most issues this week:")
        for t in noisy:
            mark = f", {t['noise']} marked as noise" if t["noise"] else ""
            lines.append(f"  · {t['check_key']}: {t['n']} issue{'s' if t['n'] != 1 else ''}{mark}")

    first = datetime.fromtimestamp(start, zone).strftime("%d %b")
    last = datetime.fromtimestamp(now, zone).strftime("%d %b")
    link = db.get("home.url") or ""
    body = "\n".join(lines) + (f"\n\n{link}" if link else "")
    return Message(OK, f"📊 {r.home}: weekly report {first}–{last}", body)


# ---- the report page: any period, as a page to print and CSV files ------------------------------------------
MAX_DAYS = 400


def period(zone, frm: str, to: str, now: float) -> tuple[float, float]:
    """From and to (local dates YYYY-MM-DD, both days included) as (start, end), the end no later than now.
    ValueError with a message for the user when they don't make a period."""
    def at(v: str, name: str, day_after: bool) -> float:
        try:
            d = datetime.strptime((v or "").strip(), "%Y-%m-%d").replace(tzinfo=zone)
        except ValueError:
            raise ValueError(f"{name}: a date as YYYY-MM-DD") from None
        return (d + timedelta(days=1) if day_after else d).timestamp()

    start, end = at(frm, "From", False), min(at(to, "To", True), now)
    if start >= now:
        raise ValueError("The period starts in the future")
    if end <= start:
        raise ValueError("To is before From")
    if end - start > MAX_DAYS * 86400 + 3600:  # an hour of slack for a summer-time change
        raise ValueError(f"At most {MAX_DAYS} days")
    return start, end


def quick_picks(zone, now: float) -> list[tuple[str, str, str]]:
    """(label, from, to) for the page's quick choices."""
    today = datetime.fromtimestamp(now, zone).date()
    first = today.replace(day=1)
    prev_last = first - timedelta(days=1)
    f = lambda d: d.isoformat()  # noqa: E731
    return [("Last 7 days", f(today - timedelta(days=6)), f(today)),
            ("Last 30 days", f(today - timedelta(days=29)), f(today)),
            (f"Previous month ({prev_last.strftime('%B %Y')})", f(prev_last.replace(day=1)), f(prev_last)),
            ("This month", f(first), f(today))]


def _iso(ts: float | None, zone) -> str:
    return datetime.fromtimestamp(ts, zone).isoformat(timespec="seconds") if ts else ""


def csv_outages(r: Report, zone) -> str:
    from .causes import SIDES

    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["started", "ended", "down_minutes", "side", "likely_cause"])
    for o in r.outages:
        w.writerow([_iso(o.started, zone), _iso(o.resolved, zone), f"{o.down / 60:.1f}", SIDES[o.side], o.cause])
    return out.getvalue()


def csv_speedtests(r: Report, zone) -> str:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["time", "engine", "download_mbps", "upload_mbps", "latency_ms"])
    for t in r.speed.tests if r.speed else []:
        w.writerow([_iso(t["ts"], zone), t["engine"]] + ["" if t[k] is None else f"{t[k]:.1f}"
                                                         for k in ("down", "up", "latency")])
    return out.getvalue()


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
