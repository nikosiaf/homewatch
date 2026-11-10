"""Grafana markers: each incident as a band from its start to its end (with its checks and likely cause), and
each device event (a reboot, a resync, the UPS on battery, a gateway or PPPoE drop) as a point. They are
written to Grafana's own annotations through its API, as the user homewatch-markers (Editor), so a marker is
one stored item at its exact time; the dashboards show them by tag. Kept in homeWatch's database first and
sent when Grafana is running, so nothing is lost while it is off or restarting. (0.39 wrote them as metric
series, one series per event; 0.42 deletes those.)"""

from __future__ import annotations

import logging
from datetime import datetime

from .engine import tz
from .modules import LEVEL_WORD

log = logging.getLogger("homewatch.markers")

MARKER = {"X-Webauth-User": "homewatch-markers", "X-Webauth-Role": "Editor"}
BACKFILL = 30 * 86400  # older events and incidents are not sent (a fresh Grafana gets the last 30 days)
BATCH = 200  # at most this many per run, so a first back-fill doesn't hold up the checks
OLD_SERIES = '{__name__=~"hw_event_value|hw_incident_value"}'


def ms(ts: float) -> int:
    return int(ts * 1000)


def incident_text(db, inc, zone) -> str:
    rows = db.all("SELECT check_key, name, worst FROM issues WHERE incident_id=? ORDER BY started, id", (inc["id"],))
    checks = ", ".join(r["name"] or r["check_key"] for r in rows[:6]) + (" …" if len(rows) > 6 else "")
    start = datetime.fromtimestamp(inc["started"], zone).strftime("%H:%M:%S")
    text = f"Incident ({LEVEL_WORD.get(inc['worst'], '')}) from {start}: {checks}"
    if inc["ended"] is not None:
        end = datetime.fromtimestamp(inc["ended"], zone).strftime("%H:%M:%S")
        text += f". Over {end} after {max(int((inc['ended'] - inc['started']) // 60), 1)} min"
    if inc["cause"]:
        text += f". Likely cause: {inc['cause']}"
    return text


async def sync(rt, now: float) -> int:
    """Send the events and incidents Grafana doesn't have yet; returns how many were sent or updated."""
    db, site, http, base, zone = rt.db, rt.cfg.site, rt.notifier.http, rt.cfg.grafana_url, tz(rt.db)
    sent = 0
    if not db.get("markers.old_series_deleted"):
        try:
            await rt.metrics.delete(OLD_SERIES)
            db.set("markers.old_series_deleted", now)
        except Exception as e:  # noqa: BLE001 - try again at the next run
            log.warning("deleting the old marker series: %s", e)
    try:
        for e in db.all("SELECT * FROM events WHERE site=? AND marked=0 ORDER BY ts LIMIT ?", (site, BATCH)):
            if e["ts"] >= now - BACKFILL:
                at = datetime.fromtimestamp(e["ts"], zone).strftime("%H:%M:%S")
                r = await http.post(f"{base}/api/annotations", headers=MARKER, timeout=10, json={
                    "time": ms(e["ts"]), "tags": ["homewatch", "event", e["module"], e["kind"]],
                    "text": f"{e['device']}: {e['detail']} at {'' if e['exact'] else '~'}{at}"})
                r.raise_for_status()
                sent += 1
            db.run("UPDATE events SET marked=1 WHERE id=?", (e["id"],))
        for inc in db.all("SELECT * FROM incidents WHERE site=? AND (ann_id IS NULL OR (ann_end=0 AND ended IS NOT NULL)) "
                          "ORDER BY started LIMIT ?", (site, BATCH)):
            if inc["ann_id"] is None and inc["started"] < now - BACKFILL:
                db.run("UPDATE incidents SET ann_id=0, ann_end=1 WHERE id=?", (inc["id"],))
                continue
            # the end waits for the incident's "back to OK", which also carries the likely cause
            over = inc["ended"] is not None and db.one(
                "SELECT 1 FROM issues WHERE incident_id=? AND ok_sent=0", (inc["id"],)) is None
            body = {"time": ms(inc["started"]), "text": incident_text(db, inc, zone),
                    "tags": ["homewatch", "incident", LEVEL_WORD.get(inc["worst"], "")]}
            if over:
                body["timeEnd"] = ms(inc["ended"])
            if inc["ann_id"] is None:
                r = await http.post(f"{base}/api/annotations", headers=MARKER, timeout=10, json=body)
                r.raise_for_status()
                db.run("UPDATE incidents SET ann_id=?, ann_end=? WHERE id=?", (r.json()["id"], int(over), inc["id"]))
                sent += 1
            elif over:
                if inc["ann_id"]:
                    r = await http.patch(f"{base}/api/annotations/{inc['ann_id']}", headers=MARKER, timeout=10, json=body)
                    if r.status_code != 404:  # deleted in Grafana: leave it
                        r.raise_for_status()
                db.run("UPDATE incidents SET ann_end=1 WHERE id=?", (inc["id"],))
                sent += 1
    except Exception as e:  # noqa: BLE001 - Grafana busy or restarting: the rest goes at the next run
        log.warning("Grafana markers: %s", e)
    return sent
