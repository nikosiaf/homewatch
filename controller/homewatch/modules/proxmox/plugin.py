"""The Proxmox module: settings, connection test, cached reads, page data."""

from __future__ import annotations

import time
from urllib.parse import urlsplit

from ...auth import audit
from ...metrics import lines, num
from ..pinning import PinnedTLS, host_port, verify_for
from .. import DevEvent, Module, booted, forecast, forecast_result
from . import checks
from .client import ApiError, Client, Snapshot

KEY = "module.proxmox"
SECRET = "module.proxmox.token"
DISK_EVERY = 3600  # SMART data changes slowly; reading it calls smartctl on the host
DEFAULTS = {"enabled": True, "url": "", "token_id": "homewatch@pve!monitor", "verify": True, "watched": [], "every_min": 5}
EVERY_MAX = 1440
SETUP_COMMANDS = [
    'pveum user add homewatch@pve --comment "homewatch, read-only"',
    "pveum acl modify / --users homewatch@pve --roles PVEAuditor",
    "pveum user token add homewatch@pve monitor --privsep 0",
]


def normalize_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if url and "://" not in url:
        url = "https://" + url
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        return ""
    port = parts.port or 8006
    return f"https://{parts.hostname}:{port}"


class ProxmoxModule(PinnedTLS, Module):
    id = checks.MODULE
    name = "Proxmox VE"
    icon = "server"
    headline = ("cpu", "busiest node CPU")
    description = ("Watches a Proxmox VE host or cluster through a read-only API token: nodes, guests, storage, disk "
                   "health and backups. Nothing on Proxmox is changed.")
    setup = [
        ("text", "In the Proxmox shell (node → Shell), run:"),
        ("code", "\n".join(SETUP_COMMANDS)),
        ("text", "The last command prints the token's secret once: paste it into the module. The token can only read "
                 "(role PVEAuditor). Then tick the guests to watch."),
    ]
    checks = checks.CHECKS

    def __init__(self):
        self.client: Client | None = None
        self.client_key = None
        self.snap: Snapshot | None = None
        self.snap_ts = 0.0
        self.error = ""
        self.disks_ts = 0.0
        self.stale = False  # read again at the next run, whatever the time

    def every_s(self, s) -> float:
        try:
            return max(1.0, float(s.get("every_min") or DEFAULTS["every_min"])) * 60
        except (TypeError, ValueError):
            return DEFAULTS["every_min"] * 60

    def load(self, rt) -> dict:
        return {**DEFAULTS, **(rt.db.get(KEY) or {})}

    def configured(self, rt) -> bool:
        return rt.db.get(KEY) is not None

    def enabled(self, settings: dict) -> bool:
        return bool(settings.get("url")) and settings.get("enabled", True)

    async def _client(self, rt, s) -> Client:
        secret = rt.secrets.get(SECRET) or ""
        full = await self.pin_settings(rt, s)
        pin = self.pins(rt).get(host_port(s["url"]), {}).get("cert")
        key = (s["url"], s["token_id"], secret, s["verify"], pin)
        if self.client is None or self.client_key != key:
            if self.client:
                await self.client.close()
            self.client, self.client_key = Client(s["url"], s["token_id"], secret, verify_for(full, s["url"])), key
        return self.client

    async def read(self, rt, s, now: float) -> None:
        try:
            with_disks = now - self.disks_ts >= DISK_EVERY
            snap = await (await self._client(rt, s)).snapshot(with_disks=with_disks, now=now)
            if with_disks:
                self.disks_ts = now
            elif self.snap:
                snap.disks = self.snap.disks
            self.snap, self.snap_ts, self.error = snap, now, ""
        except ApiError as e:
            self.snap, self.error = None, str(e)
            await self.note_changes(rt, s, str(e))

    def device_events(self, rt, prev):
        out: list[DevEvent] = []
        if not self.snap:
            return out
        for n in self.snap.nodes:
            if n.get("status", "online") == "online" and n.get("uptime"):
                booted(prev, f"node:{n['node']}", self.snap_ts - float(n["uptime"]), n["node"], f"Proxmox host {n['node']}", out)
        return out

    def series(self, site: str) -> list[str]:
        snap, ts = self.snap, self.snap_ts
        if snap is None:
            return []
        pct = lambda v: None if num(v) is None else 100 * num(v)  # noqa: E731 - Proxmox reports cpu as 0..1
        out = lines("hw_proxmox_node", [
            ({"site": site, "node": n.get("node", "")},
             {"online": float(n.get("status") == "online"), "cpu_percent": pct(n.get("cpu")), "cpus": num(n.get("maxcpu")),
              "mem_used_bytes": num(n.get("mem")), "mem_total_bytes": num(n.get("maxmem")),
              "disk_used_bytes": num(n.get("disk")), "disk_total_bytes": num(n.get("maxdisk")),
              "uptime_seconds": num(n.get("uptime"))}) for n in snap.nodes], ts)
        out += lines("hw_proxmox_guest", [
            ({"site": site, "node": g.get("node", ""), "vmid": str(g.get("vmid", "")), "name": g.get("name", ""),
              "type": g.get("type", "")},
             {"running": float(g.get("status") == "running"), "cpu_percent": pct(g.get("cpu")), "cpus": num(g.get("maxcpu")),
              "mem_used_bytes": num(g.get("mem")), "mem_total_bytes": num(g.get("maxmem")),
              "disk_used_bytes": num(g.get("disk")) or None, "disk_total_bytes": num(g.get("maxdisk")),
              "net_in_bytes": num(g.get("netin")), "net_out_bytes": num(g.get("netout")),
              "disk_read_bytes": num(g.get("diskread")), "disk_write_bytes": num(g.get("diskwrite")),
              "uptime_seconds": num(g.get("uptime"))}) for g in snap.guests], ts)
        out += lines("hw_proxmox_storage", [
            ({"site": site, "node": st.get("node", ""), "storage": st.get("storage", ""), "type": st.get("plugintype", "")},
             {"active": float(st.get("status") == "available"), "used_bytes": num(st.get("disk")),
              "total_bytes": num(st.get("maxdisk"))}) for st in snap.storages], ts)
        out += lines("hw_proxmox_disk", [
            ({"site": site, "node": node, "disk": d.get("devpath", ""), "model": d.get("model", "")},
             {"healthy": float(str(d.get("health", "")).upper() in checks.HEALTHY),
              "life_left_percent": None if num(d.get("wearout")) is None else num(d.get("wearout")),
              "size_bytes": num(d.get("size"))})
            for node, disks in snap.disks.items() for d in disks], ts)
        out += lines("hw_proxmox_backup", [
            ({"site": site, "vmid": str(vmid)}, {"last_ok_timestamp": b.get("ok"), "last_failed_timestamp": b.get("failed")})
            for vmid, b in snap.backups.items()], ts)
        return out

    async def readings(self, rt, settings, now):
        if self.snap is None or self.stale or now - self.snap_ts >= self.every_s(settings):
            self.stale = False
            await self.read(rt, settings, now)
            if self.snap is not None:
                self.forecast = await forecast(rt, "hw_proxmox_storage_used_bytes", "hw_proxmox_storage_total_bytes",
                                               "storage")
        return checks.State(now=now, snap=self.snap, error=self.error, watched=settings.get("watched", []))

    forecast: dict | None = None

    def evaluate(self, settings, thresholds, readings):
        out = checks.evaluate(settings, thresholds, readings)
        if readings.snap is not None and thresholds.get("forecast") and thresholds["forecast"].enabled:
            out.append(forecast_result("proxmox.forecast", self.forecast, thresholds["forecast"]))
        return out

    async def test(self, rt, url: str, token_id: str, secret: str, verify: bool) -> tuple[Snapshot | None, str]:
        s = await self.pin_settings(rt, {"url": url, "verify": verify})
        c = Client(url, token_id, secret, verify_for(s, url))
        try:
            return await c.snapshot(with_disks=False), ""
        except ApiError as e:
            return None, str(e)
        finally:
            await c.close()

    async def prepare_save(self, rt, form: dict) -> str:
        """Before saving: remove, or test the connection with what was entered."""
        if form.get("remove") or form.get("_part") == "more":  # the guests box changes nothing of the connection
            return ""
        old = self.load(rt)
        try:
            every = float(form.get("every_min", old.get("every_min", DEFAULTS["every_min"])))
        except (TypeError, ValueError):
            every = 0
        if not 1 <= every <= EVERY_MAX:
            return f"Read every: from 1 to {EVERY_MAX} minutes"
        url = normalize_url(form.get("url", old["url"]))
        if not url:
            return "Proxmox address: e.g. https://192.168.1.10:8006"
        token_id = form.get("token_id", old["token_id"]).strip()
        if "!" not in token_id or "@" not in token_id:
            return "Token ID looks like user@realm!tokenname, e.g. homewatch@pve!monitor"
        secret = form.get("secret", "").strip() or rt.secrets.get(SECRET) or ""
        if not secret:
            return "Enter the token secret"
        if "enabled" in form or not self.configured(rt):
            snap, err = await self.test(rt, url, token_id, secret, form.get("verify") == "on")
            if err:
                await self.note_changes(rt, {"url": url}, err)
                return f"Nothing saved: {err}"
            self.snap, self.snap_ts, self.error = snap, time.time(), ""
        return ""

    def secret_keys(self) -> list[str]:
        return [SECRET]

    def forget_credentials(self, rt) -> None:
        super().forget_credentials(rt)
        self.forget_pins(rt)

    def imported(self, settings: dict) -> tuple[dict, str]:
        url = settings.get("url")
        url = normalize_url(url) if isinstance(url, str) else ""
        if not url:
            return settings, "address: an https:// address"
        if not isinstance(settings.get("verify", True), bool):
            return settings, "certificate check: must be true or false"
        if not all(isinstance(v, int) for v in settings.get("watched", [])):
            return settings, "watched guests: must be a list of numbers"
        every = settings.get("every_min", DEFAULTS["every_min"])
        if isinstance(every, bool) or not isinstance(every, (int, float)) or not 1 <= every <= EVERY_MAX:
            return settings, f"read every: from 1 to {EVERY_MAX} minutes"
        return settings | {"url": url}, ""

    def save(self, rt, form: dict, user) -> str:
        if form.get("remove"):
            rt.db.run("DELETE FROM settings WHERE key=?", (KEY,))
            self.forget_pins(rt)
            rt.secrets.delete(SECRET)
            self.client = self.snap = None
            audit(rt.db, user.username, "proxmox", "host removed")
            rt.wake.set()
            return ""
        old = self.load(rt)
        first = not self.configured(rt)
        if form.get("_part") == "more":  # the guests box: only the ticks
            new = {k: old[k] for k in DEFAULTS} | {"watched": sorted(
                int(k.split(".", 1)[1]) for k in form if k.startswith("watch.") and k.split(".", 1)[1].isdigit())}
            rt.db.set(KEY, new)
            audit(rt.db, user.username, "proxmox", f"watched={new['watched']}")
            self.stale = True
            rt.wake.set()
            return ""
        # nothing is watched until the admin ticks guests and saves (none are pre-selected)
        new = {"enabled": True if first else form.get("enabled") == "on",
               "url": normalize_url(form.get("url", old["url"])) or old["url"],
               "token_id": form.get("token_id", old["token_id"]).strip(),
               "verify": form.get("verify") == "on",
               "every_min": round(float(form.get("every_min", old["every_min"]))),
               "watched": [] if first else old["watched"]}
        if not first and "url" not in form:  # fields greyed out (module off): keep them
            new.update(url=old["url"], token_id=old["token_id"], verify=old["verify"], every_min=old["every_min"])
        rt.db.set(KEY, new)
        if form.get("secret", "").strip():
            rt.secrets.put(SECRET, form["secret"].strip())
        audit(rt.db, user.username, "proxmox", f"enabled={new['enabled']} url={new['url']} "
                                               f"token={new['token_id']} every={new['every_min']} min")
        rt.wake.set()
        return ""

    def page_ctx(self, rt) -> dict:
        s = self.load(rt)
        snap = self.snap
        guests = []
        if snap:
            for g in sorted(snap.guests, key=lambda g: int(g["vmid"])):
                v = int(g["vmid"])
                b = snap.backups.get(v, {})
                guests.append({**g, "vmid": v, "watched": v in s["watched"], "covered": v not in snap.not_covered,
                               "backup_ok": b.get("ok"), "backup_failed": b.get("failed")
                               if (b.get("failed") or 0) > (b.get("ok") or 0) else None,
                               "backup_error": b.get("error", "")})
        return {"cert_rows": self.cert_rows(rt), "configured": self.configured(rt), "has_secret": rt.secrets.has(SECRET), "snap": snap,
                "snap_ts": self.snap_ts, "pve_error": self.error, "guests": guests,
                "read_every": f"{max(self.every_s(s), 0) / 60:.0f} min",
                "setup_commands": SETUP_COMMANDS}


plugin = ProxmoxModule()
