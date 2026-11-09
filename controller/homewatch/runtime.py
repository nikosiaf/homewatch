"""Background loops: checks, speed tests, outbox, weekly report, heartbeat and update check."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import math
import os
import time
from dataclasses import dataclass, field

import httpx

from . import __version__, grafana, report, versions
from .auth import audit, scrub_audit
from .config import Config
from .db import DB
from .engine import Engine, thresholds, timing
from .metrics import Metrics, MetricsError, line
from .modules import ERROR, CheckResult
from .modules import internet as inet
from .modules.internet import speedtest, telegraf
from .modules.registry import MODULES
from .notify import Notifier, channel_private, channel_secret, destination, forget_channel, store_private
from .secrets import Secrets

log = logging.getLogger("homewatch.runtime")

HC_SECRET = "healthchecks.url"
MODULE_LIMIT = 90  # seconds one module may take to read; devices are read one after the other
SPEED_RETRY = 15 * 60  # after a failed speed test, try again this soon instead of waiting a full interval
CHECK_NOW_GAP = 30  # s: "Check now" reads every device fresh at most this often; sooner it is an ordinary run
CHECK_NOW_CONFIRM = 3  # s: a change that "Check now" saw is read again this much later, to confirm it
HISTORY_TABLES = ("issues", "speedtests", "check_state", "outbox", "snoozes", "coverage")
CONFIG_TABLES = ("settings", "checks", "secrets", "maintenance", "channels", "sessions", "users", "audit",
                 "config_versions")
RELEASES = "https://api.github.com/repos/nikosiaf/homewatch/releases/latest"


@dataclass
class Runtime:
    cfg: Config
    db: DB
    secrets: Secrets
    metrics: Metrics
    notifier: Notifier
    engine: Engine
    results: list[CheckResult] = field(default_factory=list)
    last_run: float = 0
    last_error: str = ""
    speed_running: bool = False
    dropped: list[str] = field(default_factory=list)  # what the last import or restore cut off from its credentials
    drop_why: list[str] = field(default_factory=list)
    drafts: dict = field(default_factory=dict)  # (user id, page): what was typed in a form that didn't save; never secrets
    speed_started: float = 0
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    tasks: list[asyncio.Task] = field(default_factory=list)
    series_written: dict = field(default_factory=dict)  # module id -> reading_ts of the reading already written
    grafana_for: tuple | None = None  # (enabled modules, recorded HA entities) the dashboards were last written for
    grafana_on: bool = False  # Grafana answered at the last run: the pages then link to it
    check_now_at: float = 0  # when "Check now" last read every device fresh

    @classmethod
    def create(cls, cfg: Config) -> Runtime:
        os.makedirs(cfg.data_dir, exist_ok=True)
        os.makedirs(cfg.ookla_dir, exist_ok=True)
        db = DB(cfg.db_path)
        secrets = Secrets(db, cfg.master_key)
        if versions.move_secret_fields(db, secrets):
            scrub_audit(db)
            with db.lock:  # the old values must not linger in free pages or the write-ahead log
                db.conn.execute("VACUUM")
                db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        versions.scrub_exports(os.path.join(cfg.data_dir, "exports"))
        db.run("DELETE FROM settings WHERE key='status.tiles' OR key LIKE 'status.open.%'")  # settings of 0.16.1 to 0.16.4
        versions.record(db, "system", "startup")  # also catches changes made while homeWatch was stopped
        rt = cls(cfg, db, secrets, Metrics(cfg.vm_url), Notifier(db, secrets), Engine(db, cfg.site))
        rt.write_grafana()  # before Grafana starts (it waits for a healthy controller): it reads them once at start
        return rt

    # ---- settings helpers ----------------------------------------------------------------
    def internet_settings(self) -> dict:
        return inet.load(self.db, self.cfg.gateway_hint, self.cfg.dns_hint)

    def sync_telegraf(self) -> None:
        text = telegraf.render(self.internet_settings(), self.cfg.site)
        if telegraf.write(self.cfg.telegraf_dir, text):
            log.info("telegraf config written")

    def names(self) -> dict[str, str]:
        return {f"{m.id}.{c.id}": f"{c.name}" if m.id == "internet" else f"{m.name} · {c.name}"
                for m in MODULES.values() for c in m.checks}

    # ---- loops ---------------------------------------------------------------------------
    def start(self) -> None:
        self.migrate_heartbeat()
        scrub_audit(self.db)
        self.sync_telegraf()
        coros = [self.check_loop(), self.outbox_loop(), self.daily_loop()]
        for m in MODULES.values():
            coros += m.loops(self)
        for coro in coros:
            self.tasks.append(asyncio.create_task(coro))

    async def stop(self) -> None:
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.notifier.http.aclose()
        for m in MODULES.values():
            if getattr(m, "client", None):
                await m.client.close()
                m.client = None

    async def check_loop(self) -> None:
        await asyncio.sleep(5)
        while True:
            try:
                await self.run_checks()
            except Exception:  # noqa: BLE001 - the loop must survive anything
                log.exception("check run failed")
            interval = max(30, int(timing(self.db)["interval"]))
            self.wake.clear()
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=interval)
            except TimeoutError:
                pass

    def read_fresh(self, ids) -> list[str]:
        """Make these modules read their device at the next run, whatever their "Read every".
        Returns the ones that read it in that run (the ISP modem's own reader comes within 30 s instead)."""
        out = []
        for mid in ids:
            m = MODULES[mid]
            if hasattr(m, "stale"):
                m.stale = True
                out.append(mid)
            else:
                m.refresh_soon(self)
        return out

    async def check_now(self, now: float | None = None) -> bool:
        """The Status page's "Check now": every device read fresh, and a change that shows up read once more
        a few seconds later, so that two fresh readings confirm it (or take it back) before the page shows.
        False: the last one was under CHECK_NOW_GAP ago, so this was an ordinary run."""
        now = now or time.time()
        if now - self.check_now_at < CHECK_NOW_GAP:
            await self.run_checks()
            return False
        self.check_now_at = now
        on = [m.id for m in MODULES.values() if m.enabled(m.load(self))]
        self.read_fresh(on)
        await self.run_checks()
        if self.last_error:
            return True
        changing = {r["check_key"].split(".", 1)[0] for r in self.db.all(
            "SELECT check_key FROM check_state WHERE site=? AND pending IS NOT NULL", (self.cfg.site,))}
        again = [mid for mid in on if mid in changing and hasattr(MODULES[mid], "stale")]
        if again:
            await asyncio.sleep(CHECK_NOW_CONFIRM)
            self.read_fresh(again)
            await self.run_checks(only=set(again))
        return True

    async def run_checks(self, now: float | None = None, only: set[str] | None = None) -> list[CheckResult]:
        """One round of checks. only: just these modules; the others keep their last results untouched
        (a quick re-read must not count as a second reading for modules whose data didn't change)."""
        now = now or time.time()
        events = []
        results: list[CheckResult] = []
        metrics_error = ""
        series: list[str] = []  # detailed metrics of the modules that took a new reading in this run
        gap = max(3 * int(timing(self.db)["interval"]), 600)
        for m in MODULES.values():
            if only is not None and m.id not in only:
                results += [r for r in self.results if r.key.startswith(m.id + ".")]
                continue
            s = m.load(self)
            if not m.enabled(s):
                events += self.engine.apply(m.id, [], now)  # a switched-off module closes its issues silently
                continue
            th = thresholds(self.db, self.cfg.site, m.id, m.checks)
            failure = ""
            try:
                mine = m.evaluate(s, th, await asyncio.wait_for(m.readings(self, s, now), MODULE_LIMIT))
            except MetricsError as e:  # our own store, not this module's device: keep what the module last showed
                metrics_error = str(e)
                results += [r for r in self.results if r.key.startswith(m.id + ".")]
                continue
            except TimeoutError:
                failure = f"no answer within {MODULE_LIMIT} s"
            except Exception as e:  # noqa: BLE001 - one module must not stop the others
                log.exception("module %s failed", m.id)
                failure = type(e).__name__
            if failure:
                first = m.checks[0]
                mine = [CheckResult(f"{m.id}.{first.id}", first.name, ERROR, f"Can't read {m.name}: {failure}", None,
                                    first.unit)] if th[first.id].enabled else []
                events += self.engine.apply(m.id, mine, now, partial=True)
                results += mine
                continue
            seen = m.observed(mine)
            if seen:
                self.engine.observe(m.id, now, gap)
                taken = m.reading_ts(self)
                for r in mine:
                    r.observed_at = taken
            events += self.engine.apply(m.id, mine, now)
            results += mine
            taken = m.reading_ts(self)
            if seen and taken and self.series_written.get(m.id) != taken:
                try:
                    series += m.series(self.cfg.site)
                except Exception:  # noqa: BLE001 - detailed metrics must never break the checks
                    log.exception("module %s: detailed metrics failed", m.id)
                self.series_written[m.id] = taken
            if seen and m.reading_ts(self) and self.db.one(
                    "SELECT 1 FROM check_state WHERE site=? AND check_key LIKE ? AND pending IS NOT NULL",
                    (self.cfg.site, f"{m.id}.%")):
                m.refresh_soon(self)
        self.last_error = metrics_error
        if metrics_error:
            ev = self.engine.read_failed(metrics_error)
        else:
            ev = self.engine.read_ok()
        if ev:
            events.append(ev)
        names = self.names()
        events += self.engine.released(names, now)
        events += self.engine.reminders(names, now)
        self.notifier.dispatch(events, self.engine.counts(), now)
        self.results, self.last_run = results, now
        self.write_grafana()
        self.grafana_on = await grafana.running(self.notifier.http, self.cfg.grafana_url)
        # Grafana shows the CONFIRMED level, as the Status page does: a single bad reading that the next one
        # takes back (a device rebooting) is not a problem there either
        confirmed = {r["check_key"]: r["status"] for r in self.db.all(
            "SELECT check_key, status FROM check_state WHERE site=?", (self.cfg.site,))}
        try:
            await self.metrics.write([
                line("hw_check_status", {"site": self.cfg.site, "module": r.key.split(".")[0], "check": r.key},
                     {"value": confirmed.get(r.key, 0)}, now) for r in results
            ] + [  # the measured values, for the Status page's 24 h lines
                line("hw_check_value", {"site": self.cfg.site, "module": r.key.split(".")[0], "check": r.key},
                     {"value": float(r.value)}, now) for r in results
                if isinstance(r.value, (int, float)) and not isinstance(r.value, bool) and math.isfinite(r.value)
            ] + [line("hw_run", {"site": self.cfg.site}, {"ok": 1, "duration_seconds": time.time() - now}, now)]
                + series)
        except MetricsError as e:
            log.warning("%s", e)
        if not metrics_error:  # a store that can't be read is what the heartbeat must not hide
            await self.heartbeat()
        return results

    def ha_meta(self) -> list[dict]:
        """The recorded Home Assistant entities as the dashboards name them; kept in the database, so a restart
        (or Home Assistant being away) doesn't empty the dashboards until the next reading."""
        saved = self.db.get("grafana.ha") or []
        m = MODULES.get("homeassistant")
        snap = getattr(m, "snap", None)
        if not snap:
            return saved
        before = {e["id"]: e for e in saved}
        meta = []
        for e in snap.get("recorded", []):
            # an entity unavailable right now has no numbers: it keeps the fields it had
            fields = sorted(set(e["values"]) | set(before.get(e["entity_id"], {}).get("fields", [])))
            if fields:
                meta.append({"id": e["entity_id"], "name": e["name"], "unit": e["unit"], "dc": e["dc"],
                             "domain": e["entity_id"].split(".", 1)[0], "fields": fields})
        meta.sort(key=lambda e: e["id"])
        if meta != saved:
            self.db.set("grafana.ha", meta)
        return meta

    def write_grafana(self) -> None:
        """Keep Grafana's dashboards in line with the enabled modules and the recorded Home Assistant entities
        (when the Grafana volume is there)."""
        enabled = frozenset(m.id for m in MODULES.values() if m.enabled(m.load(self)))
        ha = self.ha_meta() if "homeassistant" in enabled else []
        key = (enabled, json.dumps(ha, sort_keys=True))
        if key == self.grafana_for:
            return
        try:
            grafana.write(self.cfg.grafana_dir, set(enabled), ha)
            self.grafana_for = key
        except OSError as e:
            log.warning("Grafana provisioning: %s", e)

    async def heartbeat(self) -> None:
        await self.notifier.heartbeat()

    def migrate_heartbeat(self) -> None:
        """Before 0.1.0 the healthchecks.io URL was a setting; it is a provider (channel) now."""
        url = self.secrets.get(HC_SECRET)
        if not url:
            return
        if not self.db.one("SELECT 1 FROM channels WHERE kind='healthchecks'"):
            cid = self.db.run("INSERT INTO channels(kind,name,levels,config) VALUES('healthchecks','healthchecks.io','[]','{}')")
            store_private(self.secrets, cid, {"url": url})
        self.secrets.delete(HC_SECRET)

    def speedtest_background(self) -> bool:
        """Start a speed test without waiting for it; False when one is already running."""
        if self.speed_running:
            return False
        self.speed_running = True  # set now, so the page shows the running row right away
        self.speed_started = time.time()
        self.tasks = [t for t in self.tasks if not t.done()]
        self.tasks.append(asyncio.create_task(self.run_speedtest(already_marked=True)))
        return True

    async def speed_loop(self) -> None:
        await asyncio.sleep(60)
        while True:
            settings = self.internet_settings()
            s = settings["speed"]
            if settings["enabled"] and s["enabled"] and self.speed_due(float(s["interval_h"]), time.time()):
                try:
                    await self.run_speedtest()
                except Exception:  # noqa: BLE001
                    log.exception("speed test failed")
            await asyncio.sleep(60)

    def speed_due(self, interval_h: float, now: float) -> bool:
        last = self.db.one("SELECT ts, ok FROM speedtests WHERE site=? ORDER BY ts DESC LIMIT 1", (self.cfg.site,))
        if last is None:
            return True
        wait = interval_h * 3600 if last["ok"] else min(SPEED_RETRY, interval_h * 3600)
        return now - last["ts"] >= wait

    async def reset(self, everything: bool) -> str:
        """Delete history (issues, speed tests, metrics); with everything=True also all settings and users."""
        tables = HISTORY_TABLES + (CONFIG_TABLES if everything else ())
        with self.db.tx() as db:
            for t in tables:
                db.run(f"DELETE FROM {t}")  # noqa: S608 - fixed table names
        self.results, self.last_run, self.last_error = [], 0, ""
        warning = ""
        try:
            await self.metrics.delete_site(self.cfg.site)
        except MetricsError as e:
            warning = str(e)
        if everything:
            self.sync_telegraf()
        self.wake.set()
        return warning

    def guard_credentials(self, snap: dict):
        """Credentials stay only with the destination they were entered for: when a snapshot changes where one
        goes, or weakens how, switch that module or channel off in the snapshot and return what deletes the
        credential. Run that in the transaction that applies the snapshot, so a failure loses neither."""
        self.dropped, self.drop_why = [], []
        forget = []
        for m in MODULES.values():
            key = f"module.{m.id}"
            old, new = self.db.get(key), snap["settings"].get(key)
            why = versions.destination_change(m, old, new)
            if not why or not any(self.secrets.has(k) for k in m.secret_keys()):
                continue
            self.drop_why.append(f"{m.name}: {why}")
            forget.append(lambda m=m: m.forget_credentials(self))
            if new is not None:
                new["enabled"] = False
                self.dropped.append(m.name)
        wanted = {c["id"]: c for c in snap["channels"]}
        for row in self.db.all("SELECT * FROM channels"):
            cid = row["id"]
            c = wanted.get(cid)
            if c is not None and c["kind"] == row["kind"] and destination(c["kind"], json.loads(c["config"])) == \
                    destination(row["kind"], json.loads(row["config"])):
                continue
            had = self.secrets.has(channel_secret(cid)) or self.secrets.has(channel_private(cid))
            forget.append(lambda cid=cid: forget_channel(self.secrets, cid))
            if had:
                self.drop_why.append(f"{row['name']}: " + ("removed" if c is None else "its server or type changes"))
            if c is not None and had:
                c["enabled"] = 0
                self.dropped.append(c["name"])
        return lambda: [f() for f in forget]

    def preview(self, snap: dict) -> dict:
        """What applying this snapshot would do; changes nothing."""
        trial = copy.deepcopy(snap)
        self.guard_credentials(trial)
        current = versions.snapshot(self.db)
        versions.clean_import(current)  # the same normalizing, so that a default being filled in isn't a change
        return {"changes": versions.changes(current, trial), "cleared": list(self.drop_why)}

    def preview_import(self, text: str) -> tuple[dict | None, str]:
        snap, err = versions.from_yaml(text)
        err = err or versions.clean_import(snap)
        return (None, err) if err else (self.preview(snap), "")

    def preview_restore(self, vid: int) -> dict | None:
        snap = versions.load(self.db, vid)
        return self.preview(snap) if snap else None

    def restore_version(self, vid: int, username: str) -> bool:
        """Put an earlier configuration back and make every part of homeWatch use it."""
        if versions.restore(self.db, vid, before=self.guard_credentials) is None:
            return False
        self._config_replaced(username, "config.restore", f"version {vid}")
        return True

    def import_config(self, text: str, username: str) -> str:
        """Replace the configuration with an uploaded export; returns an error or ''."""
        snap, err = versions.from_yaml(text)
        err = err or versions.clean_import(snap)
        if err:
            return err
        versions.record(self.db, username, "config.before-import")  # so the import can be undone
        versions.apply(self.db, snap, self.guard_credentials(snap))
        self._config_replaced(username, "config.import", f"{len(snap['settings'])} settings, "
                              f"{len(snap['checks'])} thresholds, {len(snap['channels'])} channels")
        return ""

    def _config_replaced(self, username: str, action: str, detail: str) -> None:
        for m in MODULES.values():
            if hasattr(m, "snap"):
                m.snap, m.error = None, ""  # the next run reads with the new settings
            if hasattr(m, "client"):
                m.client = None
        self.sync_telegraf()
        if self.dropped:
            detail += "; credentials deleted, destination changed: " + ", ".join(self.dropped)
        audit(self.db, username, action, detail)
        self.wake.set()

    @property
    def exports_dir(self) -> str:
        return os.path.join(self.cfg.data_dir, "exports")

    def nightly_export(self, now: float) -> None:
        today = time.strftime("%Y-%m-%d", time.localtime(now))
        if self.db.get("export.last") != today:
            versions.write_export(self.db, self.exports_dir, today)
            self.db.set("export.last", today)

    async def export_grafana(self, now: float) -> None:
        """Once a day, a copy of Grafana's "My dashboards" next to the configuration exports."""
        today = time.strftime("%Y-%m-%d", time.localtime(now))
        if self.db.get("export.grafana_last") == today:
            return
        try:
            if await grafana.export_my(self.notifier.http, self.cfg.grafana_url, self.exports_dir, today):
                self.db.set("export.grafana_last", today)
        except Exception as e:  # noqa: BLE001 - Grafana is optional; try again at the next look
            log.warning("Grafana dashboards export: %s", e)

    async def run_speedtest(self, already_marked: bool = False) -> speedtest.Result:
        if self.speed_running and not already_marked:
            return speedtest.Result(False, error="a speed test is already running")
        self.speed_running = True
        if not already_marked:
            self.speed_started = time.time()
        try:
            s = self.internet_settings()["speed"]
            if s["engine"] == "ookla":
                if not s.get("ookla_accepted"):
                    res = speedtest.Result(False, error="Ookla license not accepted")
                else:
                    res = await speedtest.ookla(self.cfg.ookla_dir)
            else:
                res = await speedtest.cloudflare()
            now = time.time()
            self.db.run("INSERT INTO speedtests(site,ts,engine,down,up,latency,ok,error) VALUES(?,?,?,?,?,?,?,?)",
                        (self.cfg.site, now, s["engine"], res.down, res.up, res.latency, int(res.ok), res.error))
            if res.ok:
                try:
                    await self.metrics.write([line("hw_speedtest", {"site": self.cfg.site, "engine": s["engine"]},
                                                   {"download_mbps": res.down, "upload_mbps": res.up,
                                                    "latency_ms": res.latency}, now)])
                except MetricsError as e:
                    log.warning("%s", e)
            return res
        finally:
            self.speed_running = False

    async def outbox_loop(self) -> None:
        while True:
            try:
                await self.notifier.send_due()
            except Exception:  # noqa: BLE001
                log.exception("outbox failed")
            await asyncio.sleep(15)

    async def daily_loop(self) -> None:
        """Weekly report slot, the daily configuration export and update check, looked at every 5 minutes."""
        while True:
            now = time.time()
            try:
                self.nightly_export(now)
                await self.export_grafana(now)
                if report.due(self.db, now):
                    await self.send_report(now)
                if self.db.get("updates.check", True) and now - self.db.get("updates.checked", 0) > 86400:
                    await self.check_update(now)
            except Exception:  # noqa: BLE001
                log.exception("daily tasks failed")
            await asyncio.sleep(300)

    async def send_report(self, now: float | None = None) -> int:
        now = now or time.time()
        self.db.set("report.last_sent", now)
        chans = [c for c in self.notifier.channels() if c["weekly_report"]]
        if not chans:
            return 0
        msg = await report.build(self.db, self.metrics, self.cfg.site, now)
        for c in chans:
            self.notifier.queue(c["id"], msg, now)
        return len(chans)

    async def check_update(self, now: float) -> None:
        self.db.set("updates.checked", now)
        try:
            r = await self.notifier.http.get(RELEASES, timeout=10, headers={"Accept": "application/vnd.github+json"})
        except httpx.HTTPError:
            return
        if r.status_code == 200:
            rel = r.json()
            tag = rel.get("tag_name", "").lstrip("v")
            self.db.set("updates.latest", tag)
            self.db.set("updates.notes", str(rel.get("body") or "")[:20000])  # shown before updating
        else:
            self.db.set("updates.latest", "")

    def update_available(self) -> str:
        latest = self.db.get("updates.latest", "")
        if not latest or "dev" in __version__:
            return ""
        return latest if _ver(latest) > _ver(__version__) else ""


def _ver(v: str) -> tuple:
    out = []
    for p in v.split("-")[0].split("."):
        out.append(int(p) if p.isdigit() else 0)
    return tuple(out)
