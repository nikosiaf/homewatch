"""Home Assistant: long-lived token of a dedicated non-admin user; reads /api/config and /api/states."""

from __future__ import annotations

from datetime import datetime

import httpx

from .. import ERROR, OK, WARNING, CheckDef, CheckResult, level
from ..apibase import ApiError, ApiModule, Choice, Field, Table, https_url

CHECKS = [
    CheckDef("api", "HA reachable", "api", help="ERROR when homewatch can't read Home Assistant's API."),
    CheckDef("core", "Core", "api", help="WARNING when Home Assistant is starting, stopping or in safe / recovery mode."),
    CheckDef("unavailable", "Unavailable entities", "api", warn=1, error=10, editable=("warn", "error"),
             help="Entities unavailable for 30 minutes or more, except ignored ones and domains you don't watch."),
    CheckDef("updates", "Updates", "api", help="WARNING when updates are available (skipped versions don't count)."),
    CheckDef("backups", "Automatic backups", "api", unit="h", warn=26, error=50, editable=("warn", "error"),
             help="Age of the last successful automatic backup; ERROR when the last attempt failed."),
]
SETTLE = 30 * 60  # an entity counts as unavailable after this long (restarts and blips stay quiet)


def _domain(e) -> str:
    return e["entity_id"].split(".", 1)[0]


