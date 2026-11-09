"""Web UI: server-rendered pages (Jinja + HTMX), login, CSRF, admin and viewer screens."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import secrets as pysecrets
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from urllib.parse import quote, urlencode
from datetime import datetime
from zoneinfo import ZoneInfo, available_timezones

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import __version__
from . import config as config_mod
from .auth import ROLES, Auth, AuthError, Throttled, User, audit
from .engine import DEFAULT_TIMING, thresholds, timing, tz
from .modules import CATALOG, LEVEL_WORD, OK
from .modules.registry import MODULES
from .notify import (COMING_SOON, KINDS, PROVIDERS, Message, channel_private, channel_secret, forget_channel, full_config,
                     private_fields, split_config, store_private)
from .runtime import Runtime

log = logging.getLogger("homewatch.web")
HERE = os.path.dirname(__file__)
COOKIE = "hw_session"
PRE = "hw_pre"
SETUP_STEPS = ["account", "home", "internet", "equipment", "notify", "healthchecks"]
SNOOZE_CHOICES = {"1h": 3600, "4h": 4 * 3600, "24h": 24 * 3600}

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


def _fmt_num(v) -> str:
    """180.0 -> "180", 2.5 -> "2.5": a limit as the admin typed it."""
    if v is None or v == "":
        return ""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)  # as typed, e.g. a form sent back with an error
    return str(int(f)) if f.is_integer() else f"{f:g}"


templates.env.filters["num"] = _fmt_num
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
    app.state.first_admin = asyncio.Lock()
    app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")
    app.add_middleware(BodyLimit)
    _routes(app)
    return app


# ---- request helpers ------------------------------------------------------------------------

def rt_of(request: Request) -> Runtime:
    return request.app.state.rt


def current(request: Request) -> User | None:
    return request.app.state.auth.session(request.cookies.get(COOKIE))


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


async def password_problem(request: Request, user: User, form: dict) -> str:
    """Sensitive actions ask for the signed-in user's password again; returns what is wrong, or ''."""
    try:
        await request.app.state.auth.verify(user.username, str(form.get("current", "")), client_ip(request))
    except Throttled as e:
        return str(e)
    except AuthError:
        return "Your current password is wrong"
    return ""


def page(request: Request, name: str, user: User | None, **ctx) -> HTMLResponse:
    rt = rt_of(request)
    if user and user.is_admin and not rt.db.get("home.url"):
        # the address the admin uses is the one to put in messages; editable under System > Home
        rt.db.set("home.url", f"{request.url.scheme}://{request.url.netloc}")
    templates.env.globals["zone"] = tz(rt.db)
    ctx.update(request=request, user=user, home=rt.db.get("home.name") or "Home",
               update=rt.update_available(), flash=request.query_params.get("msg", ""), grafana=rt.grafana_on,
               box_id=request.query_params.get("box", ""),
               error=ctx.get("error") or request.query_params.get("err", ""))
    return templates.TemplateResponse(request, name, ctx)


# the card a form belongs to ("_box" field): its message is shown inside that card after the redirect
_box: ContextVar[str] = ContextVar("box", default="")


def back(url: str, msg: str = "", err: str = "") -> RedirectResponse:
    box = _box.get()
    q = urlencode({k: v for k, v in (("msg", msg), ("err", err), ("box", box)) if v})
    return RedirectResponse(url + ("?" + q if q else "") + (f"#{box}" if box else ""), status_code=303)


MAX_BODY = 8 * 1024 * 1024  # the same limit as the Caddyfile; the largest real form is a long entity list


class _BodyTooLarge(Exception):
    pass


class BodyLimit:
    """Refuse a request body over the limit while it arrives, before anything stores it."""

    def __init__(self, app, limit: int = MAX_BODY):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        length = dict(scope["headers"]).get(b"content-length", b"0")
        too_large = PlainTextResponse("Request too large", status_code=413)
        if length.isdigit() and int(length) > self.limit:
            return await too_large(scope, receive, send)
        seen, started = 0, False

        async def counted():
            nonlocal seen
            message = await receive()
            seen += len(message.get("body", b""))
            if seen > self.limit:
                raise _BodyTooLarge
            return message

        async def tracked(message):
            nonlocal started
            started = started or message["type"] == "http.response.start"
            await send(message)

        try:
            await self.app(scope, counted, tracked)
        except _BodyTooLarge:
            if not started:
                await too_large(scope, receive, send)


async def form_of(request: Request, user: User | None, files: int = 0) -> dict | None:
    """Parse a POST form and check its CSRF token. Returns None when the token is wrong.

    Only a form that takes an upload passes files; any other that receives one answers 400.
    """
    form = dict(await request.form(max_files=files, max_fields=20000))  # module pages can list thousands of entities
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


HISTORY_ROWS = 500
NEVER_KEPT = {"csrf", "current", "password", "password2", "secret", "_box", "new", "new2"}


def keep_typed(request: Request, page_key: str, form: dict, also_secret=()) -> None:
    """Remember a form's text for the next view of its page, as the redirect would otherwise lose it."""
    rt_of(request).drafts[(request.state.user.id, page_key)] = {
        k: v for k, v in form.items() if isinstance(v, str) and k not in NEVER_KEPT and k not in also_secret
        and not k.endswith(".secret")}


def typed_over(saved: dict, draft: dict) -> dict:
    """The settings as the form had them. Tick boxes count only if the form's other fields were sent (not greyed out)."""
    def merge(values: dict, prefix: str) -> dict:
        out = {}
        for k, v in values.items():
            name = prefix + k
            if isinstance(v, dict):
                out[k] = merge(v, name + ".")
            elif isinstance(v, bool):
                out[k] = (draft.get(name) == "on") if sent and k != "enabled" else v
            elif isinstance(v, list):
                out[k] = draft[name].split() if name in draft else v
            elif isinstance(v, (str, int, float)) and name in draft:
                out[k] = draft[name]
            else:
                out[k] = v
        return out

    def scalars(values: dict, prefix: str = ""):
        for k, v in values.items():
            if isinstance(v, dict):
                yield from scalars(v, prefix + k + ".")
            elif not isinstance(v, bool):
                yield prefix + k

    sent = any(name in draft for name in scalars(saved))
    return merge(saved, "")


def ticks_over(ctx: dict, draft: dict) -> None:
    """Lists on a module's page as the form had them, so that a new reading or a failed save doesn't undo ticks."""
    if "enabled" not in draft and "_filters_shown" not in draft and "_entities" not in draft and not any(k.startswith(("choose.", "keep.", "watch.")) for k in draft):
        return
    if ctx.get("choices"):
        ctx["choices"] = [(c, "choose." + c.value in draft) for c, _ in ctx["choices"]]
    if ctx.get("filters"):
        shown = set(json.loads(draft.get("_filters_shown", "[]")))
        ctx["filters"] = [(c, "keep." + c.value in draft if c.value in shown else on) for c, on in ctx["filters"]]
    for g in ctx.get("guests", []):
        g["watched"] = f"watch.{g['vmid']}" in draft
    if "_entities" in draft:
        shown = set(json.loads(draft["_entities"]))
        for _, rows in ctx.get("entity_groups", []):
            for e in rows:
                if e["id"] in shown:
                    e["w"] = f"w.{e['id']}" in draft
                    if "_gcol" in draft:
                        e["g"] = f"g.{e['id']}" in draft
                    e["rule"] = draft.get(f"r.{e['id']}", e["rule"])


def _ha_grafana(rt) -> int:
    """Home Assistant entities ticked Show in Grafana (stored even while Grafana is off)."""
    m = MODULES.get("homeassistant")
    s = m.load(rt) if m else {}
    return len(s.get("grafana") or []) if m and m.enabled(s) else 0


