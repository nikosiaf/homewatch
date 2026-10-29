"""Shared base for modules that read a device over its API.

A module built on it declares its settings fields, setup steps and checks, and implements:
  fetch(settings, secret, now)        -> a snapshot (dict), or raise ApiError
  check(settings, thresholds, snap, now) -> list[CheckResult] for everything but the first check
  tables(snap)                         -> what the "Last reading" box shows
The base handles the rest: settings and secret storage, testing the connection before saving,
the Enabled switch, Remove, reading at most every `every` seconds, and the reachability check
(the module's first check), which suppresses all others while the device can't be read.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from ..auth import audit
from . import ERROR, OK, CheckDef, CheckResult, Module, Thresholds


class ApiError(Exception):
    pass


@dataclass
class Field:
    key: str
    label: str
    placeholder: str = ""
    kind: str = "text"  # text | number | check | url
    default: Any = ""
    help: str = ""


@dataclass
class Choice:
    value: str
    label: str
    detail: str = ""
    group: str = ""  # choices with a group are shown folded per group, with a "tick all" box


@dataclass
class Table:
    title: str
    head: list[str]
    rows: list[list[Any]] = field(default_factory=list)


def https_url(value: str, default_port: int | None = None, allow_http: bool = False) -> str:
    """Normalize "host", "host:port" or a URL to scheme://host[:port]; '' when invalid."""
    value = (value or "").strip().rstrip("/")
    if value and "://" not in value:
        value = "https://" + value
    p = urlsplit(value)
    if p.scheme not in (("https", "http") if allow_http else ("https",)) or not p.hostname:
        return ""
    port = p.port or default_port
    return f"{p.scheme}://{p.hostname}" + (f":{port}" if port else "")


