"""The Proxmox module: settings, connection test, cached reads, page data."""

from __future__ import annotations

import time
from urllib.parse import urlsplit

from ...auth import audit
from ..pinning import PinnedTLS, host_port, verify_for
from .. import Module
from . import checks
from .client import ApiError, Client, Snapshot

KEY = "module.proxmox"
SECRET = "module.proxmox.token"
DISK_EVERY = 3600  # SMART data changes slowly; reading it calls smartctl on the host
DEFAULTS = {"enabled": True, "url": "", "token_id": "homewatch@pve!monitor", "verify": True, "watched": []}
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

    async def readings(self, rt, settings, now):
        await self.read(rt, settings, now)
        return checks.State(now=now, snap=self.snap, error=self.error, watched=settings.get("watched", []))

    def evaluate(self, settings, thresholds, readings):
        return checks.evaluate(settings, thresholds, readings)

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
        if form.get("remove"):
            return ""
        old = self.load(rt)
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
        # nothing is watched until the admin ticks guests and saves (none are pre-selected)
        watched = [] if first else sorted(
            int(k.split(".", 1)[1]) for k in form if k.startswith("watch.") and k.split(".", 1)[1].isdigit())
        new = {"enabled": True if first else form.get("enabled") == "on",
               "url": normalize_url(form.get("url", old["url"])) or old["url"],
               "token_id": form.get("token_id", old["token_id"]).strip(),
               "verify": form.get("verify") == "on",
               "watched": watched if watched is not None else old["watched"]}
        if not first and "url" not in form:  # fields greyed out (module off): keep them
            new.update(url=old["url"], token_id=old["token_id"], verify=old["verify"])
        rt.db.set(KEY, new)
        if form.get("secret", "").strip():
            rt.secrets.put(SECRET, form["secret"].strip())
        audit(rt.db, user.username, "proxmox", f"enabled={new['enabled']} url={new['url']} "
                                               f"token={new['token_id']} watched={new['watched']}")
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
                "setup_commands": SETUP_COMMANDS}


plugin = ProxmoxModule()
