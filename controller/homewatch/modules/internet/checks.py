"""Internet module checks: pure functions from readings to results (no I/O, easy to test).

Diagnosis order, so one message names the likely cause:
  1. no probe data at all      -> "monitoring itself broken", everything else suppressed
  2. gateway unreachable       -> "router or LAN problem", public checks suppressed
  3. all public targets down   -> "internet line down", loss/latency/jitter/DNS suppressed
  4. ping fine, DNS failing    -> "DNS failing"
  5. otherwise                 -> individual loss / latency / jitter / DNS / speed warnings
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .. import ERROR, OK, WARNING, CheckDef, CheckResult, Thresholds

MODULE = "internet"

CHECKS: list[CheckDef] = [
    CheckDef("fresh", "Probe data fresh", "any", unit="s", error=300, editable=("error",),
             help="ERROR when no ping or DNS data arrived for this many seconds."),
    CheckDef("gateway", "Gateway reachable", "gateway",
             help="ERROR when the home router answers no pings."),
    CheckDef("reachable", "Internet reachable", "public",
             help="ERROR when every public target is at 100 % packet loss."),
    CheckDef("loss", "Packet loss", "public", unit="%", warn=25, error=50, editable=("warn", "error"),
             help="5-minute average, best public target."),
    CheckDef("latency", "Latency", "public", unit="ms", warn=200, editable=("warn", "error"),
             help="5-minute median round trip, best public target."),
    CheckDef("jitter", "Jitter", "public", unit="ms", warn=50, error=100, editable=("warn", "error"),
             help="10-minute median of the ping standard deviation, best public target."),
    CheckDef("dns", "DNS", "dns", unit="ms", warn=1000, editable=("warn",),
             help="ERROR when lookups fail while ping works; WARNING when the 5-minute median is slow."),
    CheckDef("speed", "Line speed", "speed", unit="%", warn=50, editable=("warn", "error"), lower_is_worse=True,
             help="Download or upload below this share of the contract on 2 tests in a row."),
    CheckDef("speed_age", "Speed test recent", "speed", unit="h", warn=14, editable=("warn",),
             help="WARNING when no speed test succeeded for this many hours (keep it above twice the test interval)."),
    CheckDef("speed_ping", "Speed test latency", "speed", unit="ms", warn=40, editable=("warn",), default_on=False,
             help="Latency measured by the Ookla speed test, on 2 tests in a row. Cloudflare's test measures "
                  "over HTTP, which reads much higher, so it doesn't count."),
]
CHECK_BY_ID = {c.id: c for c in CHECKS}


@dataclass
class Target:
    loss_now: float | None = None  # last run, %
    loss_avg: float | None = None  # 5 min, %
    rtt_avg: float | None = None  # 5 min, ms
    jitter_avg: float | None = None  # 10 min, ms
    last_ts: float | None = None


@dataclass
class Dns:
    ok_now: bool | None = None
    result: str = ""
    time_avg: float | None = None
    last_ts: float | None = None


@dataclass
class Speed:
    ts: float
    down: float | None
    up: float | None
    ok: bool
    latency: float | None = None
    engine: str = ""


@dataclass
class Readings:
    now: float
    public: dict[str, Target] = field(default_factory=dict)
    gateway: Target | None = None
    dns: Dns | None = None
    speeds: list[Speed] = field(default_factory=list)  # newest first
    public_trend: list[tuple[float, float]] = field(default_factory=list)  # (ts, best target's loss %), oldest first


def began(trend: list[tuple[float, float]], down: bool) -> float | None:
    """When the current state (down = every target at 100 %) began in the trend; None when it fills the whole
    trend or the trend doesn't show it now."""
    if not trend or (trend[-1][1] >= 100) != down:
        return None
    start = None
    for ts, loss in reversed(trend):
        if (loss >= 100) != down:
            return start
        start = ts
    return None


def _key(check_id: str) -> str:
    return f"{MODULE}.{check_id}"


def _level(value: float | None, t: Thresholds) -> int:
    if value is None:
        return OK
    if t.error is not None and value >= t.error:
        return ERROR
    if t.warn is not None and value >= t.warn:
        return WARNING
    return OK


def _fmt(v: float | None, unit: str) -> str:
    if v is None:
        return "no data"
    return f"{v:.0f} {unit}" if v >= 10 else f"{v:.1f} {unit}"


