"""Web UI: server-rendered pages (Jinja + HTMX), login, CSRF, admin and viewer screens."""

from __future__ import annotations

import json
import logging
import os
import secrets as pysecrets
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from urllib.parse import urlencode
from datetime import datetime
from zoneinfo import ZoneInfo, available_timezones

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import __version__
from . import config as config_mod
from .auth import ROLES, Auth, AuthError, User, audit
from .engine import DEFAULT_TIMING, thresholds, timing, tz
from .modules import CATALOG, LEVEL_WORD, OK
from .modules.registry import MODULES
from .notify import COMING_SOON, KINDS, PROVIDERS, Message, channel_secret
from .runtime import Runtime

log = logging.getLogger("homewatch.web")
HERE = os.path.dirname(__file__)
COOKIE = "hw_session"
PRE = "hw_pre"
SETUP_STEPS = ["account", "home", "internet", "notify", "healthchecks"]
SNOOZE_CHOICES = {"1h": 3600, "4h": 4 * 3600, "24h": 24 * 3600}
RANGES = {"24h": (24 * 3600, 300), "7d": (7 * 86400, 1800), "30d": (30 * 86400, 7200)}
CHARTS = {
    "latency": ('min by (url) (avg_over_time(ping_average_response_ms{{site="{s}",role="public"}}[{w}]))', "ms"),
    "loss": ('avg_over_time(ping_percent_packet_loss{{site="{s}",role="public"}}[{w}])', "%"),
    "jitter": ('avg_over_time(ping_standard_deviation_ms{{site="{s}",role="public"}}[{w}])', "ms"),
    "speed": ('last_over_time(hw_speedtest_download_mbps{{site="{s}"}}[{w}]) or '
              'label_set(last_over_time(hw_speedtest_upload_mbps{{site="{s}"}}[{w}]), "url", "upload")', "Mbit/s"),
    "sync": ('label_set(last_over_time(hw_dsl_sync_down_kbps{{site="{s}"}}[{w}]) / 1000, "url", "download") or '
             'label_set(last_over_time(hw_dsl_sync_up_kbps{{site="{s}"}}[{w}]) / 1000, "url", "upload")', "Mbit/s"),
    "snr": ('label_set(last_over_time(hw_dsl_snr_down_db{{site="{s}"}}[{w}]), "url", "down") or '
            'label_set(last_over_time(hw_dsl_snr_up_db{{site="{s}"}}[{w}]), "url", "up")', "dB"),
}

templates = Jinja2Templates(directory=os.path.join(HERE, "templates"))


def _fmt_ts(ts, fmt="%d %b %H:%M"):
    if not ts:
        return ""
    return datetime.fromtimestamp(ts, templates.env.globals.get("zone") or ZoneInfo("UTC")).strftime(fmt)


