"""Reads the Internet module's metrics from VictoriaMetrics into Readings."""

from __future__ import annotations

import time

from ...db import DB
from ...metrics import Metrics
from .checks import Dns, Readings, Speed, Target

# Telegraf's dns_query result_code: 0 success, 1 timeout, 2 error
DNS_RESULT = {0: "success", 1: "timeout", 2: "error"}


async def collect(metrics: Metrics, db: DB, site: str, now: float | None = None) -> Readings:
    now = now or time.time()
    r = Readings(now=now)
    sel = f'site="{site}"'

    async def by_target(expr: str) -> dict[str, float]:
        return {labels.get("url", ""): v for labels, _, v in await metrics.query(expr, at=now)}

    for role in ("public", "gateway"):
        s = f'{{{sel},role="{role}"}}'
        last_ts = await by_target(f"tlast_over_time(ping_percent_packet_loss{s}[1h])")
        loss_now = await by_target(f"last_over_time(ping_percent_packet_loss{s}[90s])")
        loss_avg = await by_target(f"avg_over_time(ping_percent_packet_loss{s}[5m])")
        # medians: one odd ping round must not raise an alert on its own (a single 1.1 s round once
        # pushed the 10-minute jitter average to 56 ms); sustained trouble still moves the median
        rtt = await by_target(f"median_over_time(ping_average_response_ms{s}[5m])")
        jitter = await by_target(f"median_over_time(ping_standard_deviation_ms{s}[10m])")
        targets = {
            url: Target(loss_now.get(url), loss_avg.get(url), rtt.get(url), jitter.get(url), ts)
            for url, ts in last_ts.items()
        }
        if role == "public":
            r.public = targets
        elif targets:
            r.gateway = next(iter(targets.values()))

    s = f"{{{sel}}}"
    last = await metrics.query(f"tlast_over_time(dns_query_result_code{s}[1h])", at=now)
    if last:
        code = await metrics.query(f"max(last_over_time(dns_query_result_code{s}[150s]))", at=now)
        qtime = await metrics.query(f"avg(median_over_time(dns_query_query_time_ms{s}[5m]))", at=now)
        c = int(code[0][2]) if code else None
        r.dns = Dns(
            ok_now=None if c is None else c == 0,
            result=DNS_RESULT.get(c, str(c)) if c is not None else "",
            time_avg=qtime[0][2] if qtime else None,
            last_ts=max(v for _, _, v in last),
        )

    rows = db.all("SELECT ts, down, up, ok, latency, engine FROM speedtests WHERE site=? ORDER BY ts DESC LIMIT 5", (site,))
    r.speeds = [Speed(row["ts"], row["down"], row["up"], bool(row["ok"]), row["latency"], row["engine"]) for row in rows]
    return r