def _dropped_note(rt) -> str:
    """What the last import or restore switched off because its destination changed."""
    if not rt.dropped:
        return ""
    return (" These had a new address, so their credentials were deleted and they are switched off: "
            + ", ".join(rt.dropped) + ".")


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
        if path == "/auth/grafana":  # Caddy's forward_auth for /grafana/: who is this, and in which Grafana role
            if user is None:
                return RedirectResponse("/login", status_code=303)
            return Response(status_code=200, headers={"X-Webauth-User": user.username,
                                                      "X-Webauth-Role": "Admin" if user.is_admin else "Viewer",
                                                      "Cache-Control": "no-store"})
        if user is None:
            return RedirectResponse("/login", status_code=303)
        request.state.user = user
        viewer_ok = path in ("/", "/history", "/help", "/partials/status", "/logout", "/password")
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
            async with request.app.state.first_admin:  # two forms sent at once: only the first creates an admin
                if auth.has_users():
                    return RedirectResponse("/login", status_code=303)
                await asyncio.to_thread(auth.create_user, form.get("username", ""), form.get("password", ""), "admin")
            token = await auth.login(form["username"], form["password"], client_ip(request))
        except AuthError as e:
            return back("/setup", err=str(e))
        audit(rt_of(request).db, form["username"], "setup", "admin account created")
        resp = RedirectResponse("/setup/home", status_code=303)
        set_session(resp, request, token)
        return resp

    @app.get("/setup/next-module")
    async def setup_next_module(request: Request, after: str = ""):
        """Wizard: from one chosen module page to the next, then on to notifications."""
        chosen = rt_of(request).db.get("setup.modules", [])
        rest = chosen[chosen.index(after) + 1:] if after in chosen else chosen
        return RedirectResponse(f"/modules/{rest[0]}" if rest else "/setup/notify", status_code=303)

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
        if step == "equipment":
            ctx["equipment"] = _equipment_choices(rt)
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
        if step == "equipment" and form.get("skip") is None:
            chosen = rt_of(request).db.get("setup.modules", [])
            if chosen:
                return RedirectResponse(f"/modules/{chosen[0]}", status_code=303)
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
            token = await auth.login(form.get("username", ""), form.get("password", ""), client_ip(request))
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
        return page(request, "status.html", request.state.user, **await _status_full(request))

    @app.get("/partials/status")
    async def status_partial(request: Request):
        return page(request, "_status_body.html", request.state.user, **await _status_full(request))

    @app.post("/status/filling/dismiss")
    async def filling_dismiss(request: Request):
        """Hide the "lines are still filling in" note for this user, until a module not in it yet starts a line."""
        rt = rt_of(request)
        user = request.state.user
        if await form_of(request, user) is None:
            return back("/")
        rt.db.set(f"status.filling_seen.{user.id}", sorted(n for n in (await _status_full(request))["filling_names"]))
        return RedirectResponse("/", status_code=303)

    @app.get("/history")
    async def history(request: Request, module: str = "", level: str = "", days: int = 30):
        rt = rt_of(request)
        days = max(1, min(days, 400))
        since = time.time() - days * 86400
        # also issues that started earlier but ended in the period, or are still open
        q = "SELECT * FROM issues WHERE site=? AND (started>? OR resolved>? OR resolved IS NULL)"
        args: list = [rt.cfg.site, since, since]
        if module in MODULES:
            q += " AND check_key LIKE ?"
            args.append(f"{module}.%")
        if level in ("1", "2"):
            q += " AND worst=?"
            args.append(int(level))
        rows = rt.db.all(q + f" ORDER BY started DESC LIMIT {HISTORY_ROWS + 1}", tuple(args))
        cut_off = len(rows) > HISTORY_ROWS
        rows = rows[:HISTORY_ROWS]
        now = time.time()
        # a timeline: each issue is a "problem" event and, once it ends, a "back to OK" event
        events = []
        for i in rows:
            events.append({"ts": i["started"], "kind": "problem", "level": i["worst"], "key": i["check_key"],
                           "detail": i["detail"], "open_for": None if i["resolved"] else now - i["started"],
                           "after": i["resolved"] - i["started"] if i["resolved"] else None})
            if i["resolved"] and level not in ("1", "2"):  # filtering by level: the problems only
                off = i["resolved_detail"] == "check switched off"
                events.append({"ts": i["resolved"], "kind": "off" if off else "ok", "level": OK,
                               "key": i["check_key"], "detail": i["resolved_detail"],
                               "after": i["resolved"] - i["started"]})
        events = [e for e in events if e["ts"] > since or e.get("open_for") is not None]
        events.sort(key=lambda e: e["ts"], reverse=True)
        from .report import availability

        avail = availability(rt.db, rt.cfg.site, since, now)
        check_names = {f"{m.id}.{c.id}": c.name for m in MODULES.values() for c in m.checks}
        groups = [(m.name, in_module) for m in MODULES.values()
                  if module == m.id and (in_module := [e for e in events if e["key"].split(".", 1)[0] == m.id])]
        sources = {m.id: m.name for m in MODULES.values()}
        for e in events:
            mid = e["key"].split(".", 1)[0]
            e["source"] = sources.get(mid, mid)
        incidents = _incidents(rt, since, now, check_names, sources) if not module and not level else []
        return page(request, "history.html", request.state.user, events=events, groups=groups, names=check_names,
                    incidents=incidents,
                    modules=[(m.id, m.name) for m in MODULES.values()], module=module, level=level, days=days, avail=avail, since=since, now=now, cut_off=cut_off, history_rows=HISTORY_ROWS)

    @app.get("/help")
    async def help_page(request: Request):
        return page(request, "help.html", request.state.user, modules=list(MODULES.values()))

    # -- internet module ------------------------------------------------------------------------
    @app.get("/internet")
    async def internet_old(request: Request):
        return RedirectResponse("/modules/internet", status_code=308)

    @app.get("/modules")
    async def modules_page(request: Request):
        ctx = await _status_full(request)  # the same tiles as the Status page, plus one for every module that is off
        return page(request, "modules.html", request.state.user, mod_state=_module_states(rt_of(request)),
                    cards={c["id"]: c for c in ctx["cards"]}, module_info=ctx["module_info"],
                    icons={m.id: m.icon for m in MODULES.values()})

    @app.get("/modules/{mid}")
    async def module_page(request: Request, mid: str):
        rt = rt_of(request)
        m = MODULES.get(mid)
        if m is None:
            return RedirectResponse("/modules", status_code=303)
        template = getattr(m, "template", None) or f"module_{mid}.html"
        s, ctx = m.load(rt), m.page_ctx(rt)
        draft = rt.drafts.pop((request.state.user.id, f"/modules/{mid}"), None)
        if draft:
            s = typed_over(s, draft)
            ticks_over(ctx, draft)
        return page(request, template, request.state.user, s=s, csrf=request.state.user.csrf,
                    wizard=_wizard_ctx(rt, mid),
                    module=m, th_rows=thresholds(rt.db, rt.cfg.site, m.id, m.checks),
                    th_current={r.key: r for r in rt.results}, **ctx)

    @app.post("/modules/{mid}")
    async def module_save(request: Request, mid: str):
        rt = rt_of(request)
        m = MODULES.get(mid)
        if m is None:
            return RedirectResponse("/modules", status_code=303)
        form = await form_of(request, request.state.user)
        if form is None:
            return back(f"/modules/{mid}", err="Form expired, please try again")
        secret_fields = [f.key for f in getattr(m, "fields", []) if f.secret]
        if mid == "internet" and form.get("speed.engine") == "ookla":
            err = await _ookla_ready(rt, form, request.state.user)
            if err:
                keep_typed(request, f"/modules/{mid}", form)
                return back(f"/modules/{mid}", err=err)
        first_connect = not form.get("remove") and hasattr(m, "configured") and not m.configured(rt)
        if hasattr(m, "prepare_save"):  # e.g. test the connection before anything is stored
            err = await m.prepare_save(rt, form)
            if err:
                keep_typed(request, f"/modules/{mid}", form, secret_fields)
                return back(f"/modules/{mid}", err=err)
        err = m.save(rt, form, request.state.user)
        if err:
            return back(f"/modules/{mid}", err=err)
        if form.get("remove"):
            return back(f"/modules/{mid}", msg="Removed")
        if first_connect and mid == "proxmox":
            return back(f"/modules/{mid}", msg="Connected. Now tick the guests homeWatch should watch and press Save.")
        if first_connect:
            return back(f"/modules/{mid}", msg="Connected. The first checks run within 2 minutes.")
        removed = mid == "speedport" and not form.get("model") and not m.page_ctx(rt)["configured"]
        return back(f"/modules/{mid}", msg="Modem removed" if removed else "Saved")

    @app.post("/modules/{mid}/refresh")
    async def module_refresh(request: Request, mid: str):
        rt = rt_of(request)
        m = MODULES.get(mid)
        form = await form_of(request, request.state.user)
        if m is None or not (hasattr(m, "fetch") or mid == "proxmox"):
            return RedirectResponse("/modules", status_code=303)
        if form is None:
            return back(f"/modules/{mid}", err="Form expired, please try again")
        if set(form) - {"csrf", "_box"}:  # sent from a settings form: the new reading keeps its unsaved ticks
            keep_typed(request, f"/modules/{mid}", form, [f.key for f in getattr(m, "fields", []) if f.secret])
        if hasattr(m, "reread"):  # read now, not at the module's next turn
            m.reread()
        else:
            m.stale = True
        await m.readings(rt, m.load(rt), time.time())
        rt.wake.set()  # re-run the checks on the fresh reading
        if m.error:
            return back(f"/modules/{mid}", err=f"Can't read {m.name}: {m.error}")
        summary = (getattr(m, "snap", None) or {}).get("_summary")
        return back(f"/modules/{mid}", msg=f"Read {m.name} now: {summary}" if summary else f"{m.name} read now")

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

    @app.post("/modules/speedport/trust-certificate")
    async def speedport_trust_certificate(request: Request):
        form = await form_of(request, request.state.user)
        if form is None:
            return back("/modules/speedport", err="Form expired, please try again")
        err = MODULES["speedport"].trust_new_certificate(rt_of(request), str(form.get("fingerprint", "")), request.state.user)
        return back("/modules/speedport", err=err) if err else back("/modules/speedport", msg="New certificate trusted")

    @app.post("/modules/{mid}/trust-certificate")
    async def module_trust_certificate(request: Request, mid: str):
        m = MODULES.get(mid)
        form = await form_of(request, request.state.user)
        if form is None or not hasattr(m, "trust_certificate"):
            return back(f"/modules/{mid}" if m else "/modules", err="Form expired, please try again")
        err = m.trust_certificate(rt_of(request), str(form.get("host", "")), str(form.get("fingerprint", "")), request.state.user)
        return back(f"/modules/{mid}", err=err) if err else back(f"/modules/{mid}", msg="New certificate trusted")

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

    # -- checks, limits, timing, snooze ----------------------------------------------------------
    @app.get("/checks")
    async def checks_moved():  # limits are on each module's page now; old bookmarks land on the list
        return RedirectResponse("/modules", status_code=303)

    def _config_row(rt, cid: int):
        return rt.db.one("SELECT * FROM device_configs WHERE id=? AND site=? AND module='mikrotik'", (cid, rt.cfg.site))

    @app.get("/modules/mikrotik/configs/{cid}")
    async def mikrotik_config(request: Request, cid: int):
        """One saved configuration: what changed since the copy before it, and the whole text."""
        import difflib
        from .modules.mikrotik.plugin import config_body
        rt = rt_of(request)
        row = _config_row(rt, cid)
        if row is None:
            return RedirectResponse("/modules/mikrotik#configs", status_code=303)
        prev = rt.db.one("SELECT ts, text FROM device_configs WHERE site=? AND module='mikrotik' AND device=? AND ts<? "
                         "ORDER BY ts DESC LIMIT 1", (rt.cfg.site, row["device"], row["ts"]))
        diff = []
        if prev:
            for line in difflib.unified_diff(config_body(prev["text"]).split("\n"), config_body(row["text"]).split("\n"),
                                             lineterm="", n=3):
                if line.startswith(("---", "+++")):
                    continue
                diff.append(("at" if line.startswith("@@") else "add" if line.startswith("+")
                             else "del" if line.startswith("-") else "ctx", line))
        return page(request, "mikrotik_config.html", request.state.user, id=cid, dev_name=row["name"] or row["device"],
                    device=row["device"], ts=row["ts"], text=row["text"], lines=row["text"].count("\n") + 1,
                    prev_ts=prev["ts"] if prev else None, diff=diff)

    @app.get("/modules/mikrotik/configs/{cid}/download")
    async def mikrotik_config_download(request: Request, cid: int):
        rt = rt_of(request)
        row = _config_row(rt, cid)
        if row is None:
            return RedirectResponse("/modules/mikrotik#configs", status_code=303)
        name = re.sub(r"[^A-Za-z0-9._-]+", "-", row["name"] or row["device"]).strip("-") or "mikrotik"
        when = datetime.fromtimestamp(row["ts"], ZoneInfo(rt.db.get("home.tz") or "UTC")).strftime("%Y-%m-%d-%H%M")
        return Response(row["text"], media_type="text/plain; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{name}-{when}.rsc"'})

    @app.post("/modules/{mid}/thresholds")
    async def thresholds_save(request: Request, mid: str):
        rt = rt_of(request)
        user = request.state.user
        m = MODULES.get(mid)
        if m is None:
            return RedirectResponse("/modules", status_code=303)
        form = await form_of(request, user)
        here = f"/modules/{mid}"
        if form is None:
            return back(here, err="Form expired")
        changes, rows = [], []
        old = thresholds(rt.db, rt.cfg.site, m.id, m.checks)
        hidden = m.hidden_checks(m.load(rt)) if hasattr(m, "hidden_checks") and hasattr(m, "load") else set()
        for d in m.checks:
            key = f"{m.id}.{d.id}"
            if d.id in hidden:  # not on the page: keeps its limits
                continue
            enabled = form.get(f"{key}.enabled") == "on"
            warn = _float(form.get(f"{key}.warn")) if "warn" in d.editable else d.warn
            error = _float(form.get(f"{key}.error")) if "error" in d.editable else d.error
            if d.unit == "%" and any(v is not None and not 0 <= v <= 100 for v in (warn, error)):
                return back(here, err=f"{d.name}: a percentage, from 0 to 100")
            if warn is not None and error is not None and (error > warn if d.lower_is_worse else warn > error):
                return back(here, err=f"{d.name}: the WARNING level must come before the ERROR level")
            o = old[d.id]
            if (o.warn, o.error, o.enabled) != (warn, error, enabled):
                changes.append(f"{key}: warn {o.warn}→{warn}, error {o.error}→{error}, "
                               f"enabled {o.enabled}→{enabled}")
            rows.append((rt.cfg.site, key, int(enabled), warn, error))
        picked = {}  # sensors chosen in a check's row (Home Assistant: disk, memory, CPU)
        for cid, fkey in (m.limit_fields().items() if hasattr(m, "limit_fields") else ()):
            v = form.get(f"{m.id}.{cid}.sensor")
            if v is None:
                continue
            v = v.strip()
            if v and not re.fullmatch(r"sensor\.[a-z0-9_]+", v):
                return back(here, err=f"{m.field(fkey).label}: an entity id like sensor.disk_use_percent")
            picked[fkey] = v
        if picked:
            s = m.load(rt)
            new = {k: v for k, v in picked.items() if (s.get(k) or "") != v}
            if new:
                s = {k: v for k, v in (s | new).items() if k != "_configured"}
                rt.db.set(m.key, s)
                changes.append(", ".join(f"{k}: {v or 'default'}" for k, v in new.items()))
                m.reread()
        for row in rows:
            rt.db.run("INSERT INTO checks(site,check_key,enabled,warn,error) VALUES(?,?,?,?,?) "
                      "ON CONFLICT(site,check_key) DO UPDATE SET enabled=excluded.enabled, "
                      "warn=excluded.warn, error=excluded.error", row)
        if changes:
            audit(rt.db, user.username, "checks", "; ".join(changes))
        rt.wake.set()
        return back(here, msg="Saved")

    @app.post("/notifications/timing")
    async def timing_save(request: Request):
        rt = rt_of(request)
        user = request.state.user
        form = await form_of(request, user)
        if form is None:
            return back("/notifications", err="Form expired")
        t = {}
        for k, lo, hi in (("interval", 30, 3600), ("confirm", 1, 10), ("remind_h", 0, 168), ("fail_alert", 1, 100)):
            v = _float(form.get(f"timing.{k}"))
            if v is None or not lo <= v <= hi:
                return back("/notifications", err=f"{k} must be between {lo} and {hi}")
            t[k] = v
        if t != timing(rt.db):
            old = timing(rt.db)
            rt.db.set("engine.timing", t)
            audit(rt.db, user.username, "checks", f"timing {old} → {t}")
        rt.wake.set()
        return back("/notifications", msg="Saved")

    @app.post("/checks/run")
    async def checks_run(request: Request):
        rt = rt_of(request)
        if await form_of(request, request.state.user) is None:
            return back("/", err="Form expired")
        await rt.check_now()
        if rt.last_error:
            return back("/", err=f"Check run failed: {rt.last_error}")
        return back("/", msg=f"Checks ran successfully at {_fmt_ts(rt.last_run, '%H:%M:%S')}.")

    @app.post("/maintenance/snooze")
    async def snooze(request: Request):
        rt = rt_of(request)
        user = request.state.user
        form = await form_of(request, user)
        here = "/" if form and form.get("_from") == "status" else "/maintenance"
        if form is None:
            return back(here, err="Form expired")
        key = form.get("check", "")
        if key not in rt.names():
            return back(here, err="Unknown check")
        choice = form.get("for", "")
        now = time.time()
        if choice in SNOOZE_CHOICES:
            until = now + SNOOZE_CHOICES[choice]
        else:
            try:
                until = datetime.fromisoformat(form.get("until", "")).replace(tzinfo=tz(rt.db)).timestamp()
            except ValueError:
                return back(here, err="Pick a duration or a valid time")
        if until <= now:
            return back(here, err="That time is in the past")
        reason = form.get("reason", "")[:200]
        rt.db.run("INSERT INTO snoozes(site,check_key,until,reason,created_by,created) VALUES(?,?,?,?,?,?)",
                  (rt.cfg.site, key, until, reason, user.username, now))
        audit(rt.db, user.username, "snooze", f"{key} until {_fmt_ts(until)} ({reason})")
        rt.wake.set()
        return back(here, msg="Snoozed")

    @app.post("/maintenance/unsnooze")
    async def unsnooze(request: Request):
        rt = rt_of(request)
        user = request.state.user
        form = await form_of(request, user)
        if form is None:
            return back("/maintenance", err="Form expired")
        rt.db.run("UPDATE snoozes SET until=? WHERE site=? AND check_key=? AND until>?",
                  (time.time(), rt.cfg.site, form.get("check", ""), time.time()))
        audit(rt.db, user.username, "unsnooze", form.get("check", ""))
        rt.wake.set()
        return back("/maintenance", msg="Snooze ended")

    # -- maintenance -----------------------------------------------------------------------------
    @app.get("/maintenance")
    async def maintenance_page(request: Request):
        rt = rt_of(request)
        rows = rt.db.all("SELECT * FROM maintenance WHERE site=? ORDER BY id DESC", (rt.cfg.site,))
        # checks to pick from, folded per module (modules in use only)
        check_groups = [(m.name, [(f"{m.id}.{c.id}", c.name) for c in m.checks]) for m in MODULES.values()
                        if m.enabled(m.load(rt))]
        snoozes = rt.db.all("SELECT * FROM snoozes WHERE site=? AND until>? ORDER BY until", (rt.cfg.site, time.time()))
        return page(request, "maintenance.html", request.state.user, rows=rows, names=rt.names(), snoozes=snoozes,
                    check_groups=check_groups, csrf=request.state.user.csrf, now=time.time(), json=json)

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
            config, draft = json.loads(c["config"]), rt.drafts.pop((request.state.user.id, f"/notifications/{c['id']}"), None)
            row = dict(c)
            if draft:  # as typed; encrypted fields stay empty
                public = {k: draft[k] for k, _, _ in PROVIDERS[c["kind"]].FIELDS if k in draft and k not in private_fields(c["kind"])}
                config |= public
                row |= {"name": draft.get("name", c["name"]), "quiet_start": draft.get("quiet_start") or None,
                        "quiet_end": draft.get("quiet_end") or None, "enabled": int(draft.get("enabled") == "on")}
            chans.append(row | {"config": config, "levels": [lv for lv in (1, 2) if f"level{lv}" in draft] if draft else json.loads(c["levels"]),
                                    "has_private": rt.secrets.has(channel_private(c["id"])),
                                    "last_ok": rt.db.get(f"channel.{c['id']}.last_ok", 0),
                                    "has_secret": rt.secrets.has(channel_secret(c["id"])),
                                    "last_ping": rt.db.get(f"heartbeat.{c['id']}.last_ok", 0)})
        queued = rt.db.all("SELECT channel_id, COUNT(*) n, MAX(last_error) e, MIN(created) oldest, MIN(next_try) next_try "
                           "FROM outbox GROUP BY channel_id")
        return page(request, "notifications.html", request.state.user, chans=chans, providers=PROVIDERS,
                    timing=timing(rt.db), default_timing=DEFAULT_TIMING,
                    coming=COMING_SOON, csrf=request.state.user.csrf,
                    add=rt.drafts.pop((request.state.user.id, "/notifications/add"), {}), now=time.time(),
                    schedule=rt.db.get("report.schedule") or {"weekday": 0, "time": "08:00"},
                    queued={q["channel_id"]: q for q in queued})

    @app.post("/notifications/add")
    async def channel_add(request: Request):
        form = await form_of(request, request.state.user)
        if form is None:
            return back("/notifications", err="Form expired")
        err = _save_section(request, "notify", form, request.state.user)
        if err:
            private = {f"{k}.{name}" for k in PROVIDERS for name in private_fields(k)}
            keep_typed(request, "/notifications/add", form, private)
            return back("/notifications", err=err)
        return back("/notifications", msg="Added")

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
        config, err = _channel_form(rt, ch, form)
        if err:
            keep_typed(request, f"/notifications/{cid}", form, private_fields(ch["kind"]))
            return back("/notifications", err=err)
        config, private = split_config(ch["kind"], config)
        levels = [lv for lv in (1, 2) if form.get(f"level{lv}") == "on"]
        qs, qe = form.get("quiet_start", ""), form.get("quiet_end", "")
        if bool(qs) != bool(qe):
            return back("/notifications", err="Quiet hours need both a start and an end")
        rt.db.run("UPDATE channels SET name=?, enabled=?, levels=?, quiet_start=?, quiet_end=?, config=? WHERE id=?",
                  (form.get("name", ch["name"])[:60] or ch["name"], int(form.get("enabled") == "on"),
                   json.dumps(levels), qs or None, qe or None, json.dumps(config), cid))
        store_private(rt.secrets, cid, private)
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
        forget_channel(rt.secrets, cid)
        rt.db.run("DELETE FROM settings WHERE key IN (?, ?)", (f"channel.{cid}.last_ok", f"heartbeat.{cid}.last_ok"))
        audit(rt.db, user.username, "channel.delete", str(cid))
        return back("/notifications", msg="Removed")

    @app.post("/notifications/{cid}/test")
    async def channel_test(request: Request, cid: int):
        rt = rt_of(request)
        form = await form_of(request, request.state.user)
        if form is None:
            return back("/notifications", err="Form expired")
        ch = rt.db.one("SELECT * FROM channels WHERE id=?", (cid,))
        if ch is None or ch["kind"] not in PROVIDERS:
            return back("/notifications", err="Unknown channel")
        if "name" in form:  # the page stays as typed: testing saves nothing
            keep_typed(request, f"/notifications/{cid}", form, private_fields(ch["kind"]))
        config, err = _channel_form(rt, ch, form)
        if err:
            return back("/notifications", err=err)
        try:
            if ch["kind"] == "healthchecks":
                await PROVIDERS["healthchecks"].ping(rt.notifier.http, config)
                rt.db.set(f"heartbeat.{cid}.last_ok", time.time())
                return back("/notifications", msg=f"{ch['name']}: ping sent; healthchecks.io should show it as up")
            home = rt.db.get("home.name") or "Home"
            msg = Message(OK, f"✅ {home}: test message",
                          f"homeWatch can reach this channel.\n{rt.db.get('home.url') or ''}")
            await rt.notifier.deliver(ch, msg, config, form.get("secret", "").strip() or None)
        except Exception as e:  # noqa: BLE001
            return back("/notifications", err=f"{ch['name']}: {e}")
        return back("/notifications", msg=f"{ch['name']}: test message sent")

    # -- system ------------------------------------------------------------------------------------
    @app.get("/system")
    async def system_page(request: Request):
        from . import updater
        rt = rt_of(request)
        users = rt.db.all("SELECT id, username, role, created FROM users ORDER BY username")
        log_rows = rt.db.all("SELECT * FROM audit ORDER BY id DESC LIMIT 100")
        version_rows = rt.db.all("SELECT id, ts, username, action, summary FROM config_versions ORDER BY id DESC LIMIT 200")
        vm_ok = await rt.metrics.healthy()
        return page(request, "system.html", request.state.user, users=users, log=log_rows, vm_ok=vm_ok,
                    **await _grafana_ctx(rt),
                    last_run=rt.last_run, last_error=rt.last_error, csrf=request.state.user.csrf,
                    updates=rt.db.get("updates.check", True), latest=rt.db.get("updates.latest", ""),
                    checked=rt.db.get("updates.checked", 0), updater=updater.state(rt.cfg.update_dir),
                    roles=ROLES, tzname=rt.db.get("home.tz") or "UTC", zones=sorted(available_timezones()),
                    home_name=rt.db.get("home.name") or "Home", home_url=rt.db.get("home.url") or "",
                    probe_age=_probe_age(rt), probes_started=bool(rt.results), ookla=_ookla_version(rt), versions=version_rows,
                    exports=_exports(rt), connections=[(m.name, *t) for m in MODULES.values()
                                                       if m.is_set_up(rt) and (t := m.transport(rt))])

    @app.get("/system/export")
    async def config_export(request: Request):
        from . import versions
        name = f"homewatch-config-{time.strftime('%Y-%m-%d')}.yaml"
        return Response(versions.to_yaml(rt_of(request).db), media_type="application/yaml",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.get("/system/exports")
    async def config_export_pick(name: str = ""):  # the export dropdown
        return RedirectResponse(f"/system/exports/{quote(name)}" if name else "/system#backup", status_code=303)

    @app.get("/system/exports/{name}")
    async def config_export_file(request: Request, name: str):
        from . import versions
        rt = rt_of(request)
        path = os.path.join(rt.exports_dir, name)
        if not versions.export_name_ok(name) or not os.path.isfile(path):
            return RedirectResponse("/system#backup", status_code=303)
        with open(path, encoding="utf-8") as f:
            body = f.read()
        return Response(body, media_type="application/yaml",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.post("/system/import")
    async def config_import(request: Request):
        from . import versions
        rt = rt_of(request)
        form = await form_of(request, request.state.user, files=1)
        if form is None:
            return back("/system", err="Form expired")
        up = form.get("file")
        if not hasattr(up, "read"):
            return back("/system", err="Choose an export file first")
        raw = await up.read(versions.MAX_IMPORT + 1)
        if len(raw) > versions.MAX_IMPORT:
            return back("/system", err="That file is too big for a homeWatch export")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return back("/system", err="That file is not a text (YAML) file")
        plan, err = rt.preview_import(text)
        if err:
            return back("/system", err=f"Nothing imported: {err}")
        return page(request, "config_preview.html", request.state.user, title="Import settings", action="/system/import/apply",
                    box="backup", text=text, button="Import", csrf=request.state.user.csrf, **plan)

    @app.post("/system/import/apply")
    async def config_import_apply(request: Request):
        from . import versions
        rt = rt_of(request)
        user = request.state.user
        form = await form_of(request, user)
        if form is None:
            return back("/system", err="Form expired")
        text = str(form.get("text", ""))
        if len(text.encode()) > versions.MAX_IMPORT:
            return back("/system", err="That file is too big for a homeWatch export")
        problem = await password_problem(request, user, form)
        if problem:
            return back("/system", err=f"Nothing imported: {problem}")
        err = rt.import_config(text, user.username)
        if err:
            return back("/system", err=f"Nothing imported: {err}")
        return back("/system", msg="Imported. The previous settings are kept as a version. Enter passwords and "
                                   "tokens on any module or channel that shows \"not set\"." + _dropped_note(rt))

    @app.get("/system/versions/{vid}")
    async def version_view(request: Request, vid: int):
        row = rt_of(request).db.one("SELECT * FROM config_versions WHERE id=?", (vid,))
        if row is None:
            return RedirectResponse("/system#versions", status_code=303)
        body = json.dumps({"version": row["id"], "saved": _fmt_ts(row["ts"]), "by": row["username"],
                           "change": row["summary"], **json.loads(row["data"])}, indent=2)
        return Response(body, media_type="application/json",
                        headers={"Content-Disposition": f'inline; filename="homewatch-config-v{vid}.json"'})

    @app.get("/system/versions/{vid}/restore")
    async def version_restore_preview(request: Request, vid: int):
        rt = rt_of(request)
        plan = rt.preview_restore(vid)
        if plan is None:
            return back("/system", err=f"Version {vid} no longer exists")
        return page(request, "config_preview.html", request.state.user, title=f"Restore version {vid}",
                    action=f"/system/versions/{vid}/restore", box="versions", text="", button="Restore",
                    csrf=request.state.user.csrf, **plan)

    @app.post("/system/versions/{vid}/restore")
    async def version_restore(request: Request, vid: int):
        rt = rt_of(request)
        form = await form_of(request, request.state.user)
        if form is None:
            return back("/system", err="Form expired")
        problem = await password_problem(request, request.state.user, form)
        if problem:
            return back("/system", err=problem)
        if not rt.restore_version(vid, request.state.user.username):
            return back("/system", err=f"Version {vid} no longer exists")
        return back("/system", msg=f"Version {vid} restored." + (_dropped_note(rt) or " Passwords and tokens were left as they are."))

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
        problem = await password_problem(request, user, form)
        if problem:
            return back("/system", err=problem)
        try:
            await asyncio.to_thread(request.app.state.auth.create_user, form.get("username", ""), form.get("password", ""),
                                    form.get("role", "viewer"))
        except AuthError as e:
            return back("/system", err=str(e))
        audit(rt.db, user.username, "user.add", f"{form.get('username')} ({form.get('role')})")
        return back("/system", msg="User added")

    @app.post("/users/{uid}/delete")
    async def user_delete(request: Request, uid: int):
        rt = rt_of(request)
        user = request.state.user
        form = await form_of(request, user)
        if form is None:
            return back("/system", err="Form expired")
        problem = await password_problem(request, user, form)
        if problem:
            return back("/system", err=problem)
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
        problem = await password_problem(request, user, form)
        if problem:
            return back("/password", err=problem)
        if form.get("new") != form.get("new2"):
            return back("/password", err="The new passwords don't match")
        try:
            await asyncio.to_thread(auth.set_password, user.id, form.get("new", ""))
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
        problem = await password_problem(request, user, form)
        if problem:
            return back("/system", err=problem)
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

    @app.post("/system/grafana")
    async def grafana_switch(request: Request):
        from . import updater
        rt = rt_of(request)
        form = await form_of(request, request.state.user)
        if form is None:
            return back("/system", err="Form expired")
        _box.set("grafana")
        want = "on" if form.get("to") == "on" else "off"
        err = updater.ask(rt.cfg.update_dir, "", request.state.user.username, f"grafana {want}")
        if err:
            return back("/system", err=err)
        audit(rt.db, request.state.user.username, "grafana", f"asked the updater to turn Grafana {want}")
        if want == "off":
            rt.grafana_on = False  # the links go now; turning on shows them once Grafana answers
        return back("/system", msg=f"Asked the host to turn Grafana {want}; this takes up to a minute"
                    + (" (the first time it downloads Grafana, about 2 GB)." if want == "on" else "."))

    @app.post("/system/retention")
    async def retention_set(request: Request):
        from . import updater
        rt = rt_of(request)
        user = request.state.user
        form = await form_of(request, user)
        if form is None:
            return back("/system", err="Form expired")
        _box.set("grafana")
        keep = form.get("keep", "")
        if keep not in updater.RETENTIONS:
            return back("/system", err="Choose how long to keep history")
        if keep == rt.cfg.retention:
            return back("/system", msg=f"History is already kept for {updater.RETENTIONS[keep]}")
        problem = await password_problem(request, user, form)
        if problem:
            return back("/system", err=problem)
        err = updater.ask(rt.cfg.update_dir, "", user.username, f"retention {keep}")
        if err:
            return back("/system", err=err)
        audit(rt.db, user.username, "retention", f"asked the updater to keep {keep} of history (was {rt.cfg.retention})")
        return back("/system", msg=f"Asked the host to keep {updater.RETENTIONS[keep]} of history; "
                    "homeWatch restarts in a few seconds.")

    @app.get("/system/grafana-exports")
    async def grafana_export_pick(request: Request):
        name = request.query_params.get("name", "")
        return RedirectResponse(f"/system/grafana-exports/{quote(name)}" if name else "/system#grafana", status_code=303)

    @app.get("/system/grafana-exports/{name}")
    async def grafana_export_file(request: Request, name: str):
        from . import grafana
        rt = rt_of(request)
        path = os.path.join(rt.exports_dir, name)
        if not grafana.MY_EXPORT.match(name) or not os.path.isfile(path):
            return RedirectResponse("/system#grafana", status_code=303)
        with open(path, encoding="utf-8") as f:
            body = f.read()
        return Response(body, media_type="application/json", headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.post("/system/updates/check")
    async def updates_check(request: Request):
        rt = rt_of(request)
        if await form_of(request, request.state.user) is None:
            return back("/system", err="Form expired")
        _box.set("home")
        await rt.check_update(time.time())
        latest, newer = rt.db.get("updates.latest", ""), rt.update_available()
        if not latest:
            return back("/system", err="Couldn't read the latest release from GitHub; try again later")
        return back("/system", msg=f"homeWatch {newer} is available" if newer else f"{latest} is the latest release")

    @app.post("/system/updates/install")
    async def updates_install(request: Request):
        from . import updater
        rt = rt_of(request)
        if await form_of(request, request.state.user) is None:
            return back("/system", err="Form expired")
        _box.set("health")
        newer = rt.update_available()
        if not newer:
            return back("/system", err="No newer release to install")
        err = updater.ask(rt.cfg.update_dir, newer, request.state.user.username)
        if err:
            return back("/system", err=err)
        audit(rt.db, request.state.user.username, "update", f"asked the updater for {newer}")
        return RedirectResponse(f"/system/update/progress?to={quote(newer)}", status_code=303)

    @app.get("/system/update")
    async def update_confirm(request: Request):
        """Before updating: what happens, the release notes, OK or Cancel."""
        from . import updater
        rt = rt_of(request)
        newer = rt.update_available()
        if not newer or not updater.state(rt.cfg.update_dir)["ready"]:
            return RedirectResponse("/system", status_code=303)
        return page(request, "update.html", request.state.user, csrf=request.state.user.csrf, to=newer,
                    notes=_release_notes(rt.db.get("updates.notes", "")))

    @app.get("/system/update/progress")
    async def update_progress(request: Request, to: str = ""):
        if not re.fullmatch(r"\d+\.\d+\.\d+", to):
            return RedirectResponse("/system", status_code=303)
        return page(request, "update_progress.html", request.state.user, to=to)

    @app.get("/system/updates/state")
    async def update_state(request: Request):
        from . import updater
        st = updater.state(rt_of(request).cfg.update_dir)
        return JSONResponse({"state": st["state"], "summary": st["summary"], "asked": st["asked"], "version": __version__})


def _probe_age(rt) -> float | None:
    """Seconds since Telegraf's last ping/DNS data, from the latest check run."""
    fresh = next((r for r in rt.results if r.key == "internet.fresh"), None)
    return fresh.value if fresh and fresh.value is not None else None


def _ookla_version(rt) -> str:
    from .modules.internet.speedtest import ookla_path

    path = ookla_path(rt.cfg.ookla_dir)
    if not os.access(path, os.X_OK):
        return ""
    if not rt.db.get("ookla.version"):  # installed before versions were recorded: ask the program once
        import subprocess

        try:
            out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=5).stdout
            rt.db.set("ookla.version", out.split()[3] if out.startswith("Speedtest by Ookla") else "")
        except (OSError, subprocess.SubprocessError, IndexError):
            return "version unknown"
    return rt.db.get("ookla.version") or "version unknown"


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
    rt.db.set("ookla.version", OOKLA_VERSION)
    return ""


def _module_states(rt) -> dict[str, dict]:
    """Per module: on/off and, when on, its worst confirmed check status (as on the status page)."""
    states = {r["check_key"]: r["status"] for r in rt.db.all("SELECT check_key, status FROM check_state WHERE site=?",
                                                               (rt.cfg.site,))}
    out = {}
    for m in MODULES.values():
        on = m.enabled(m.load(rt))
        levels = [states.get(r.key, OK) for r in rt.results if r.key.startswith(m.id + ".")] if on else []
        out[m.id] = {"on": on, "checks": len(levels), "worst": max(levels, default=OK),
                     "problems": sum(1 for x in levels if x > OK)}
    return out


def _incidents(rt, since: float, now: float, names: dict, sources: dict) -> list[dict]:
    """Incidents of the period with more than one check, newest first, each with its checks in time order."""
    out = []
    for inc in rt.db.all("SELECT * FROM incidents WHERE site=? AND (started>? OR ended>? OR ended IS NULL) "
                         "ORDER BY started DESC LIMIT 50", (rt.cfg.site, since, since)):
        rows = rt.db.all("SELECT * FROM issues WHERE incident_id=? ORDER BY started, id", (inc["id"],))
        if len(rows) < 2 and not inc["cause"]:
            continue
        from . import causes

        evidence = [{"ts": ts, "device": device, "detail": detail, "exact": exact}
                    for ts, _module, device, detail, exact in causes.loads(inc["evidence"])]
        mods = []
        for r in rows:
            src = sources.get(r["check_key"].split(".", 1)[0], r["check_key"].split(".", 1)[0])
            if src not in mods:
                mods.append(src)
        out.append({"cause": inc["cause"], "evidence": evidence,
                    "started": inc["started"], "ended": inc["ended"], "worst": inc["worst"], "sources": mods,
                    "dur": (inc["ended"] or now) - inc["started"], "open": inc["ended"] is None,
                    "checks": [{"name": names.get(r["check_key"]) or r["name"] or r["check_key"],
                               "source": sources.get(r["check_key"].split(".", 1)[0], ""), "worst": r["worst"],
                               "started": r["started"], "resolved": r["resolved"], "detail": r["detail"],
                               "dur": (r["resolved"] or now) - r["started"]} for r in rows]})
    return out


async def _grafana_ctx(rt) -> dict:
    """The System page's Grafana and history box."""
    from . import grafana, updater
    vm = {}
    try:
        r = await rt.metrics.http.get(f"{rt.cfg.vm_url}/metrics", timeout=5)
        for line in r.text.splitlines():
            name, _, value = line.partition(" ")
            if name.startswith(("vm_data_size_bytes", "vm_free_disk_space_bytes")):
                key = name.split("{")[0]
                vm[key] = vm.get(key, 0) + float(value)
    except Exception:  # noqa: BLE001 - shown as unknown
        pass
    mine = []
    try:
        for n in sorted((n for n in os.listdir(rt.exports_dir) if grafana.MY_EXPORT.match(n)), reverse=True):
            st = os.stat(os.path.join(rt.exports_dir, n))
            mine.append({"name": n, "ts": st.st_mtime, "kb": max(1, round(st.st_size / 1024))})
    except FileNotFoundError:
        pass
    return {"grafana_on": await grafana.running(rt.notifier.http, rt.cfg.grafana_url),
            "grafana_ready": os.path.isdir(rt.cfg.grafana_dir), "features": updater.features(rt.cfg.update_dir),
            "retention": rt.cfg.retention, "retentions": updater.RETENTIONS, "vm_size": vm.get("vm_data_size_bytes"),
            "vm_free": vm.get("vm_free_disk_space_bytes"), "my_exports": mine, "ha_grafana": _ha_grafana(rt)}


def _exports(rt) -> list[dict]:
    from . import versions
    out = []
    for n in versions.list_exports(rt.exports_dir):
        st = os.stat(os.path.join(rt.exports_dir, n))
        out.append({"name": n, "ts": st.st_mtime, "kb": max(1, round(st.st_size / 1024))})
    return out


def _status_ctx(request: Request) -> dict:
    rt = rt_of(request)
    now = time.time()
    states = {r["check_key"]: r for r in rt.db.all("SELECT * FROM check_state WHERE site=?", (rt.cfg.site,))}
    tiles = []
    why_silent = rt.engine.silences(now)
    for r in rt.results:
        st_row = states.get(r.key)
        tiles.append({"r": r, "status": st_row["status"] if st_row else OK,
                      "pending": st_row["pending"] if st_row else None,
                      "silenced": why_silent(r.key)})
    overall = max((t["status"] for t in tiles), default=OK)
    groups = []
    for m in MODULES.values():
        mine = [t for t in tiles if t["r"].key.startswith(m.id + ".")]
        if mine:
            groups.append((m.id, m.name, mine))
    errors, warnings = rt.engine.counts()
    sp = MODULES["speedport"]
    info = {}
    for m in MODULES.values():
        mine = [t["r"] for t in tiles if t["r"].key.startswith(m.id + ".")]
        if mine:
            info[m.id] = {"observed": m.observed(mine), "last_good": m.reading_ts(rt) or rt.engine.last_observed(m.id)}
    switched_off = [m.name for m in MODULES.values() if m.is_set_up(rt) and not m.enabled(m.load(rt))]
    unknown = [m.name for m in MODULES.values() if m.id in info and not info[m.id]["observed"]]
    from . import grafana
    return dict(grafana=rt.grafana_on, boards=set(grafana.BUILDERS), module_info=info, switched_off=switched_off, unknown=unknown, tiles=tiles, tile_groups=groups, dsl=sp.enabled(sp.load(rt)), overall=overall, open=rt.engine.open_issues(), names=rt.names(), now=now,
                last_run=rt.last_run, last_error=rt.last_error, errors=errors, warnings=warnings,
                setup_done=rt.db.get("setup.done", False))


SPARK_SPAN, SPARK_STEP, SPARK_TTL = 86400, 300, 120  # the tiles' lines: 24 h in 5-minute steps, read at most every 2 min
# lines with a longer history than the check values (stored since 0.34.0): the probes and the modem's own readings
SPARK_HISTORY = {
    "internet.latency": 'min(avg_over_time(ping_average_response_ms{{site="{s}",role="public"}}[{w}s]))',
    "speedport.snr": 'last_over_time(hw_dsl_snr_down_db{{site="{s}"}}[20m])',
}


async def _sparks(rt) -> dict[str, list[float]]:
    """{check key: values over 24 h} for each module's headline check."""
    now = time.time()
    cached = getattr(rt, "spark_cache", None)
    if cached and now - cached[0] < SPARK_TTL:
        return cached[1]
    keys = [f"{m.id}.{m.headline[0]}" for m in MODULES.values() if m.headline[0]]
    out: dict[str, list[float]] = {}
    site = rt.cfg.site
    queries = [(None, f'avg_over_time(hw_check_value_value{{site="{site}",check=~"{"|".join(keys)}"}}[{SPARK_STEP}s])')]
    queries += [(k, q.format(s=site, w=SPARK_STEP)) for k, q in SPARK_HISTORY.items()]
    answers = await asyncio.gather(*(rt.metrics.query_range(q, now - SPARK_SPAN, now, SPARK_STEP) for _, q in queries),
                                   return_exceptions=True)
    for (key, _), rows in zip(queries, answers, strict=True):
        if isinstance(rows, Exception):  # the lines are a nicety; the page works without them
            log.debug("sparklines: %s", rows)
            continue
        for labels, values in rows:
            vals = [v for _, v in values]
            k = key or labels.get("check", "")
            if key or len(vals) > len(out.get(k, [])):  # the longer history wins
                out[k] = vals
    rt.spark_cache = (now, out)
    return out


def spark(values, w: int = 108, h: int = 34, pad: float = 3) -> dict | None:
    """SVG paths for a small line with its area, scaled to its own range; None with fewer than two points."""
    vals = [v for v in values or [] if isinstance(v, (int, float)) and math.isfinite(v)]
    if len(vals) < 2:
        return None
    lo, hi = min(vals), max(vals)
    pts = [(pad + i * (w - 2 * pad) / (len(vals) - 1),
            h / 2 if hi == lo else h - pad - (v - lo) / (hi - lo) * (h - 2 * pad)) for i, v in enumerate(vals)]
    line = " ".join(f"{'L' if i else 'M'}{x:.1f} {y:.1f}" for i, (x, y) in enumerate(pts))
    return {"line": line, "area": f"{line} L{w - pad:.1f} {h} L{pad:.1f} {h} Z", "x": round(pts[-1][0], 1),
            "y": round(pts[-1][1], 1), "w": w, "h": h}


def _fmt_value(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v:.1f}" if abs(v) < 10 and not float(v).is_integer() else f"{v:.0f}"


async def _status_full(request: Request) -> dict:
    """The Status page: the checks, plus the hero's numbers and each module tile's headline and 24 h line."""
    from .report import availability
    rt = rt_of(request)
    ctx = _status_ctx(request)
    now = ctx["now"]
    sparks = await _sparks(rt)
    results = {t["r"].key: t["r"] for t in ctx["tiles"]}
    cards = []
    for mod_id, mod_name, mod_tiles in ctx["tile_groups"]:
        m = MODULES[mod_id]
        worst = max(t["status"] for t in mod_tiles)
        key = f"{m.id}.{m.headline[0]}" if m.headline[0] else ""
        r = results.get(key)
        cards.append({"id": mod_id, "name": mod_name, "tiles": mod_tiles, "worst": worst, "icon": m.icon,
                      "value": _fmt_value(r.value if r else None), "unit": (r.unit if r else "") or "",
                      "label": m.headline[1], "spark": spark(sparks.get(key, [])),
                      "probs": [t for t in mod_tiles if t["status"] > 0]})
    cards.sort(key=lambda c: -c["worst"])  # problems first, in their usual order otherwise
    kpis = []
    inet = MODULES["internet"]
    if inet.enabled(inet.load(rt)):
        a = availability(rt.db, rt.cfg.site, now - 86400, now)
        if a.percent is not None:  # nothing watched yet: no number rather than a dash
            kpis.append((f"{a.percent:.2f}".rstrip("0").rstrip("."), "%", "Internet up, last 24 h"))
        st = rt.db.one("SELECT down, up FROM speedtests WHERE site=? AND ok=1 ORDER BY ts DESC LIMIT 1", (rt.cfg.site,))
        line_s = inet.load(rt).get("line") or {}
        cd, cu = line_s.get("contract_down") or 0, line_s.get("contract_up") or 0
        if st and st["down"] is not None:
            kpis.append((f"{st['down']:.1f}", f"/ {st['up'] or 0:.1f}",
                         "Last speed test, Mbit/s" + (f" (of {cd:g} / {cu:g})" if cd else "")))
        lat, loss = results.get("internet.latency"), results.get("internet.loss")
        if lat and lat.value is not None:
            kpis.append((f"{lat.value:.0f}", "ms", "Latency" + (f", {loss.value:.0f} % loss" if loss and loss.value is not None else "")))
    kpis.append((str(len(cards)), f"· {len(ctx['tiles'])} checks", "Modules watched"))
    full = SPARK_SPAN // SPARK_STEP * 0.9  # points a line has once it covers (nearly) 24 hours
    lines = {c["name"]: len(sparks.get(f"{c['id']}.{MODULES[c['id']].headline[0]}", [])) for c in cards if c["label"]}
    filling = {"missing": [n for n, k in lines.items() if k < 2], "partial": [n for n, k in lines.items() if 2 <= k < full]}
    names = set(filling["missing"]) | set(filling["partial"])
    seen = set(rt.db.get(f"status.filling_seen.{request.state.user.id}") or [])  # dismissed: show again only for new ones
    ctx.update(cards=cards, kpis=kpis, filling=filling if names - seen else None, filling_names=names, hero_line=spark(sparks.get("internet.latency", []), 600, 120, 0),
               home_name=rt.db.get("home.name") or "Home",
               attention=len(ctx["open"]) or sum(1 for t in ctx["tiles"] if t["status"] > 0))
    return ctx


def _release_notes(body: str) -> list[tuple[str, str]]:
    """GitHub's release notes (Markdown) as (kind, text) lines: h, li or p; plain text, nothing rendered as HTML."""
    out = []
    for raw in (body or "").splitlines():
        t = raw.strip()
        if not t:
            continue
        if t.startswith("#"):
            out.append(("h", t.lstrip("#").strip()))
        elif t[:2] in ("- ", "* "):
            out.append(("li", t[2:].strip()))
        elif raw.startswith("  ") and out and out[-1][0] == "li":  # a list item that goes on
            out[-1] = ("li", out[-1][1] + " " + t)
        else:
            out.append(("p", t))
    return out[:80]


def _channel_form(rt, ch, form: dict) -> tuple[dict, str]:
    """A channel's config from its form, with encrypted fields left empty kept as saved: (config, what is wrong)."""
    mod = PROVIDERS[ch["kind"]]
    saved = full_config(rt.secrets, ch)
    config = {k: form.get(k, "").strip() or (saved.get(k, "") if k in private_fields(ch["kind"]) or k not in form else "")
              for k, _, _ in mod.FIELDS}
    if hasattr(mod, "normalize"):
        config, err = mod.normalize(config)
        if err:
            return config, err
    if form.get("secret", "").strip() and hasattr(mod, "check_secret"):
        err = mod.check_secret(form["secret"])
        if err:
            return config, err
    return config, ""


def _equipment_choices(rt) -> list[dict]:
    """Wizard step 'Your equipment': every available module except the internet line (set up already)."""
    chosen = set(rt.db.get("setup.modules", []))
    out = []
    for mid, name, available in CATALOG:
        m = MODULES.get(mid)
        if not available or m is None or mid == "internet":
            continue
        first = (getattr(m, "description", "") or "").split(". ")[0].rstrip(".") + "."
        out.append({"id": mid, "name": name, "about": first, "ticked": mid in chosen,
                    "connected": bool(getattr(m, "configured", lambda _rt: False)(rt))})
    return out


def _wizard_ctx(rt, mid: str) -> dict | None:
    """Banner for a module page while the first-run wizard walks through the chosen modules."""
    if rt.db.get("setup.done", False):
        return None
    chosen = rt.db.get("setup.modules", [])
    if mid not in chosen:
        return None
    i = chosen.index(mid)
    nxt = chosen[i + 1] if i + 1 < len(chosen) else None
    names = {m: n for m, n, _ in CATALOG}
    return {"index": i + 1, "total": len(chosen), "name": names.get(mid, mid),
            "next_name": names.get(nxt, nxt) if nxt else None, "next_url": f"/setup/next-module?after={mid}"}


def _save_section(request: Request, section: str, form: dict, user: User) -> str:
    """Shared by the wizard and the settings pages. Returns an error message or ''."""
    rt = rt_of(request)
    if section == "equipment":
        chosen = [mid for mid, _n, available in CATALOG
                  if available and mid != "internet" and mid in MODULES and form.get(f"mod.{mid}") == "on"]
        rt.db.set("setup.modules", chosen)
        audit(rt.db, user.username, "setup", "equipment: " + (", ".join(chosen) or "none"))
        return ""
    if section == "home":
        name = form.get("name", "").strip()[:60]
        zone = form.get("tz", "UTC")
        if zone not in available_timezones():
            return "Unknown time zone"
        if "url" in form:
            url = form.get("url", "").strip().rstrip("/")
            if url and not url.startswith(("https://", "http://")):
                return "homeWatch address: start it with https://"
            rt.db.set("home.url", url)
        rt.db.set("home.name", name or "Home")
        rt.db.set("home.tz", zone)
        detail = f"name={name} tz={zone}"
        if "_updates" in form:  # the System page's Home box also holds the update check (not the wizard's)
            rt.db.set("updates.check", form.get("check") == "on")
            detail += f" update_check={form.get('check') == 'on'}"
        audit(rt.db, user.username, "home", detail)
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
        if getattr(mod, "SECRET_REQUIRED", False) and not secret:
            return f"{mod.TITLE}: enter the {mod.SECRET_LABEL.lower()}"
        if secret and hasattr(mod, "check_secret"):
            err = mod.check_secret(secret)
            if err:
                return err
        if hasattr(mod, "normalize"):
            config, err = mod.normalize(config)
            if err:
                return err
        config, private = split_config(kind, config)
        name = form.get(f"{kind}.name", "").strip()[:60] or mod.TITLE
        levels = [] if kind == "healthchecks" else [1, 2]
        cid = rt.db.run("INSERT INTO channels(kind,name,levels,config) VALUES(?,?,?,?)",
                        (kind, name, json.dumps(levels), json.dumps(config)))
        store_private(rt.secrets, cid, private)
        if secret:
            rt.secrets.put(channel_secret(cid), secret)
        audit(rt.db, user.username, "channel.add", f"{kind} {name}")
        return ""
    return "Unknown section"