def _fmt_dur(seconds):
    if seconds is None:
        return ""
    m = int(seconds // 60)
    if m < 60:
        return f"{m} min"
    h, m = divmod(m, 60)
    return f"{h} h {m} min" if h < 48 else f"{h // 24} d {h % 24} h"


templates.env.filters["ts"] = _fmt_ts
templates.env.filters["dur"] = _fmt_dur
templates.env.globals["LEVEL_WORD"] = LEVEL_WORD
templates.env.globals["version"] = __version__
templates.env.globals["CATALOG"] = CATALOG


def create_app(rt: Runtime | None = None, start_loops: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.rt = rt or Runtime.create(config_mod.load())
        app.state.auth = Auth(app.state.rt.db)
        if start_loops:
            app.state.rt.start()
        yield
        if start_loops:
            await app.state.rt.stop()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")
    _routes(app)
    return app


# ---- request helpers ------------------------------------------------------------------------

def rt_of(request: Request) -> Runtime:
    return request.app.state.rt


def current(request: Request) -> User | None:
    return request.app.state.auth.session(request.cookies.get(COOKIE))


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def page(request: Request, name: str, user: User | None, **ctx) -> HTMLResponse:
    rt = rt_of(request)
    if user and user.is_admin and not rt.db.get("home.url"):
        # the address the admin uses is the one to put in messages; editable under System > Home
        rt.db.set("home.url", f"{request.url.scheme}://{request.url.netloc}")
    templates.env.globals["zone"] = tz(rt.db)
    ctx.update(request=request, user=user, home=rt.db.get("home.name") or "Home",
               update=rt.update_available(), flash=request.query_params.get("msg", ""),
               box_id=request.query_params.get("box", ""),
               error=ctx.get("error") or request.query_params.get("err", ""))
    return templates.TemplateResponse(request, name, ctx)


# the card a form belongs to ("_box" field): its message is shown inside that card after the redirect
_box: ContextVar[str] = ContextVar("box", default="")


def back(url: str, msg: str = "", err: str = "") -> RedirectResponse:
    box = _box.get()
    q = urlencode({k: v for k, v in (("msg", msg), ("err", err), ("box", box)) if v})
    return RedirectResponse(url + ("?" + q if q else "") + (f"#{box}" if box else ""), status_code=303)


async def form_of(request: Request, user: User | None) -> dict | None:
    """Parse a POST form and check its CSRF token. Returns None when the token is wrong."""
    form = dict(await request.form())
    box = str(form.get("_box", ""))
    _box.set(box if box.replace("-", "").isalnum() else "")
    token = form.get("csrf") or request.headers.get("x-csrf-token", "")
    expected = user.csrf if user else request.cookies.get(PRE, "")
    if not expected or not pysecrets.compare_digest(str(token), expected):
        return None
    return form


def set_session(resp: Response, request: Request, token: str) -> None:
    secure = rt_of(request).cfg.secure_cookies
    resp.set_cookie(COOKIE, token, httponly=True, secure=secure, samesite="strict", max_age=7 * 86400)


def pre_value(request: Request) -> str:
    """CSRF token for forms shown before login (double-submit cookie)."""
    return request.cookies.get(PRE) or pysecrets.token_urlsafe(24)


def with_pre(request: Request, resp: Response, tok: str) -> Response:
    resp.set_cookie(PRE, tok, httponly=True, secure=rt_of(request).cfg.secure_cookies, samesite="strict")
    return resp


def _float(v, default=None):
    try:
        return float(v) if v not in (None, "") else default
    except ValueError:
        return default


# ---- routes ---------------------------------------------------------------------------------

def _routes(app: FastAPI) -> None:  # noqa: C901 - one place for all pages keeps them easy to find

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        if path.startswith("/static") or path in ("/healthz",):
            return await call_next(request)
        auth: Auth = request.app.state.auth
        if not auth.has_users():
            if path != "/setup":
                return RedirectResponse("/setup", status_code=303)
            return await call_next(request)
        if path in ("/login",):
            return await call_next(request)
        user = current(request)
        if user is None:
            return RedirectResponse("/login", status_code=303)
        request.state.user = user
        viewer_ok = path in ("/", "/history", "/partials/status", "/api/series", "/logout", "/password")
        if not user.is_admin and not viewer_ok:
            return HTMLResponse("Admins only", status_code=403)
        resp = await call_next(request)
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/healthz")
    async def healthz(request: Request):
        rt = rt_of(request)
        return JSONResponse({"ok": True, "version": __version__, "last_run": rt.last_run})

    # -- setup wizard ------------------------------------------------------------------------
    @app.get("/setup")
    async def setup_start(request: Request):
        auth: Auth = request.app.state.auth
        if auth.has_users():
            return RedirectResponse("/setup/home", status_code=303)
        tok = pre_value(request)
        return with_pre(request, page(request, "setup_account.html", None, step=1, steps=SETUP_STEPS, csrf=tok), tok)

    @app.post("/setup")
    async def setup_account(request: Request):
        auth: Auth = request.app.state.auth
        if auth.has_users():
            return RedirectResponse("/login", status_code=303)
        form = await form_of(request, None)
        if form is None:
            return back("/setup", err="Form expired, please try again")
        if form.get("password") != form.get("password2"):
            return back("/setup", err="The passwords don't match")
        try:
            auth.create_user(form.get("username", ""), form.get("password", ""), "admin")
            token = auth.login(form["username"], form["password"], client_ip(request))
        except AuthError as e:
            return back("/setup", err=str(e))
        audit(rt_of(request).db, form["username"], "setup", "admin account created")
        resp = RedirectResponse("/setup/home", status_code=303)
        set_session(resp, request, token)
        return resp

    @app.get("/setup/{step}")
    async def setup_step(request: Request, step: str):
        if step not in SETUP_STEPS[1:]:
            return RedirectResponse("/", status_code=303)
        rt = rt_of(request)
        ctx = dict(step=SETUP_STEPS.index(step) + 1, steps=SETUP_STEPS, csrf=request.state.user.csrf,
                   s=rt.internet_settings(), zones=sorted(available_timezones()),
                   tzname=rt.db.get("home.tz") or os.environ.get("TZ", "UTC"),
                   providers=PROVIDERS, coming=COMING_SOON, skip_heartbeat=step == "notify",
                   hc=rt.db.one("SELECT 1 FROM channels WHERE kind='healthchecks'") is not None)
        return page(request, f"setup_{step}.html", request.state.user, **ctx)

    @app.post("/setup/{step}")
    async def setup_save(request: Request, step: str):
        user = request.state.user
        form = await form_of(request, user)
        if form is None:
            return back(f"/setup/{step}", err="Form expired, please try again")
        nxt = SETUP_STEPS[SETUP_STEPS.index(step) + 1] if step != SETUP_STEPS[-1] else None
        if form.get("skip") is None:
            err = await _ookla_ready(rt_of(request), form, user) if (
                step == "internet" and form.get("speed.engine") == "ookla") else ""
            err = err or _save_section(request, step, form, user)
            if err:
                return back(f"/setup/{step}", err=err)
        if nxt:
            return RedirectResponse(f"/setup/{nxt}", status_code=303)
        rt_of(request).db.set("setup.done", True)
        rt_of(request).wake.set()
        return back("/", msg="Setup done. The first data appears within about 2 minutes.")

    # -- login --------------------------------------------------------------------------------
    @app.get("/login")
    async def login_page(request: Request):
        tok = pre_value(request)
        return with_pre(request, page(request, "login.html", None, csrf=tok), tok)

    @app.post("/login")
    async def login(request: Request):
        form = await form_of(request, None)
        if form is None:
            return back("/login", err="Form expired, please try again")
        auth: Auth = request.app.state.auth
        try:
            token = auth.login(form.get("username", ""), form.get("password", ""), client_ip(request))
        except AuthError as e:
            return back("/login", err=str(e))
        resp = RedirectResponse("/", status_code=303)
        set_session(resp, request, token)
        return resp

    @app.post("/logout")
    async def logout(request: Request):
        if await form_of(request, request.state.user) is None:
            return back("/")
        request.app.state.auth.logout(request.cookies.get(COOKIE, ""))
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(COOKIE)
        return resp

    # -- status and history (viewers too) ----------------------------------------------------
    @app.get("/")
    async def status(request: Request):
        return page(request, "status.html", request.state.user, **_status_ctx(request))

    @app.get("/partials/status")
    async def status_partial(request: Request):
        return page(request, "_status_body.html", request.state.user, **_status_ctx(request))

    @app.get("/api/series")
    async def series(request: Request, chart: str = "latency", range: str = "24h"):  # noqa: A002
        rt = rt_of(request)
        if chart not in CHARTS or range not in RANGES:
            return JSONResponse({"error": "unknown chart"}, status_code=400)
        span, step = RANGES[range]
        q, unit = CHARTS[chart]
        expr = q.format(s=rt.cfg.site, w=f"{step}s")
        now = time.time()
        try:
            data = await rt.metrics.query_range(expr, now - span, now, step)
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"error": str(e)[:200]}, status_code=502)
        stamps = sorted({t for _, vals in data for t, _ in vals})
        idx = {t: i for i, t in enumerate(stamps)}
        series_out = []
        for labels, vals in data:
            col = [None] * len(stamps)
            for t, v in vals:
                col[idx[t]] = round(v, 2)
            name = labels.get("url") or ("download" if chart == "speed" else "value")
            series_out.append({"name": name, "data": col})
        return JSONResponse({"unit": unit, "x": stamps, "series": series_out})

    @app.get("/history")
    async def history(request: Request, check: str = "", level: str = "", days: int = 30):
        rt = rt_of(request)
        days = max(1, min(days, 400))
        since = time.time() - days * 86400
        q = "SELECT * FROM issues WHERE site=? AND started>?"
        args: list = [rt.cfg.site, since]
        if check:
            q += " AND check_key=?"
            args.append(check)
        if level in ("1", "2"):
            q += " AND worst=?"
            args.append(int(level))
        rows = rt.db.all(q + " ORDER BY started DESC LIMIT 500", tuple(args))
        from .report import downtime

        down, outages = downtime(rt.db, rt.cfg.site, "internet.reachable", since, time.time())
        uptime = 100 * (1 - down / (days * 86400))
        return page(request, "history.html", request.state.user, rows=rows, names=rt.names(), check=check,
                    level=level, days=days, uptime=uptime, outages=outages, now=time.time())

    # -- internet module ------------------------------------------------------------------------
    @app.get("/internet")
    async def internet_old(request: Request):
        return RedirectResponse("/modules/internet", status_code=308)

    @app.get("/modules")
    async def modules_page(request: Request):
        return page(request, "modules.html", request.state.user)

    @app.get("/modules/{mid}")
    async def module_page(request: Request, mid: str):
        rt = rt_of(request)
        m = MODULES.get(mid)
        if m is None:
            return RedirectResponse("/modules", status_code=303)
        return page(request, f"module_{mid}.html", request.state.user, s=m.load(rt), csrf=request.state.user.csrf,
                    module=m, **m.page_ctx(rt))

    @app.post("/modules/{mid}")
    async def module_save(request: Request, mid: str):
        rt = rt_of(request)
        m = MODULES.get(mid)
        if m is None:
            return RedirectResponse("/modules", status_code=303)
        form = await form_of(request, request.state.user)
        if form is None:
            return back(f"/modules/{mid}", err="Form expired, please try again")
        if mid == "internet" and form.get("speed.engine") == "ookla":
            err = await _ookla_ready(rt, form, request.state.user)
            if err:
                return back(f"/modules/{mid}", err=err)
        err = m.save(rt, form, request.state.user)
        return back(f"/modules/{mid}", err=err) if err else back(f"/modules/{mid}", msg="Saved")

    @app.post("/modules/speedport/read")
    async def speedport_read(request: Request):
        rt = rt_of(request)
        m = MODULES["speedport"]
        if await form_of(request, request.state.user) is None:
            note, bad = "Form expired, reload the page", True
        else:
            err = await m.read_now(rt)
            rt.wake.set()
            note, bad = (f"Reading the modem failed: {err}", True) if err else ("Modem read successfully", False)
        if request.headers.get("hx-request"):
            return page(request, "_speedport_reading.html", request.state.user, result=note, result_err=bad,
                        csrf=request.state.user.csrf, **m.page_ctx(rt))
        return back("/modules/speedport", err=note) if bad else back("/modules/speedport", msg=note)

    def speedtests_partial(request: Request, note: str = "", note_err: bool = False):
        rt = rt_of(request)
        m = MODULES["internet"]
        return page(request, "_speedtests.html", request.state.user, s=m.load(rt), csrf=request.state.user.csrf,
                    started=rt.speed_started, result=note, result_err=note_err, **m.page_ctx(rt))

    @app.get("/modules/internet/speedtests")
    async def speedtests(request: Request):
        rt = rt_of(request)
        note, bad = "", False
        if not rt.speed_running:  # just finished: say how it went
            last = rt.db.one("SELECT * FROM speedtests WHERE site=? ORDER BY ts DESC LIMIT 1", (rt.cfg.site,))
            if last and last["ts"] >= rt.speed_started - 1:
                note, bad = ((f"Speed test: {last['down']:.1f} / {last['up']:.1f} Mbit/s", False) if last["ok"]
                             else (f"Speed test failed: {last['error']}", True))
        return speedtests_partial(request, note, bad)

    @app.post("/modules/internet/speedtest")
    async def speedtest_now(request: Request):
        rt = rt_of(request)
        if await form_of(request, request.state.user) is None:
            return back("/modules/internet", err="Form expired")
        started = rt.speedtest_background()
        if started:
            audit(rt.db, request.state.user.username, "speedtest", "run now")
        if request.headers.get("hx-request"):
            return speedtests_partial(request)
        return back("/modules/internet", msg="Speed test started; reload in about 20 seconds")

    # -- checks, thresholds, snooze -------------------------------------------------------------
    @app.get("/checks")
    async def checks_page(request: Request):
        rt = rt_of(request)
        groups = [(m, thresholds(rt.db, rt.cfg.site, m.id, m.checks)) for m in MODULES.values()
                  if m.enabled(m.load(rt))]
        current_vals = {r.key: r for r in rt.results}
        snoozes = {r["check_key"]: r for r in rt.db.all(
            "SELECT * FROM snoozes WHERE site=? AND until>? ORDER BY until", (rt.cfg.site, time.time()))}
        return page(request, "checks.html", request.state.user, groups=groups, current=current_vals, snoozes=snoozes,
                    csrf=request.state.user.csrf, timing=timing(rt.db), default_timing=DEFAULT_TIMING)

    @app.post("/checks")
    async def checks_save(request: Request):
        rt = rt_of(request)
        user = request.state.user
        form = await form_of(request, user)
        if form is None:
            return back("/checks", err="Form expired")
        changes = []
        for m in MODULES.values():
            if f"{m.id}.present" not in form:
                continue  # a module not shown on the page (switched off) keeps its thresholds
            old = thresholds(rt.db, rt.cfg.site, m.id, m.checks)
            for d in m.checks:
                key = f"{m.id}.{d.id}"
                enabled = form.get(f"{key}.enabled") == "on"
                warn = _float(form.get(f"{key}.warn")) if "warn" in d.editable else d.warn
                error = _float(form.get(f"{key}.error")) if "error" in d.editable else d.error
                if warn is not None and error is not None and (error > warn if d.lower_is_worse else warn > error):
                    return back("/checks", err=f"{d.name}: the WARNING level must come before the ERROR level")
                o = old[d.id]
                if (o.warn, o.error, o.enabled) != (warn, error, enabled):
                    changes.append(f"{key}: warn {o.warn}→{warn}, error {o.error}→{error}, "
                                   f"enabled {o.enabled}→{enabled}")
                rt.db.run("INSERT INTO checks(site,check_key,enabled,warn,error) VALUES(?,?,?,?,?) "
                          "ON CONFLICT(site,check_key) DO UPDATE SET enabled=excluded.enabled, "
                          "warn=excluded.warn, error=excluded.error", (rt.cfg.site, key, int(enabled), warn, error))
        t = {}
        for k, lo, hi in (("interval", 30, 3600), ("confirm", 1, 10), ("remind_h", 0, 168), ("fail_alert", 1, 100)):
            v = _float(form.get(f"timing.{k}"))
            if v is None or not lo <= v <= hi:
                return back("/checks", err=f"{k} must be between {lo} and {hi}")
            t[k] = v
        if t != timing(rt.db):
            changes.append(f"timing {timing(rt.db)} → {t}")
            rt.db.set("engine.timing", t)
        if changes:
            audit(rt.db, user.username, "checks", "; ".join(changes))
        rt.wake.set()
        return back("/checks", msg="Saved")

    @app.post("/checks/run")
    async def checks_run(request: Request):
        rt = rt_of(request)
        if await form_of(request, request.state.user) is None:
            return back("/", err="Form expired")
        await rt.run_checks()
        if rt.last_error:
            return back("/", err=f"Check run failed: {rt.last_error}")
        return back("/", msg=f"Checks ran at {_fmt_ts(rt.last_run, '%H:%M:%S')}. A change still needs "
                             f"{int(timing(rt.db)['confirm'])} runs in a row before it is sent.")

    @app.post("/checks/snooze")
    async def snooze(request: Request):
        rt = rt_of(request)
        user = request.state.user
        form = await form_of(request, user)
        if form is None:
            return back("/checks", err="Form expired")
        key = form.get("check", "")
        if key not in rt.names():
            return back("/checks", err="Unknown check")
        choice = form.get("for", "")
        now = time.time()
        if choice in SNOOZE_CHOICES:
            until = now + SNOOZE_CHOICES[choice]
        else:
            try:
                until = datetime.fromisoformat(form.get("until", "")).replace(tzinfo=tz(rt.db)).timestamp()
            except ValueError:
                return back("/checks", err="Pick a duration or a valid time")
        if until <= now:
            return back("/checks", err="That time is in the past")
        reason = form.get("reason", "")[:200]
        rt.db.run("INSERT INTO snoozes(site,check_key,until,reason,created_by,created) VALUES(?,?,?,?,?,?)",
                  (rt.cfg.site, key, until, reason, user.username, now))
        audit(rt.db, user.username, "snooze", f"{key} until {_fmt_ts(until)} ({reason})")
        return back("/checks", msg="Snoozed")

    @app.post("/checks/unsnooze")
    async def unsnooze(request: Request):
        rt = rt_of(request)
        user = request.state.user
        form = await form_of(request, user)
        if form is None:
            return back("/checks", err="Form expired")
        rt.db.run("UPDATE snoozes SET until=? WHERE site=? AND check_key=? AND until>?",
                  (time.time(), rt.cfg.site, form.get("check", ""), time.time()))
        audit(rt.db, user.username, "unsnooze", form.get("check", ""))
        rt.wake.set()
        return back("/checks", msg="Snooze ended")

    # -- maintenance -----------------------------------------------------------------------------
    @app.get("/maintenance")
    async def maintenance_page(request: Request):
        rt = rt_of(request)
        rows = rt.db.all("SELECT * FROM maintenance WHERE site=? ORDER BY id DESC", (rt.cfg.site,))
        return page(request, "maintenance.html", request.state.user, rows=rows, names=rt.names(),
                    csrf=request.state.user.csrf, now=time.time(), json=json)

    @app.post("/maintenance")
    async def maintenance_add(request: Request):
        rt = rt_of(request)
        user = request.state.user
        form = await form_of(request, user)
        if form is None:
            return back("/maintenance", err="Form expired")
        name = form.get("name", "").strip()[:100] or "Maintenance"
        keys = [k for k in rt.names() if form.get(f"check.{k}") == "on"]
        checks_json = json.dumps(keys) if keys and form.get("scope") == "some" else None
        zone = tz(rt.db)
        if form.get("kind") == "weekly":
            try:
                h, m = map(int, form.get("start_time", "").split(":"))
                weekday, dur = int(form.get("weekday", "")), int(form.get("dur_min", ""))
            except ValueError:
                return back("/maintenance", err="Weekly window: pick a day, a start time and a duration")
            if not (0 <= weekday <= 6 and 1 <= dur <= 7 * 1440):
                return back("/maintenance", err="Weekly window: invalid day or duration")
            rt.db.run("INSERT INTO maintenance(site,name,kind,weekday,start_min,dur_min,checks,created_by,created) "
                      "VALUES(?,?,?,?,?,?,?,?,?)", (rt.cfg.site, name, "weekly", weekday, h * 60 + m, dur,
                                                    checks_json, user.username, time.time()))
        else:
            try:
                start = datetime.fromisoformat(form.get("start", "")).replace(tzinfo=zone).timestamp()
                end = datetime.fromisoformat(form.get("end", "")).replace(tzinfo=zone).timestamp()
            except ValueError:
                return back("/maintenance", err="One-off window: give a start and an end")
            if end <= start:
                return back("/maintenance", err="The end must be after the start")
            rt.db.run("INSERT INTO maintenance(site,name,kind,start_ts,end_ts,checks,created_by,created) "
                      "VALUES(?,?,?,?,?,?,?,?)", (rt.cfg.site, name, "once", start, end, checks_json,
                                                  user.username, time.time()))
        audit(rt.db, user.username, "maintenance.add", name)
        return back("/maintenance", msg="Window added")

    @app.post("/maintenance/{mid}/delete")
    async def maintenance_delete(request: Request, mid: int):
        rt = rt_of(request)
        user = request.state.user
        if await form_of(request, user) is None:
            return back("/maintenance", err="Form expired")
        row = rt.db.one("SELECT name FROM maintenance WHERE id=?", (mid,))
        rt.db.run("DELETE FROM maintenance WHERE id=?", (mid,))
        audit(rt.db, user.username, "maintenance.delete", row["name"] if row else str(mid))
        rt.wake.set()
        return back("/maintenance", msg="Window removed")

    # -- notifications -------------------------------------------------------------------------
    @app.get("/notifications")
    async def notifications_page(request: Request):
        rt = rt_of(request)
        chans = []
        for c in rt.notifier.channels(enabled_only=False, messages_only=False):
            config = json.loads(c["config"])
            if c["kind"] == "ntfy" and not config.get("url"):
                config = KINDS["ntfy"].normalize(config)[0]
            chans.append(dict(c) | {"config": config, "levels": json.loads(c["levels"]),
                                    "has_secret": rt.secrets.has(channel_secret(c["id"])),
                                    "last_ping": rt.db.get(f"heartbeat.{c['id']}.last_ok", 0)})
        queued = rt.db.all("SELECT channel_id, COUNT(*) n, MAX(last_error) e FROM outbox GROUP BY channel_id")
        return page(request, "notifications.html", request.state.user, chans=chans, providers=PROVIDERS,
                    coming=COMING_SOON, csrf=request.state.user.csrf,
                    schedule=rt.db.get("report.schedule") or {"weekday": 0, "time": "08:00"},
                    queued={q["channel_id"]: q for q in queued})

    @app.post("/notifications/add")
    async def channel_add(request: Request):
        form = await form_of(request, request.state.user)
        if form is None:
            return back("/notifications", err="Form expired")
        err = _save_section(request, "notify", form, request.state.user)
        return back("/notifications", err=err) if err else back("/notifications", msg="Added")

    @app.post("/notifications/report")
    async def report_save(request: Request):
        rt = rt_of(request)
        user = request.state.user
        form = await form_of(request, user)
        if form is None:
            return back("/notifications", err="Form expired")
        try:
            h, m = map(int, form.get("report_time", "08:00").split(":"))
            wd = int(form.get("report_weekday", "0"))
            assert 0 <= h < 24 and 0 <= m < 60 and 0 <= wd <= 6
        except (ValueError, AssertionError):
            return back("/notifications", err="Weekly report: invalid day or time")
        rt.db.set("report.schedule", {"weekday": wd, "time": f"{h:02d}:{m:02d}"})
        for c in rt.notifier.channels():  # disabled channels can't be ticked; keep their choice for later
            rt.db.run("UPDATE channels SET weekly_report=? WHERE id=?", (int(form.get(f"report.{c['id']}") == "on"),
                                                                         c["id"]))
        audit(rt.db, user.username, "report", f"weekday={wd} time={h:02d}:{m:02d}")
        if form.get("send_now"):
            n = await rt.send_report()
            if not n:
                return back("/notifications", err="No enabled channel is ticked for the weekly report")
            return back("/notifications", msg=f"Weekly report sent to {n} channel(s)")
        return back("/notifications", msg="Weekly report saved")

    @app.post("/notifications/{cid}")
    async def channel_edit(request: Request, cid: int):
        rt = rt_of(request)
        user = request.state.user
        form = await form_of(request, user)
        if form is None:
            return back("/notifications", err="Form expired")
        ch = rt.db.one("SELECT * FROM channels WHERE id=?", (cid,))
        if ch is None or ch["kind"] not in PROVIDERS:
            return back("/notifications", err="Unknown channel")
        mod = PROVIDERS[ch["kind"]]
        config = {k: form.get(k, "").strip() for k, _, _ in mod.FIELDS}
        if hasattr(mod, "normalize"):
            config, err = mod.normalize(config)
            if err:
                return back("/notifications", err=err)
        levels = [lv for lv in (1, 2) if form.get(f"level{lv}") == "on"]
        qs, qe = form.get("quiet_start", ""), form.get("quiet_end", "")
        if bool(qs) != bool(qe):
            return back("/notifications", err="Quiet hours need both a start and an end")
        rt.db.run("UPDATE channels SET name=?, enabled=?, levels=?, quiet_start=?, quiet_end=?, config=? WHERE id=?",
                  (form.get("name", ch["name"])[:60] or ch["name"], int(form.get("enabled") == "on"),
                   json.dumps(levels), qs or None, qe or None, json.dumps(config), cid))
        if form.get("secret"):
            rt.secrets.put(channel_secret(cid), form["secret"].strip())
        audit(rt.db, user.username, "channel.edit", f"{ch['kind']} {ch['name']}")
        return back("/notifications", msg="Saved")

    @app.post("/notifications/{cid}/delete")
    async def channel_delete(request: Request, cid: int):
        rt = rt_of(request)
        user = request.state.user
        if await form_of(request, user) is None:
            return back("/notifications", err="Form expired")
        rt.db.run("DELETE FROM channels WHERE id=?", (cid,))
        rt.db.run("DELETE FROM outbox WHERE channel_id=?", (cid,))
        rt.secrets.delete(channel_secret(cid))
        audit(rt.db, user.username, "channel.delete", str(cid))
        return back("/notifications", msg="Removed")

    @app.post("/notifications/{cid}/test")
    async def channel_test(request: Request, cid: int):
        rt = rt_of(request)
        if await form_of(request, request.state.user) is None:
            return back("/notifications", err="Form expired")
        ch = rt.db.one("SELECT * FROM channels WHERE id=?", (cid,))
        if ch is None:
            return back("/notifications", err="Unknown channel")
        try:
            if ch["kind"] == "healthchecks":
                await PROVIDERS["healthchecks"].ping(rt.notifier.http, json.loads(ch["config"]))
                rt.db.set(f"heartbeat.{cid}.last_ok", time.time())
                return back("/notifications", msg=f"{ch['name']}: ping sent; healthchecks.io should show it as up")
            home = rt.db.get("home.name") or "Home"
            msg = Message(OK, f"✅ {home}: test message",
                          f"homewatch can reach this channel.\n{rt.db.get('home.url') or ''}")
            await rt.notifier.deliver(ch, msg)
        except Exception as e:  # noqa: BLE001
            return back("/notifications", err=f"{ch['name']}: {e}")
        return back("/notifications", msg=f"{ch['name']}: test message sent")

    # -- system ------------------------------------------------------------------------------------
    @app.get("/system")
    async def system_page(request: Request):
        rt = rt_of(request)
        users = rt.db.all("SELECT id, username, role, created FROM users ORDER BY username")
        log_rows = rt.db.all("SELECT * FROM audit ORDER BY id DESC LIMIT 100")
        vm_ok = await rt.metrics.healthy()
        return page(request, "system.html", request.state.user, users=users, log=log_rows, vm_ok=vm_ok,
                    last_run=rt.last_run, last_error=rt.last_error, csrf=request.state.user.csrf,
                    updates=rt.db.get("updates.check", True), latest=rt.db.get("updates.latest", ""),
                    roles=ROLES, tzname=rt.db.get("home.tz") or "UTC", zones=sorted(available_timezones()),
                    home_name=rt.db.get("home.name") or "Home", home_url=rt.db.get("home.url") or "")

    @app.post("/system/home")
    async def system_home(request: Request):
        form = await form_of(request, request.state.user)
        if form is None:
            return back("/system", err="Form expired")
        err = _save_section(request, "home", form, request.state.user)
        return back("/system", err=err) if err else back("/system", msg="Saved")

    @app.post("/users")
    async def user_add(request: Request):
        rt = rt_of(request)
        user = request.state.user
        form = await form_of(request, user)
        if form is None:
            return back("/system", err="Form expired")
        try:
            request.app.state.auth.create_user(form.get("username", ""), form.get("password", ""),
                                               form.get("role", "viewer"))
        except AuthError as e:
            return back("/system", err=str(e))
        audit(rt.db, user.username, "user.add", f"{form.get('username')} ({form.get('role')})")
        return back("/system", msg="User added")

    @app.post("/users/{uid}/delete")
    async def user_delete(request: Request, uid: int):
        rt = rt_of(request)
        user = request.state.user
        if await form_of(request, user) is None:
            return back("/system", err="Form expired")
        if uid == user.id:
            return back("/system", err="You can't remove yourself")
        row = rt.db.one("SELECT username, role FROM users WHERE id=?", (uid,))
        if row and row["role"] == "admin" and rt.db.one("SELECT COUNT(*) n FROM users WHERE role='admin'")["n"] <= 1:
            return back("/system", err="Keep at least one admin")
        rt.db.run("DELETE FROM users WHERE id=?", (uid,))
        audit(rt.db, user.username, "user.delete", row["username"] if row else str(uid))
        return back("/system", msg="User removed")

    @app.get("/password")
    async def password_page(request: Request):
        return page(request, "password.html", request.state.user, csrf=request.state.user.csrf)

    @app.post("/password")
    async def password_change(request: Request):
        rt = rt_of(request)
        user = request.state.user
        form = await form_of(request, user)
        if form is None:
            return back("/password", err="Form expired")
        auth: Auth = request.app.state.auth
        try:
            auth.login(user.username, form.get("current", ""), client_ip(request))
        except AuthError:
            return back("/password", err="The current password is wrong")
        if form.get("new") != form.get("new2"):
            return back("/password", err="The new passwords don't match")
        try:
            auth.set_password(user.id, form.get("new", ""))
        except AuthError as e:
            return back("/password", err=str(e))
        audit(rt.db, user.username, "password", "changed")
        resp = back("/login", msg="Password changed, please log in again")
        resp.delete_cookie(COOKIE)
        return resp

    @app.post("/system/reset")
    async def system_reset(request: Request):
        rt = rt_of(request)
        user = request.state.user
        form = await form_of(request, user)
        if form is None:
            return back("/system", err="Form expired")
        everything = form.get("mode") == "all"
        word = "RESET" if everything else "DELETE"
        if form.get("confirm", "").strip() != word:
            return back("/system", err=f"Type {word} in the box to confirm")
        warning = await rt.reset(everything)
        if everything:
            log.warning("all configuration and history reset by %s", user.username)
            resp = RedirectResponse("/setup", status_code=303)
            resp.delete_cookie(COOKIE)
            return resp
        audit(rt.db, user.username, "history.delete", "issues, speed tests and metrics deleted")
        if warning:
            return back("/system", err=f"History deleted, but metrics could not be: {warning}")
        return back("/system", msg="History and metrics deleted")

    @app.post("/system/updates")
    async def updates_toggle(request: Request):
        rt = rt_of(request)
        form = await form_of(request, request.state.user)
        if form is None:
            return back("/system", err="Form expired")
        rt.db.set("updates.check", form.get("check") == "on")
        audit(rt.db, request.state.user.username, "updates", f"check={form.get('check') == 'on'}")
        return back("/system", msg="Saved")


