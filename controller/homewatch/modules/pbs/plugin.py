"""Proxmox Backup Server: API token with the Audit role (read-only)."""

from __future__ import annotations

import re

import httpx

from .. import ERROR, OK, WARNING, CheckDef, CheckResult, level
from ..pinning import verify_for
from ..apibase import SELF_SIGNED, why, ApiError, ApiModule, Choice, Field, Table, https_url

CHECKS = [
    CheckDef("api", "PBS reachable", "api", help="ERROR when homewatch can't read the PBS API."),
    CheckDef("datastores", "Datastores", "api", unit="%", warn=85, error=95, editable=("warn", "error"),
             help="Fullest datastore; ERROR when a datastore reports an error (e.g. not mounted)."),
    CheckDef("backups", "Backups", "api", unit="h", warn=26, error=50, editable=("warn", "error"),
             help="Age of the newest backup of each VM, container or host you chose."),
    CheckDef("jobs", "Verify, prune, GC, sync", "api",
             help="ERROR when the latest run of a job type failed in the last 8 days; WARNING when it had warnings."),
    CheckDef("updates", "Updates", "api", help="WARNING when package updates are available on the PBS host."),
]
JOB_TYPES = {"verificationjob": "verify", "verify": "verify", "prunejob": "prune", "prune": "prune",
             "garbage_collection": "garbage collection", "syncjob": "sync"}
_TOKEN = re.compile(r"^[^@\s]+@[^!\s]+![A-Za-z0-9._-]+$")