class ApiModule(Module):
    description = ""
    setup: list[tuple[str, str]] = []  # ("text" | "code", content)
    fields: list[Field] = []
    secret_label = ""  # empty: no secret
    secret_placeholder = ""
    every = 0  # seconds between reads; 0 = every check run
    choices_title = ""  # e.g. "Ports that should be up"; empty: no choice list
    choices_help = ""
    filter_title = ""  # e.g. "Domains to watch": a list that is ticked by default; unticked values go to "skipped"
    filter_help = ""
    template = "module_generic.html"

    def __init__(self):
        self.snap: dict | None = None
        self.snap_ts = 0.0
        self.error = ""

    # ---- settings --------------------------------------------------------------------------
    @property
    def key(self) -> str:
        return f"module.{self.id}"

    @property
    def secret_key(self) -> str:
        return f"module.{self.id}.secret"

    def load(self, rt) -> dict:
        saved = rt.db.get(self.key)
        base = {"enabled": True, "chosen": [], "skipped": []} | {f.key: f.default for f in self.fields}
        return base | (saved or {}) | {"_configured": saved is not None}

    def configured(self, rt) -> bool:
        return rt.db.get(self.key) is not None

    def enabled(self, settings: dict) -> bool:
        return bool(settings.get("_configured", True)) and bool(settings.get("enabled"))

    def validate(self, form: dict, old: dict) -> tuple[dict, str]:
        """Turn the form into settings; override to normalize addresses. Returns (settings, error)."""
        out = {}
        for f in self.fields:
            raw = form.get(f.key, old.get(f.key, f.default))
            if f.kind == "check":
                out[f.key] = form.get(f.key) == "on" if f.key in form or "enabled" in form or "_new" in form else bool(raw)
            elif f.kind == "number":
                try:
                    out[f.key] = float(raw)
                except (TypeError, ValueError):
                    return {}, f"{f.label}: a number"
            else:
                out[f.key] = str(raw).strip()
        return out, ""

    # ---- reading ---------------------------------------------------------------------------
    async def fetch(self, settings: dict, secret: str, now: float) -> dict:
        raise NotImplementedError

    def check(self, settings: dict, th: dict[str, Thresholds], snap: dict, now: float) -> list[CheckResult]:
        raise NotImplementedError

    def tables(self, snap: dict, settings: dict) -> list[Table]:
        return []

    def choices(self, snap: dict) -> list[Choice]:
        return []

    def default_chosen(self, snap: dict) -> list[str]:
        return []

    def filters(self, snap: dict) -> list[Choice]:
        return []

    async def readings(self, rt, settings, now):
        if self.snap is None or now - self.snap_ts >= self.every:
            try:
                self.snap = await self.fetch(settings, rt.secrets.get(self.secret_key) or "", now)
                self.snap_ts, self.error = now, ""
            except ApiError as e:
                self.snap, self.error = None, str(e)
        return (settings, self.snap, self.error, now)

    def evaluate(self, settings, th, readings):
        settings, snap, error, now = readings
        first = self.checks[0]

        def result(d: CheckDef, status, detail, value=None, sup=None):
            return CheckResult(f"{self.id}.{d.id}", d.name, status, detail, value, d.unit,
                               f"{self.id}.{sup}" if sup else None)

        out = []
        if snap is None:
            if error:
                out.append(result(first, ERROR, f"Can't read {self.name}: {error}"))
                out += [result(d, OK, "no reading", sup=first.id) for d in self.checks[1:]]
            else:
                out.append(result(first, OK, "No reading yet"))
        else:
            out.append(result(first, OK, snap.get("_summary", "Read OK")))
            out += self.check(settings, th, snap, now)
        return [r for r in out if th[r.key.split(".", 1)[1]].enabled]

    # ---- saving ----------------------------------------------------------------------------
    async def prepare_save(self, rt, form: dict) -> str:
        if form.get("remove"):
            return ""
        old = self.load(rt)
        if not self.configured(rt):
            form["_new"] = "1"
        new, err = self.validate(form, old)
        if err:
            return err
        secret = form.get("secret", "").strip() or rt.secrets.get(self.secret_key) or ""
        if self.secret_label and not secret:
            return f"Enter the {self.secret_label.lower()}"
        turning_off = self.configured(rt) and "enabled" not in form
        if not turning_off:
            try:
                snap = await self.fetch(old | new, secret, time.time())
            except ApiError as e:
                return f"Nothing saved: {e}"
            self.snap, self.snap_ts, self.error = snap, time.time(), ""
            form["_default_chosen"] = json.dumps(self.default_chosen(snap))
        form["_validated"] = json.dumps(new)
        return ""

    def save(self, rt, form: dict, user) -> str:
        if form.get("remove"):
            rt.db.run("DELETE FROM settings WHERE key=?", (self.key,))
            rt.secrets.delete(self.secret_key)
            self.snap, self.error = None, ""
            audit(rt.db, user.username, self.id, "removed")
            rt.wake.set()
            return ""
        first = not self.configured(rt)
        old = self.load(rt)
        new = json.loads(form.get("_validated", "{}"))
        settings = old | new
        settings["enabled"] = True if first else form.get("enabled") == "on"
        if self.choices_title:
            if first:
                settings["chosen"] = json.loads(form.get("_default_chosen", "[]"))
            elif "enabled" in form:  # the list is part of the (not greyed-out) form
                settings["chosen"] = sorted(k.split(".", 1)[1] for k in form if k.startswith("choose."))
        if self.filter_title and not first and "enabled" in form:
            # values not on the page this time (e.g. a domain that has no entities now) keep their setting
            shown = set(json.loads(form.get("_filters_shown", "[]")))
            kept = {k.split(".", 1)[1] for k in form if k.startswith("keep.")}
            settings["skipped"] = sorted((set(old.get("skipped", [])) - shown) | (shown - kept))
        settings.pop("_configured", None)
        rt.db.set(self.key, settings)
        if form.get("secret", "").strip():
            rt.secrets.put(self.secret_key, form["secret"].strip())
        shown = {k: v for k, v in settings.items() if k not in ("chosen", "skipped")}
        audit(rt.db, user.username, self.id, json.dumps(shown) + (" secret=replaced" if form.get("secret") else ""))
        rt.wake.set()
        return ""

    def page_ctx(self, rt) -> dict:
        s = self.load(rt)
        snap = self.snap
        chosen, skipped = set(s.get("chosen", [])), set(s.get("skipped", []))
        filters = self.filters(snap) if snap and self.filter_title else []
        return {"filters": [(c, c.value not in skipped) for c in filters], "skipped": skipped,
                "filters_shown": json.dumps([c.value for c in filters]),"configured": self.configured(rt), "has_secret": rt.secrets.has(self.secret_key),
                "snap": snap, "snap_ts": self.snap_ts, "api_error": self.error,
                "tables": self.tables(snap, s) if snap else [],
                "choices": [(c, c.value in chosen) for c in self.choices(snap)] if snap and self.choices_title else []}
