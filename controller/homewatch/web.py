"""Web UI: server-rendered pages (Jinja + HTMX), login, CSRF, admin and viewer screens."""

from __future__ import annotations

import json
import logging
import os
import secrets as pysecrets
import time
from contextlib import asynccontextmanager
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
from .modules import LEVEL_WORD, OK
from .modules.internet import checks as inet_checks
from .modules.internet import save as save_internet
from .modules.internet import speedtest as st
from .modules.internet import valid_host
from .notify import COMING_SOON, KINDS, Message, channel_secret
from .runtime import HC_SECRET, Runtime

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
    templates.env.globals["zone"] = tz(rt.db)
    ctx.update(request=request, user=user, home=rt.db.get("home.name") or "Home",
               update=rt.update_available(), flash=request.query_params.get("msg", ""),
               error=ctx.get("error") or request.query_params.get("err", ""))
    return templates.TemplateResponse(request, name, ctx)


def back(url: str, msg: str = "", err: str = "") -> RedirectResponse:
    from urllib.parse import urlencode

    q = urlencode({k: v for k, v in (("msg", msg), ("err", err)) if v})
    return RedirectResponse(url + ("?" + q if q else ""), status_code=303)


async def form_of(request: Request, user: User | None) -> dict | None:
    """Parse a POST form and check its CSRF token. Returns None when the token is wrong."""
    form = dict(await request.form())
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
                   kinds=list(KINDS), coming=COMING_SOON, hc=rt.secrets.has(HC_SECRET))
        return page(request, f"setup_{step}.html", request.state.user, **ctx)

    @app.post("/setup/{step}")
    async def setup_save(request: Request, step: str):
        user = request.state.user
        form = await form_of(request, user)
        if form is None:
            return back(f"/setup/{step}", err="Form expired, please try again")
        nxt = SETUP_STEPS[SETUP_STEPS.index(step) + 1] if step != SETUP_STEPS[-1] else None
        if form.get("skip") is None:
            err = _save_section(request, step, form, user)
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
    async def internet_page(request: Request):
        rt = rt_of(request)
        tests = rt.db.all("SELECT * FROM speedtests WHERE site=? ORDER BY ts DESC LIMIT 10", (rt.cfg.site,))
        ookla_found = os.access(st.ookla_path(rt.cfg.ookla_dir), os.X_OK)
        return page(request, "internet.html", request.state.user, s=rt.internet_settings(), tests=tests,
                    csrf=request.state.user.csrf, ookla_found=ookla_found, ookla_dir=rt.cfg.ookla_dir,
                    running=rt.speed_running)

    @app.post("/internet")
    async def internet_save(request: Request):
        form = await form_of(request, request.state.user)
        if form is None:
            return back("/internet", err="Form expired, please try again")
        err = _save_section(request, "internet", form, request.state.user)
        return back("/internet", err=err) if err else back("/internet", msg="Saved")

    @app.post("/internet/speedtest")
    async def speedtest_now(request: Request):
        rt = rt_of(request)
        if await form_of(request, request.state.user) is None:
            return back("/internet", err="Form expired")
        res = await rt.run_speedtest()
        audit(rt.db, request.state.user.username, "speedtest", "run now")
        if res.ok:
            return back("/internet", msg=f"Speed test: {res.down:.1f} / {res.up:.1f} Mbit/s")
        return back("/internet", err=f"Speed test failed: {res.error}")

    # -- checks, thresholds, snooze -------------------------------------------------------------
    @app.get("/checks")
    async def checks_page(request: Request):
        rt = rt_of(request)
        th = thresholds(rt.db, rt.cfg.site, inet_checks.MODULE, inet_checks.CHECKS)
        current_vals = {r.key: r for r in rt.results}
        snoozes = {r["check_key"]: r for r in rt.db.all(
            "SELECT * FROM snoozes WHERE site=? AND until>? ORDER BY until", (rt.cfg.site, time.time()))}
        return page(request, "checks.html", request.state.user, defs=inet_checks.CHECKS, th=th,
                    module=inet_checks.MODULE, current=current_vals, snoozes=snoozes,
                    csrf=request.state.user.csrf, timing=timing(rt.db), default_timing=DEFAULT_TIMING)

    @app.post("/checks")
    async def checks_save(request: Request):
        rt = rt_of(request)
        user = request.state.user
        form = await form_of(request, user)
        if form is None:
            return back("/checks", err="Form expired")
        changes = []
        old = thresholds(rt.db, rt.cfg.site, inet_checks.MODULE, inet_checks.CHECKS)
        for d in inet_checks.CHECKS:
            key = f"{inet_checks.MODULE}.{d.id}"
            enabled = form.get(f"{d.id}.enabled") == "on"
            warn = _float(form.get(f"{d.id}.warn")) if "warn" in d.editable else d.warn
            error = _float(form.get(f"{d.id}.error")) if "error" in d.editable else d.error
            if warn is not None and error is not None and not d.lower_is_worse and warn > error:
                return back("/checks", err=f"{d.name}: the warning level must not be above the error level")
            o = old[d.id]
            if (o.warn, o.error, o.enabled) != (warn, error, enabled):
                changes.append(f"{key}: warn {o.warn}→{warn}, error {o.error}→{error}, enabled {o.enabled}→{enabled}")
            rt.db.run("INSERT INTO checks(site,check_key,enabled,warn,error) VALUES(?,?,?,?,?) "
                      "ON CONFLICT(site,check_key) DO UPDATE SET enabled=excluded.enabled, warn=excluded.warn, "
                      "error=excluded.error", (rt.cfg.site, key, int(enabled), warn, error))
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
        chans = [dict(c) | {"config": json.loads(c["config"]), "levels": json.loads(c["levels"]),
                            "has_secret": rt.secrets.has(channel_secret(c["id"]))}
                 for c in rt.notifier.channels(enabled_only=False)]
        queued = rt.db.all("SELECT channel_id, COUNT(*) n, MAX(last_error) e FROM outbox GROUP BY channel_id")
        return page(request, "notifications.html", request.state.user, chans=chans, kinds=KINDS,
                    coming=COMING_SOON, csrf=request.state.user.csrf, hc=rt.secrets.has(HC_SECRET),
                    hc_last=rt.db.get("healthchecks.last_ok", 0), url=rt.db.get("home.url") or "",
                    schedule=rt.db.get("report.schedule") or {"weekday": 0, "time": "08:00"},
                    queued={q["channel_id"]: q for q in queued})

    @app.post("/notifications/add")
    async def channel_add(request: Request):
        form = await form_of(request, request.state.user)
        if form is None:
            return back("/notifications", err="Form expired")
        err = _save_section(request, "notify", form, request.state.user)
        return back("/notifications", err=err) if err else back("/notifications", msg="Channel added")

    @app.post("/notifications/{cid}")
    async def channel_edit(request: Request, cid: int):
        rt = rt_of(request)
        user = request.state.user
        form = await form_of(request, user)
        if form is None:
            return back("/notifications", err="Form expired")
        ch = rt.db.one("SELECT * FROM channels WHERE id=?", (cid,))
        if ch is None:
            return back("/notifications", err="Unknown channel")
        mod = KINDS[ch["kind"]]
        config = {k: form.get(k, "").strip() for k, _, _ in mod.FIELDS}
        levels = [lv for lv in (1, 2) if form.get(f"level{lv}") == "on"]
        qs, qe = form.get("quiet_start", ""), form.get("quiet_end", "")
        if bool(qs) != bool(qe):
            return back("/notifications", err="Quiet hours need both a start and an end")
        rt.db.run("UPDATE channels SET name=?, enabled=?, levels=?, quiet_start=?, quiet_end=?, weekly_report=?, "
                  "config=? WHERE id=?", (form.get("name", ch["name"])[:60], int(form.get("enabled") == "on"),
                                          json.dumps(levels), qs or None, qe or None,
                                          int(form.get("weekly_report") == "on"), json.dumps(config), cid))
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
        return back("/notifications", msg="Channel removed")

    @app.post("/notifications/{cid}/test")
    async def channel_test(request: Request, cid: int):
        rt = rt_of(request)
        if await form_of(request, request.state.user) is None:
            return back("/notifications", err="Form expired")
        ch = rt.db.one("SELECT * FROM channels WHERE id=?", (cid,))
        if ch is None:
            return back("/notifications", err="Unknown channel")
        home = rt.db.get("home.name") or "Home"
        msg = Message(OK, f"✅ {home}: test message", f"homewatch can reach this channel.\n{rt.db.get('home.url') or ''}")
        try:
            await rt.notifier.deliver(ch, msg)
        except Exception as e:  # noqa: BLE001
            return back("/notifications", err=f"{ch['name']}: {e}")
        return back("/notifications", msg=f"{ch['name']}: test message sent")

    @app.post("/settings/general")
    async def general_save(request: Request):
        rt = rt_of(request)
        user = request.state.user
        form = await form_of(request, user)
        if form is None:
            return back("/notifications", err="Form expired")
        url = form.get("url", "").strip()
        if url and not url.startswith("https://"):
            return back("/notifications", err="The page address must start with https://")
        rt.db.set("home.url", url)
        hc = form.get("hc_url", "").strip()
        if form.get("hc_clear") == "on":
            rt.secrets.delete(HC_SECRET)
        elif hc:
            if not hc.startswith("https://"):
                return back("/notifications", err="The healthchecks.io URL must start with https://")
            rt.secrets.put(HC_SECRET, hc)
        t = form.get("report_time", "08:00")
        try:
            h, m = map(int, t.split(":"))
            wd = int(form.get("report_weekday", "0"))
            assert 0 <= h < 24 and 0 <= m < 60 and 0 <= wd <= 6
        except (ValueError, AssertionError):
            return back("/notifications", err="Weekly report: invalid day or time")
        rt.db.set("report.schedule", {"weekday": wd, "time": f"{h:02d}:{m:02d}"})
        audit(rt.db, user.username, "settings.general", f"url={url} hc={'set' if hc else 'unchanged'}")
        return back("/notifications", msg="Saved")

    @app.post("/report/send")
    async def report_send(request: Request):
        rt = rt_of(request)
        if await form_of(request, request.state.user) is None:
            return back("/notifications", err="Form expired")
        n = await rt.send_report()
        if not n:
            return back("/notifications", err="No channel has the weekly report switched on")
        return back("/notifications", msg=f"Weekly report queued for {n} channel(s)")

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
                    home_name=rt.db.get("home.name") or "Home")

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

    @app.post("/system/updates")
    async def updates_toggle(request: Request):
        rt = rt_of(request)
        form = await form_of(request, request.state.user)
        if form is None:
            return back("/system", err="Form expired")
        rt.db.set("updates.check", form.get("check") == "on")
        audit(rt.db, request.state.user.username, "updates", f"check={form.get('check') == 'on'}")
        return back("/system", msg="Saved")


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
    errors, warnings = rt.engine.counts()
    return dict(tiles=tiles, overall=overall, open=rt.engine.open_issues(), names=rt.names(), now=now,
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
        rt.db.set("home.name", name or "Home")
        rt.db.set("home.tz", zone)
        audit(rt.db, user.username, "home", f"name={name} tz={zone}")
        return ""
    if section == "internet":
        s = rt.internet_settings()
        targets = [t.strip() for t in form.get("public.targets", "").replace(",", " ").split() if t.strip()]
        if not 1 <= len(targets) <= 4 or not all(valid_host(t) for t in targets):
            return "Public targets: 1 to 4 IP addresses or host names"
        gw, dns_server, domain = (form.get("gateway.target", "").strip(), form.get("dns.server", "").strip(),
                                  form.get("dns.domain", "").strip())
        for label, v in (("Gateway", gw), ("DNS server", dns_server), ("DNS name", domain)):
            if v and not valid_host(v):
                return f"{label}: not a valid address or name"
        engine = form.get("speed.engine", "cloudflare")
        if engine not in ("cloudflare", "ookla"):
            return "Unknown speed test engine"
        interval = _float(form.get("speed.interval_h"), 3)
        if not 0.5 <= interval <= 48:
            return "Speed test interval: 0.5 to 48 hours"
        new = {
            "public": {"enabled": form.get("public.enabled") == "on", "targets": targets},
            "gateway": {"enabled": form.get("gateway.enabled") == "on" and bool(gw), "target": gw},
            "dns": {"enabled": form.get("dns.enabled") == "on" and bool(dns_server), "server": dns_server,
                    "domain": domain or "cloudflare.com"},
            "speed": {"enabled": form.get("speed.enabled") == "on", "engine": engine, "interval_h": interval,
                      "contract_down": _float(form.get("speed.contract_down"), 0) or 0,
                      "contract_up": _float(form.get("speed.contract_up"), 0) or 0,
                      "ookla_accepted": form.get("speed.ookla_accepted") == "on"},
        }
        save_internet(rt.db, new)
        rt.sync_telegraf()
        audit(rt.db, user.username, "internet", json.dumps({k: new[k] for k in new if new[k] != s.get(k)}))
        rt.wake.set()
        return ""
    if section == "notify":
        kind = form.get("kind", "")
        if kind not in KINDS:
            return "Pick Telegram or ntfy"
        mod = KINDS[kind]
        config = {k: form.get(k, "").strip() for k, _, _ in mod.FIELDS}
        if kind == "telegram" and not (config["chat_id"] and form.get("secret")):
            return "Telegram needs a bot token and a chat ID"
        if kind == "ntfy" and not config["topic"]:
            return "ntfy needs a topic"
        if kind == "ntfy" and config["server"] and not config["server"].startswith(("https://", "http://")):
            return "ntfy server must start with https://"
        name = form.get("name", "").strip()[:60] or kind.capitalize()
        cid = rt.db.run("INSERT INTO channels(kind,name,levels,config) VALUES(?,?,?,?)",
                        (kind, name, json.dumps([1, 2]), json.dumps(config)))
        if form.get("secret"):
            rt.secrets.put(channel_secret(cid), form["secret"].strip())
        audit(rt.db, user.username, "channel.add", f"{kind} {name}")
        return ""
    if section == "healthchecks":
        url = form.get("hc_url", "").strip()
        if url:
            if not url.startswith("https://"):
                return "The healthchecks.io URL must start with https://"
            rt.secrets.put(HC_SECRET, url)
            audit(rt.db, user.username, "healthchecks", "set")
        return ""
    return "Unknown section"