class PBSModule(ApiModule):
    id = "pbs"
    name = "Proxmox Backup Server"
    checks = CHECKS
    every_min = 15  # backups run nightly
    description = ("Watches a Proxmox Backup Server through a read-only API token: datastore usage, the age of your "
                   "backups, verify / prune / garbage collection / sync results and package updates.")
    setup = [
        ("text", "On the PBS host (Administration → Shell), create a user with a token that can only read:"),
        ("code", "proxmox-backup-manager user create homewatch@pbs --comment \"homewatch, read-only\"\n"
                 "proxmox-backup-manager acl update / Audit --auth-id homewatch@pbs\n"
                 "proxmox-backup-manager user generate-token homewatch@pbs monitor\n"
                 "proxmox-backup-manager acl update / Audit --auth-id 'homewatch@pbs!monitor'"),
        ("text", "The third command prints the token's value once: paste it below as the token secret."),
    ]
    fields = [
        Field("url", "PBS address", "https://192.168.1.30:8007"),
        Field("token_id", "Token ID", "homewatch@pbs!monitor", default="homewatch@pbs!monitor"),
        Field("verify", "Check the HTTPS certificate", kind="check", default=True,
              help=SELF_SIGNED),
    ]
    secret_label = "Token secret"
    secret_placeholder = "xxxxxxxx-xxxx-…"
    more_title = "Additional configuration"
    more_help = "After connecting: the backups homewatch checks."
    choices_title = "VMs, containers and hosts to watch"
    choices_help = ("homewatch checks that each of these gets a fresh backup. When you connect, the ones backed up in "
                    "the last 26 hours are ticked; old backups of guests you no longer back up are left unticked.")

    def validate(self, form, old):
        out, err = super().validate(form, old)
        if err:
            return out, err
        url = https_url(out["url"], default_port=8007)
        if not url:
            return out, "PBS address: e.g. https://192.168.1.30:8007"
        if not _TOKEN.match(out["token_id"] or ""):
            return out, "Token ID looks like user@realm!tokenname, e.g. homewatch@pbs!monitor"
        return out | {"url": url}, ""

    async def fetch(self, s, secret, now):
        headers = {"Authorization": f"PBSAPIToken={s['token_id']}:{secret}"}
        async with httpx.AsyncClient(verify=verify_for(s, s["url"]), timeout=20, headers=headers) as http:  # noqa: S501
            async def get(path, optional=False, **params):
                try:
                    r = await http.get(f"{s['url']}/api2/json/{path}", params=params or None)
                except httpx.HTTPError as e:
                    raise ApiError(f"can't reach PBS ({why(e)})") from None
                if r.status_code == 401:
                    raise ApiError("PBS rejected the API token")
                if r.status_code in (403, 404, 501) and optional:
                    return None
                if r.status_code == 403:
                    raise ApiError(f"the token lacks permission for {path} (give user and token the Audit role on /)")
                if r.status_code >= 300:
                    raise ApiError(f"PBS answered HTTP {r.status_code} for {path}")
                return r.json().get("data")

            version = await get("version") or {}
            stores = await get("status/datastore-usage") or []
            groups = []
            for st in stores:
                # the guest's name is the comment (notes) of its newest snapshot, not of the group
                names: dict[tuple, tuple] = {}
                for x in await get(f"admin/datastore/{st['store']}/snapshots", optional=True) or []:
                    k = (x.get("backup-type"), str(x.get("backup-id")))
                    if x.get("comment") and (k not in names or x.get("backup-time", 0) > names[k][0]):
                        names[k] = (x.get("backup-time", 0), x["comment"].splitlines()[0].strip())
                for g in await get(f"admin/datastore/{st['store']}/groups", optional=True) or []:
                    k = (g.get("backup-type"), str(g.get("backup-id")))
                    groups.append({"store": st["store"], "type": k[0], "id": k[1],
                                   "last": g.get("last-backup"), "count": g.get("backup-count"),
                                   "comment": g.get("comment") or names.get(k, (0, ""))[1]})
            tasks = await get("nodes/localhost/tasks", optional=True, since=int(now - 8 * 86400), limit=500) or []
            updates = await get("nodes/localhost/apt/update", optional=True)
        snap = {"version": version.get("version", "?"),
                "stores": [{k: x.get(k) for k in ("store", "total", "used", "avail", "error", "estimated-full-date")}
                           for x in stores],
                "groups": groups,
                "jobs": [{"type": t.get("worker_type"), "id": t.get("worker_id"), "end": t.get("endtime"),
                          "status": t.get("status")} for t in tasks
                         if t.get("worker_type") in JOB_TYPES and t.get("status") is not None],
                "updates": None if updates is None else len(updates)}
        snap["_summary"] = f"PBS {snap['version']}, {len(stores)} datastore(s), {len(groups)} backup groups"
        return snap

    @staticmethod
    def _key(g):
        return f"{g['store']}/{g['type']}/{g['id']}"

    @staticmethod
    def _label(g):
        kind = {"vm": "VM", "ct": "Container", "host": "Host"}.get(g["type"], g["type"])
        return f"{kind} {g['id']}" + (f" · {g['comment']}" if g["comment"] else "")

    @staticmethod
    def _short(g):
        kind = {"vm": "VM", "ct": "CT"}.get(g["type"], g["type"])
        return f"{g['comment']} ({kind} {g['id']})" if g["comment"] else f"{kind} {g['id']}"

    def default_chosen(self, snap):
        import time
        fresh = time.time() - 26 * 3600  # guests left out of the backup job keep old snapshots; don't watch those
        return sorted(self._key(g) for g in snap["groups"] if (g.get("last") or 0) > fresh)

    def choices(self, snap):
        many = len({g["store"] for g in snap["groups"]}) > 1
        order = {"vm": 0, "ct": 1, "host": 2}
        rows = sorted(snap["groups"], key=lambda g: (g["store"], order.get(g["type"], 3), int(g["id"]) if g["id"].isdigit() else 0, g["id"]))
        return [Choice(self._key(g), self._label(g), f"datastore {g['store']}" if many else "") for g in rows]

    def check(self, s, th, snap, now):
        out = []

        def add(cid, status, detail, value=None):
            dd = next(c for c in CHECKS if c.id == cid)
            out.append(CheckResult(f"{self.id}.{cid}", dd.name, status, detail, value, dd.unit))

        broken = [f"{x['store']}: {x['error']}" for x in snap["stores"] if x.get("error")]
        fills = [(100 * x["used"] / x["total"], x["store"]) for x in snap["stores"] if x.get("total")]
        if broken:
            add("datastores", ERROR, "; ".join(broken))
        elif fills:
            p, name = max(fills)
            add("datastores", level(p, th["datastores"]), f"fullest {name} {p:.0f} %", p)
        else:
            add("datastores", OK, "No datastores")

        chosen = set(s.get("chosen", []))
        by_key = {self._key(g): g for g in snap["groups"]}
        problems, lvl, oldest = [], OK, 0.0
        for k in sorted(chosen):
            g = by_key.get(k)
            if g is None or not g.get("last"):
                problems.append(f"{k}: no snapshots")
                lvl = ERROR
                continue
            age = (now - g["last"]) / 3600
            oldest = max(oldest, age)
            g_lvl = level(age, th["backups"])
            if g_lvl:
                problems.append(f"{self._short(g)} {age:.0f} h old")
                lvl = max(lvl, g_lvl)
        if not chosen:
            add("backups", OK, "Nothing chosen to watch")
        elif problems:
            add("backups", lvl, "; ".join(problems), oldest)
        else:
            add("backups", OK, f"{len(chosen)} backed up, oldest {oldest:.0f} h ago", oldest)

        latest: dict[tuple, dict] = {}
        for j in snap["jobs"]:
            key = (JOB_TYPES[j["type"]], j.get("id") or "")
            if key not in latest or (j.get("end") or 0) > (latest[key].get("end") or 0):
                latest[key] = j
        failed = [f"{t} {i}".strip() + f": {j['status']}" for (t, i), j in latest.items()
                  if j["status"] != "OK" and not str(j["status"]).startswith("WARNINGS")]
        warned = [f"{t} {i}".strip() + f": {j['status']}" for (t, i), j in latest.items()
                  if str(j["status"]).startswith("WARNINGS")]
        if failed:
            add("jobs", ERROR, "; ".join(failed)[:300])
        elif warned:
            add("jobs", WARNING, "; ".join(warned)[:300])
        elif latest:
            add("jobs", OK, "latest runs OK: " + ", ".join(sorted({t for t, _ in latest})))
        else:
            add("jobs", OK, "no verify, prune, GC or sync runs in the last 8 days")

        if snap["updates"] is None:
            add("updates", OK, "not readable")
        else:
            add("updates", WARNING if snap["updates"] else OK,
                f"{snap['updates']} package updates available" if snap["updates"] else "up to date")
        return out

    def tables(self, snap, s):
        import time
        return [Table("Datastores", ["Datastore", "Used", "Full on"],
                      [[x["store"], f"{100 * x['used'] / x['total']:.0f} % of {x['total'] / 1e12:.1f} TB" if x.get("total") else x.get("error"),
                        time.strftime("%d %b %Y", time.localtime(x["estimated-full-date"]))
                        if (x.get("estimated-full-date") or 0) > 0 else None] for x in snap["stores"]]),
                Table("Backups", ["Guest", "Datastore", "Snapshots", "Newest"],
                      [[self._label(g), g["store"], g["count"],
                        time.strftime("%d %b %H:%M", time.localtime(g["last"])) if g.get("last") else None]
                       for g in sorted(snap["groups"], key=self._key)])]


plugin = PBSModule()
