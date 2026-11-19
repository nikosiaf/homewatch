"""Read-only Proxmox VE API client (token auth, GET only)."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from urllib.parse import quote

import httpx

from ..apibase import why


class ApiError(Exception):
    pass


@dataclass
class Snapshot:
    """Everything the checks need from one read."""

    nodes: list[dict] = field(default_factory=list)
    guests: list[dict] = field(default_factory=list)
    storages: list[dict] = field(default_factory=list)
    disks: dict[str, list[dict]] = field(default_factory=dict)  # node -> disks
    backups: dict[int, dict] = field(default_factory=dict)  # vmid -> {"ok": ts|None, "failed": ts|None, "error": str}
    not_covered: set[int] = field(default_factory=set)  # guests no backup job includes
    # node -> pending package updates [{"package", "old", "new"}]; None when the token can't read them
    # (GET /nodes/{node}/apt/update needs Sys.Modify, which PVEAuditor lacks); a node is left out when the read failed
    updates: dict[str, list[dict] | None] = field(default_factory=dict)
    # node -> {"running": "7.0.14-22", "newest": "7.0.14-29"} from apt/versions (PVEAuditor is enough); None when
    # refused or no running kernel is listed; a node is left out when the read failed
    kernels: dict[str, dict | None] = field(default_factory=dict)


_KERNEL_PKG = re.compile(r"^(?:proxmox|pve)-kernel-(\d+\.\d+\.\d+-\d+)-pve(?:-signed)?$")


def kernel_key(v: str) -> tuple[int, ...]:
    """'7.0.14-29' (or '7.0.14-29-pve') -> (7, 0, 14, 29), for comparing kernel versions."""
    return tuple(int(x) for x in re.findall(r"\d+", v.removesuffix("-pve")))


def parse_kernels(versions: list[dict]) -> dict | None:
    """The running kernel and the newest installed one, from GET /nodes/{node}/apt/versions."""
    running = next((x.get("RunningKernel") for x in versions if x.get("RunningKernel")), None)
    if not running:
        return None
    running = running.removesuffix("-pve")
    installed = [m.group(1) for x in versions if (m := _KERNEL_PKG.match(x.get("Package", "")))
                 and x.get("CurrentState", "Installed") == "Installed"]
    newest = max(installed, key=kernel_key, default=running)
    return {"running": running, "newest": max(newest, running, key=kernel_key)}


_START = re.compile(r"Starting Backup of VM (\d+)")
_FAIL = re.compile(r"Backup of VM (\d+) failed - (.*)")
_DONE = re.compile(r"Finished Backup of VM (\d+)")


def parse_vzdump_log(lines: list[str]) -> dict[int, str]:
    """Per guest in a multi-guest backup task: 'ok' or the error text."""
    out: dict[int, str] = {}
    for line in lines:
        if m := _FAIL.search(line):
            out[int(m.group(1))] = m.group(2).strip()[:200]
        elif m := _DONE.search(line):
            if not out.get(int(m.group(1))):  # "" from its Starting line; a failure text stays
                out[int(m.group(1))] = "ok"
        elif m := _START.search(line):
            out.setdefault(int(m.group(1)), "")
    return {k: (v or "did not finish") for k, v in out.items()}


class Client:
    def __init__(self, url: str, token_id: str, secret: str, verify=False, timeout: float = 15):
        self.base = url.rstrip("/") + "/api2/json"
        self.http = httpx.AsyncClient(verify=verify, timeout=timeout,  # noqa: S501 - self-signed is the norm
                                      headers={"Authorization": f"PVEAPIToken={token_id}={secret}"})
        self.log_cache: dict[str, dict[int, str]] = {}

    async def get(self, path: str, optional: bool = False, **params):
        try:
            r = await self.http.get(self.base + path, params=params or None)
        except httpx.HTTPError as e:
            raise ApiError(f"can't reach Proxmox ({why(e)})") from None
        if r.status_code == 401:
            raise ApiError("Proxmox rejected the API token")
        if r.status_code == 403 and optional:
            return None
        if r.status_code == 403:
            raise ApiError(f"the token lacks permission for {path} (give it the PVEAuditor role on /)")
        if r.status_code >= 300:
            raise ApiError(f"Proxmox answered HTTP {r.status_code} for {path}")
        return r.json().get("data")

    async def snapshot(self, with_disks: bool = True, now: float | None = None) -> Snapshot:
        """with_disks also reads the pending package updates and kernels: they all change slowly."""
        now = now or time.time()
        s = Snapshot()
        s.nodes = await self.get("/nodes") or []
        s.guests = [g for g in (await self.get("/cluster/resources", type="vm") or []) if not g.get("template")]
        s.storages = await self.get("/cluster/resources", type="storage") or []
        try:
            s.not_covered = {int(g["vmid"]) for g in await self.get("/cluster/backup-info/not-backed-up") or []}
        except ApiError:
            pass
        for n in s.nodes:
            if n.get("status") != "online":
                continue
            if with_disks:
                try:
                    s.disks[n["node"]] = await self.get(f"/nodes/{n['node']}/disks/list") or []
                except ApiError:
                    s.disks[n["node"]] = []
                try:
                    s.updates[n["node"]] = await self._updates(n["node"])
                except ApiError:
                    pass
                try:
                    s.kernels[n["node"]] = parse_kernels(await self.get(f"/nodes/{n['node']}/apt/versions",
                                                                        optional=True) or [])
                except ApiError:
                    pass
            await self._backups(n["node"], s, now)
        return s

    async def _updates(self, node: str) -> list[dict] | None:
        """Pending updates from the node's apt cache (Proxmox refreshes it daily); this starts no apt run."""
        pkgs = await self.get(f"/nodes/{node}/apt/update", optional=True)
        if pkgs is None:
            return None
        return [{"package": x.get("Package", "?"), "old": x.get("OldVersion", ""), "new": x.get("Version", "")}
                for x in pkgs]

    async def _backups(self, node: str, s: Snapshot, now: float) -> None:
        tasks = await self.get(f"/nodes/{node}/tasks", typefilter="vzdump", source="all", limit=100,
                               since=int(now - 8 * 86400)) or []
        for t in tasks:
            if t.get("status") is None or "endtime" not in t:
                continue  # still running
            end = float(t["endtime"])
            if t.get("id"):  # a single guest: the task status is its result
                results = {int(t["id"]): "ok" if t["status"] == "OK" else t["status"]}
            else:  # a job over several guests: read its log once
                upid = t["upid"]
                if upid not in self.log_cache:
                    log = await self.get(f"/nodes/{node}/tasks/{quote(upid, safe='')}/log", limit=5000) or []
                    self.log_cache[upid] = parse_vzdump_log([x.get("t", "") for x in log])
                results = self.log_cache[upid]
            for vmid, res in results.items():
                b = s.backups.setdefault(vmid, {"ok": None, "failed": None, "error": ""})
                if res == "ok":
                    b["ok"] = max(b["ok"] or 0, end)
                elif (b["failed"] or 0) < end:
                    b["failed"], b["error"] = end, res

    async def close(self):
        await self.http.aclose()