async def _ookla_ready(rt, form: dict, user) -> str:
    """Choosing the Ookla engine needs its license accepted and the CLI installed; install it on first save."""
    from .modules.internet.speedtest import OOKLA_VERSION, install_ookla, ookla_path

    if form.get("speed.ookla_accepted") != "on":
        return "To use the Ookla engine, accept Ookla's EULA and privacy policy"
    if os.access(ookla_path(rt.cfg.ookla_dir), os.X_OK):
        return ""
    err = await install_ookla(rt.cfg.ookla_dir)
    if err:
        return f"Nothing saved: {err}"
    audit(rt.db, user.username, "ookla", f"installed {OOKLA_VERSION}")
    return ""


def _status_ctx(request: Request) -> dict:
    rt = rt_of(request)
    now = time.time()
    states = {r["check_key"]: r for r in rt.db.all("SELECT * FROM check_state WHERE site=?", (rt.cfg.site,))}
    tiles = []
    for r in rt.results:
        st_row = states.get(r.key)
        tiles.append({"r": r, "status": st_row["status"] if st_row else OK,
                      "pending": st_row["pending"] if st_row else None,
                      "silenced": rt.engine.silenced(r.key, now)})
    overall = max((t["status"] for t in tiles), default=OK)
    groups = []
    for m in MODULES.values():
        mine = [t for t in tiles if t["r"].key.startswith(m.id + ".")]
        if mine:
            groups.append((m.id, m.name, mine))
    errors, warnings = rt.engine.counts()
    sp = MODULES["speedport"]
    return dict(tiles=tiles, tile_groups=groups, dsl=sp.enabled(sp.load(rt)), overall=overall, open=rt.engine.open_issues(), names=rt.names(), now=now,
                last_run=rt.last_run, last_error=rt.last_error, errors=errors, warnings=warnings,
                setup_done=rt.db.get("setup.done", False))