def _ts(value) -> float | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class HomeAssistantModule(ApiModule):
    id = "homeassistant"
    name = "Home Assistant"
    checks = CHECKS
    description = ("Watches Home Assistant through the token of a non-admin user: core state, entities that become "
                   "unavailable, available updates and automatic backups. Nothing in Home Assistant is changed.")
    setup = [
        ("text", "In Home Assistant: Settings → People → Users → Add user. Name it homewatch and leave "
                 "\"Administrator\" off."),
        ("text", "Log in as homewatch (a private browser window is easiest), open its profile → Security → "
                 "Long-lived access tokens → Create token, and paste the token below."),
    ]
    fields = [
        Field("url", "Home Assistant address", "http://homeassistant.local:8123"),
        Field("verify", "Check the HTTPS certificate", kind="check", default=False,
              help="Only matters for https addresses; leave off for self-signed certificates."),
    ]
    secret_label = "Long-lived access token"
    secret_placeholder = "eyJ0eXAiOiJKV1Qi…"
    filter_title = "Entities to watch"
    filter_help = ("Unavailable entities are reported only when they are ticked here, grouped by domain. New entities "
                   "are watched, except in a domain where you untick everything.")
    choices_title = "Unavailable entities to ignore"
    choices_help = ("These are unavailable now, grouped by domain. Ticked ones are ignored (on purpose, or long gone). "
                    "Entities that become unavailable later are reported; tick them here if that's expected.")

    def validate(self, form, old):
        out, err = super().validate(form, old)
        if err:
            return out, err
        url = https_url(out["url"], allow_http=True)
        if not url:
            return out, "Home Assistant address: e.g. http://homeassistant.local:8123"
        return out | {"url": url}, ""

    async def fetch(self, s, secret, now):
        async with httpx.AsyncClient(verify=bool(s.get("verify")), timeout=20,  # noqa: S501
                                     headers={"Authorization": f"Bearer {secret}"}) as http:
            async def get(path):
                try:
                    r = await http.get(f"{s['url']}/api/{path}")
                except httpx.HTTPError as e:
                    raise ApiError(f"can't reach Home Assistant ({type(e).__name__})") from None
                if r.status_code == 401:
                    raise ApiError("Home Assistant rejected the token")
                if r.status_code >= 300:
                    raise ApiError(f"Home Assistant answered HTTP {r.status_code} for /api/{path}")
                return r.json()

            config = await get("config")
            states = await get("states")
        keep = ("entity_id", "state", "last_changed")
        snap = {"config": {k: config.get(k) for k in ("version", "state", "safe_mode", "recovery_mode", "location_name")},
                "unavailable": [{k: e.get(k) for k in keep} | {"name": e.get("attributes", {}).get("friendly_name", "")}
                                for e in states if e.get("state") == "unavailable"],
                "updates": [{"entity_id": e["entity_id"], **{k: e.get("attributes", {}).get(k) for k in
                             ("title", "installed_version", "latest_version", "skipped_version")}}
                            for e in states if e["entity_id"].startswith("update.") and e.get("state") == "on"],
                "backup": {e["entity_id"].removeprefix("sensor.backup_"): e.get("state") for e in states
                           if e["entity_id"] in ("sensor.backup_last_successful_automatic_backup",
                                                 "sensor.backup_last_attempted_automatic_backup")},
                "entities": len(states),
                "all": sorted([e["entity_id"], e.get("attributes", {}).get("friendly_name") or ""] for e in states)}
        snap["_summary"] = f"Home Assistant {config.get('version', '?')}, {len(states)} entities"
        return snap

    def default_chosen(self, snap):
        return sorted(e["entity_id"] for e in snap["unavailable"])

    def choices(self, snap):
        return [Choice(e["entity_id"], e.get("name") or e["entity_id"].split(".", 1)[1],
                       e["entity_id"] if e.get("name") else "", group=_domain(e))
                for e in sorted(snap["unavailable"], key=lambda e: e["entity_id"])]

    def filters(self, snap):
        return [Choice(eid, name or eid.split(".", 1)[1], eid if name else "", group=eid.split(".", 1)[0])
                for eid, name in snap.get("all") or []]

    @staticmethod
    def _reported(s, snap):
        """Unavailable entities that count: not ignored and in a watched domain."""
        ignored, skipped = set(s.get("chosen", [])), set(s.get("skipped", []))
        return [e for e in snap["unavailable"] if e["entity_id"] not in ignored and e["entity_id"] not in skipped
                and f"group:{_domain(e)}" not in skipped]

    def check(self, s, th, snap, now):
        out = []

        def add(cid, status, detail, value=None):
            d = next(c for c in CHECKS if c.id == cid)
            out.append(CheckResult(f"{self.id}.{cid}", d.name, status, detail, value, d.unit))

        c = snap["config"]
        if c.get("safe_mode") or c.get("recovery_mode"):
            add("core", WARNING, f"{c.get('version')} is running in {'recovery' if c.get('recovery_mode') else 'safe'} mode")
        elif c.get("state") not in (None, "RUNNING"):
            add("core", WARNING, f"{c.get('version')} is {str(c.get('state')).lower()}")
        else:
            add("core", OK, f"{c.get('version')} running")

        new = [e for e in self._reported(s, snap) if (now - (_ts(e.get("last_changed")) or now)) >= SETTLE]
        n = len(new)
        names = ", ".join((e.get("name") or e["entity_id"]) for e in new[:5]) + (f" (+{n - 5} more)" if n > 5 else "")
        quiet = len(snap["unavailable"]) - len(self._reported(s, snap))
        add("unavailable", level(n, th["unavailable"]) if n else OK,
            f"{n} unavailable: {names}" if n else f"none (ignoring {quiet})", n)

        ups = [u for u in snap["updates"] if not u.get("skipped_version") or u["skipped_version"] != u.get("latest_version")]
        add("updates", WARNING if ups else OK,
            "; ".join(f"{u.get('title') or u['entity_id']} {u.get('installed_version')} → {u.get('latest_version')}"
                      for u in ups[:4]) + (f" (+{len(ups) - 4} more)" if len(ups) > 4 else "") if ups else "up to date",
            len(ups))

        ok_ts = _ts(snap["backup"].get("last_successful_automatic_backup"))
        try_ts = _ts(snap["backup"].get("last_attempted_automatic_backup"))
        if ok_ts is None and try_ts is None:
            add("backups", OK, "No automatic backups set up (no backup sensors)")
        elif try_ts and (ok_ts is None or try_ts - ok_ts > 3600):
            add("backups", ERROR, "The last automatic backup attempt failed"
                + (f"; last success {(now - ok_ts) / 3600:.0f} h ago" if ok_ts else ""))
        else:
            age = (now - ok_ts) / 3600
            add("backups", level(age, th["backups"]), f"last automatic backup {age:.0f} h ago", age)
        return out

    def tables(self, snap, s):
        return [Table("Updates available", ["Update", "Installed", "Latest"],
                      [[u.get("title") or u["entity_id"], u.get("installed_version"), u.get("latest_version")]
                       for u in snap["updates"]]),
                Table("Unavailable now, not ignored", ["Entity", "Name", "Since"],
                      [[e["entity_id"], e.get("name"), str(e.get("last_changed", ""))[:16].replace("T", " ")]
                       for e in self._reported(s, snap)])]


plugin = HomeAssistantModule()
