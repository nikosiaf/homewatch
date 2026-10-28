"""Background loops: checks, speed tests, outbox, weekly report, heartbeat and update check."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field

import httpx

from . import __version__, report
from .config import Config
from .db import DB
from .engine import Engine, thresholds, timing
from .metrics import Metrics, MetricsError, line
from .modules import CheckResult
from .modules import internet as inet
from .modules.internet import checks as inet_checks
from .modules.internet import collect as inet_collect
from .modules.internet import speedtest, telegraf
from .notify import Notifier
from .secrets import Secrets

log = logging.getLogger("homewatch.runtime")

HC_SECRET = "healthchecks.url"
SPEED_RETRY = 15 * 60  # after a failed speed test, try again this soon instead of waiting a full interval
HISTORY_TABLES = ("issues", "speedtests", "check_state", "outbox", "snoozes")
CONFIG_TABLES = ("settings", "checks", "secrets", "maintenance", "channels", "sessions", "users", "audit")
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
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    tasks: list[asyncio.Task] = field(default_factory=list)

    @classmethod
    def create(cls, cfg: Config) -> Runtime:
        os.makedirs(cfg.data_dir, exist_ok=True)
        os.makedirs(cfg.ookla_dir, exist_ok=True)
        db = DB(cfg.db_path)
        secrets = Secrets(db, cfg.master_key)
        return cls(cfg, db, secrets, Metrics(cfg.vm_url), Notifier(db, secrets), Engine(db, cfg.site))

    # ---- settings helpers ----------------------------------------------------------------
    def internet_settings(self) -> dict:
        return inet.load(self.db, self.cfg.gateway_hint, self.cfg.dns_hint)

    def sync_telegraf(self) -> None:
        text = telegraf.render(self.internet_settings(), self.cfg.site)
        if telegraf.write(self.cfg.telegraf_dir, text):
            log.info("telegraf config written")

    def names(self) -> dict[str, str]:
        return {f"{inet_checks.MODULE}.{c.id}": c.name for c in inet_checks.CHECKS}

    # ---- loops ---------------------------------------------------------------------------
    def start(self) -> None:
        self.sync_telegraf()
        for coro in (self.check_loop(), self.speed_loop(), self.outbox_loop(), self.daily_loop()):
            self.tasks.append(asyncio.create_task(coro))

    async def stop(self) -> None:
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

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

    async def run_checks(self, now: float | None = None) -> list[CheckResult]:
        now = now or time.time()
        s = self.internet_settings()
        th = thresholds(self.db, self.cfg.site, inet_checks.MODULE, inet_checks.CHECKS)
        try:
            readings = await inet_collect.collect(self.metrics, self.db, self.cfg.site, now)
        except MetricsError as e:
            self.last_error = str(e)
            ev = self.engine.read_failed(str(e))
            if ev:
                self.notifier.dispatch([ev], self.engine.counts(), now)
            return self.results
        self.last_error = ""
        events = []
        ok_ev = self.engine.read_ok()
        if ok_ev:
            events.append(ok_ev)
        results = inet_checks.evaluate(s, th, readings)
        events += self.engine.apply(inet_checks.MODULE, results, now)
        names = self.names()
        events += self.engine.released(names, now)
        events += self.engine.reminders(names, now)
        self.notifier.dispatch(events, self.engine.counts(), now)
        self.results, self.last_run = results, now
        try:
            await self.metrics.write([
                line("hw_check_status", {"site": self.cfg.site, "module": r.key.split(".")[0], "check": r.key},
                     {"value": r.effective}, now) for r in results
            ] + [line("hw_run", {"site": self.cfg.site}, {"ok": 1, "duration_seconds": time.time() - now}, now)])
        except MetricsError as e:
            log.warning("%s", e)
        await self.heartbeat()
        return results

    async def heartbeat(self) -> None:
        url = self.secrets.get(HC_SECRET)
        if not url:
            return
        try:
            await self.notifier.http.get(url, timeout=10)
            self.db.set("healthchecks.last_ok", time.time())
        except httpx.HTTPError as e:
            log.warning("healthchecks ping failed: %s", type(e).__name__)

    async def speed_loop(self) -> None:
        await asyncio.sleep(60)
        while True:
            s = self.internet_settings()["speed"]
            if s["enabled"] and self.speed_due(float(s["interval_h"]), time.time()):
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

    async def run_speedtest(self) -> speedtest.Result:
        if self.speed_running:
            return speedtest.Result(False, error="a speed test is already running")
        self.speed_running = True
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
        """Weekly report slot and the once-a-day update check, looked at every 5 minutes."""
        while True:
            now = time.time()
            try:
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
            tag = r.json().get("tag_name", "").lstrip("v")
            self.db.set("updates.latest", tag)
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