def _save_section(request: Request, section: str, form: dict, user: User) -> str:
    """Shared by the wizard and the settings pages. Returns an error message or ''."""
    rt = rt_of(request)
    if section == "home":
        name = form.get("name", "").strip()[:60]
        zone = form.get("tz", "UTC")
        if zone not in available_timezones():
            return "Unknown time zone"
        if "url" in form:
            url = form.get("url", "").strip().rstrip("/")
            if url and not url.startswith(("https://", "http://")):
                return "homewatch address: start it with https://"
            rt.db.set("home.url", url)
        rt.db.set("home.name", name or "Home")
        rt.db.set("home.tz", zone)
        audit(rt.db, user.username, "home", f"name={name} tz={zone}")
        return ""
    if section == "internet":
        return MODULES["internet"].save(rt, form, user)
    if section == "healthchecks":  # wizard step: the heartbeat is a provider like the others
        if not form.get("healthchecks.url", "").strip():
            return ""
        form = {**form, "kind": "healthchecks"}
        section = "notify"
    if section == "notify":
        kind = form.get("kind", "")
        if kind not in PROVIDERS:
            return "Pick a provider"
        mod = PROVIDERS[kind]
        # fields are named "<kind>.<field>" because every provider's fields are on the page at once
        config = {k: form.get(f"{kind}.{k}", "").strip() for k, _, _ in mod.FIELDS}
        secret = form.get(f"{kind}.secret", "").strip()
        if kind == "telegram" and not (config["chat_id"] and secret):
            return "Telegram needs a bot token and a chat ID"
        if hasattr(mod, "normalize"):
            config, err = mod.normalize(config)
            if err:
                return err
        name = form.get(f"{kind}.name", "").strip()[:60] or mod.TITLE
        levels = [] if kind == "healthchecks" else [1, 2]
        cid = rt.db.run("INSERT INTO channels(kind,name,levels,config) VALUES(?,?,?,?)",
                        (kind, name, json.dumps(levels), json.dumps(config)))
        if secret:
            rt.secrets.put(channel_secret(cid), secret)
        audit(rt.db, user.username, "channel.add", f"{kind} {name}")
        return ""
    return "Unknown section"