def _ago(seconds: float) -> str:
    m = int(seconds // 60)
    return f"{m} min" if m < 120 else f"{m // 60} h {m % 60} min"


def evaluate(settings: dict, thresholds: dict[str, Thresholds], r: Readings) -> list[CheckResult]:
    """Return one result per enabled check of an enabled probe."""
    probes = {
        "public": settings["public"]["enabled"] and bool(settings["public"]["targets"]),
        "gateway": settings["gateway"]["enabled"] and bool(settings["gateway"]["target"]),
        "dns": settings["dns"]["enabled"] and bool(settings["dns"]["server"]),
        "speed": settings["speed"]["enabled"],
    }
    probes["any"] = probes["public"] or probes["gateway"] or probes["dns"]
    out: dict[str, CheckResult] = {}

    def add(check_id: str, status: int, detail: str, value=None, suppressed_by=None, since=None):
        d = CHECK_BY_ID[check_id]
        out[check_id] = CheckResult(_key(check_id), d.name, status, detail, value, d.unit,
                                    _key(suppressed_by) if suppressed_by else None, since=since)

    # 1. freshness of the Telegraf probes
    root = None
    if probes["any"]:
        stamps = [t.last_ts for t in r.public.values() if t.last_ts] if probes["public"] else []
        if probes["gateway"] and r.gateway and r.gateway.last_ts:
            stamps.append(r.gateway.last_ts)
        if probes["dns"] and r.dns and r.dns.last_ts:
            stamps.append(r.dns.last_ts)
        limit = thresholds["fresh"].error or 180
        if not stamps:
            add("fresh", ERROR, "No probe data at all: is the Telegraf container running?")
            root = "fresh"
        else:
            age = r.now - max(stamps)
            if age > limit:
                add("fresh", ERROR, f"No probe data for {_ago(age)}: monitoring itself is broken", age)
                root = "fresh"
            else:
                add("fresh", OK, f"Last probe data {age:.0f} s ago", age)

    # 2. gateway
    if probes["gateway"]:
        g = r.gateway
        target = settings["gateway"]["target"]
        if root:
            add("gateway", OK, "no data", suppressed_by=root)
        elif g is None or g.loss_now is None:
            add("gateway", OK, f"No data yet for {target}")
        elif g.loss_now >= 100:
            add("gateway", ERROR, f"Router or LAN problem: gateway {target} does not answer", g.loss_now)
            root = "gateway"
        else:
            add("gateway", OK, f"{target} answers in {_fmt(g.rtt_avg, 'ms')}", g.loss_now)

    # 3. public targets
    line_down = False
    if probes["public"]:
        targets = [(name, t) for name, t in r.public.items() if t.loss_now is not None]
        if root:
            for c in ("reachable", "loss", "latency", "jitter"):
                add(c, OK, "no data" if root == "fresh" else "gateway down", suppressed_by=root)
        elif not targets:
            add("reachable", OK, "No data yet")
        else:
            down = [n for n, t in targets if t.loss_now >= 100]
            if len(down) == len(targets):
                add("reachable", ERROR, f"Internet line down: none of {', '.join(down)} answers", 100,
                    since=began(r.public_trend, True))
                line_down = True
                for c in ("loss", "latency", "jitter"):
                    add(c, OK, "line down", suppressed_by="reachable")
            else:
                note = f" ({', '.join(down)} not answering)" if down else ""
                add("reachable", OK, f"{len(targets) - len(down)} of {len(targets)} targets answer{note}", 0,
                    since=began(r.public_trend, False))

                def best(attr):
                    vals = [(getattr(t, attr), n) for n, t in targets if getattr(t, attr) is not None and n not in down]
                    return min(vals) if vals else (None, "")

                for cid, attr, what in (("loss", "loss_avg", "packet loss"), ("latency", "rtt_avg", "latency"),
                                        ("jitter", "jitter_avg", "jitter")):
                    if not thresholds[cid].enabled:
                        continue
                    v, n = best(attr)
                    unit = CHECK_BY_ID[cid].unit
                    lvl = _level(v, thresholds[cid])
                    add(cid, lvl, f"{what} {_fmt(v, unit)} (best target {n})" if v is not None else "No data yet", v)

    # 4. DNS
    if probes["dns"]:
        d = r.dns
        server = settings["dns"]["server"]
        domain = settings["dns"]["domain"]
        if root:
            add("dns", OK, "no data" if root == "fresh" else "gateway down", suppressed_by=root)
        elif line_down:
            add("dns", OK, "line down", suppressed_by="reachable")
        elif d is None or d.ok_now is None:
            add("dns", OK, "No data yet")
        elif not d.ok_now:
            add("dns", ERROR, f"DNS failing: {domain} via {server} returns {d.result or 'no answer'}")
        else:
            lvl = _level(d.time_avg, thresholds["dns"])
            add("dns", lvl, f"{domain} via {server} in {_fmt(d.time_avg, 'ms')}", d.time_avg)

    # 5. speed (from the speed test history, newest first)
    if probes["speed"]:
        add_speed(settings["line"], thresholds, r.speeds, add, r.now)

    return [out[c.id] for c in CHECKS if c.id in out and thresholds[c.id].enabled]


def add_speed(s: dict, ths: dict[str, Thresholds], speeds: list[Speed], add, now: float) -> None:
    t = ths["speed"]
    c_down, c_up = float(s.get("contract_down") or 0), float(s.get("contract_up") or 0)
    good = [x for x in speeds if x.ok][:2]
    if not good:
        add("speed", OK, "No speed test yet")
        return
    last = good[0]
    age = (now - last.ts) / 3600
    add("speed_age", _level(age, ths["speed_age"]),
        f"last successful test {age:.1f} h ago" if age >= 1 else f"last successful test {age * 60:.0f} min ago", age)
    pings = [x.latency for x in good if x.latency is not None and x.engine != "cloudflare"]
    if pings:
        slow = len(pings) >= 2 and ths["speed_ping"].warn is not None and all(p > ths["speed_ping"].warn for p in pings)
        add("speed_ping", WARNING if slow else OK,
            f"{pings[0]:.0f} ms" + (" on 2 tests in a row" if slow else ""), pings[0])
    else:
        add("speed_ping", OK, "only measured by the Ookla engine")
    contract = f" of {c_down:g} / {c_up:g}" if c_down and c_up else ""
    detail = f"{last.down or 0:.1f} / {last.up or 0:.1f} Mbit/s"
    if not (c_down and c_up) or (t.warn is None and t.error is None):
        add("speed", OK, f"{detail} (no contracted speed set)")
        return

    def pct(x: Speed) -> float:
        return min((x.down or 0) / c_down, (x.up or 0) / c_up) * 100

    share = pct(last)
    detail = f"{detail} ({share:.0f} %{contract})"

    def low(limit):
        return limit is not None and len(good) >= 2 and all(pct(x) < limit for x in good)

    lvl = ERROR if low(t.error) else WARNING if low(t.warn) else OK
    add("speed", lvl, detail + (" on 2 tests in a row" if lvl else ""), share)
