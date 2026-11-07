"""Docker hosts: reads each host's Engine API through a read-only socket proxy (containers + info only)."""

from __future__ import annotations

import asyncio
import re

import httpx

from .. import ERROR, OK, WARNING, CheckDef, CheckResult, level
from ..apibase import why, ApiError, ApiModule, Choice, Field, Table
from ...metrics import lines

RESTART_WINDOW = 3600  # restarts are counted over the last hour
CHECKS = [
    CheckDef("api", "Hosts reachable", "api", help="ERROR when none of the Docker hosts can be read."),
    CheckDef("each", "Every host read", "api", help="ERROR when one of several Docker hosts can't be read."),
    CheckDef("stopped", "Containers running", "api",
             help="ERROR when a watched container is not running. Watched = it has a restart policy "
                  "(always, unless-stopped or on-failure) and is ticked under \"Containers to watch\"."),
    CheckDef("restarts", "Restarts", "api", warn=1, error=3, editable=("warn", "error"),
             help="Restarts of watched containers in the last hour (a crash loop), all hosts together."),
    CheckDef("unhealthy", "Health checks", "api",
             help="WARNING when a watched container's own health check reports unhealthy."),
    CheckDef("removed", "Containers removed", "api",
             help="WARNING when a watched container disappears from its host (seen since homeWatch started). "
                  "Save the module's page to accept a removal."),
]
PORT = 2375
_HOST = re.compile(r"^[a-zA-Z0-9.-]+(:\d{1,5})?$")
AUTO = ("always", "unless-stopped", "on-failure")


def hosts(value: str) -> list[str]:
    return [h if ":" in h else f"{h}:{PORT}" for h in re.split(r"[\s,]+", value or "") if h]


def short_image(image: str) -> str:
    """An image name for the table: a pinned digest shortened to its first 12 characters."""
    name, sep, digest = (image or "").partition("@sha256:")
    return f"{name}@sha256:{digest[:12]}" if sep and len(digest) > 12 else image


class DockerModule(ApiModule):
    id = "docker"
    name = "Docker hosts"
    icon = "box"
    headline = ("restarts", "restarts in the last hour")
    checks = CHECKS
    description = ("Watches containers on one or more Docker hosts through a read-only proxy in front of the Docker "
                   "socket: containers that stop, crash loops, failing health checks and removed containers. "
                   "Containers with a restart policy are watched; nothing on the hosts is changed.")
    setup = [
        ("text", "homeWatch never gets the Docker socket itself (that would be root on the host). On every host, run "
                 "a small proxy that only allows reading containers. Reading includes each container's settings and "
                 "environment variables (often passwords), so only homeWatch may reach it."),
        ("text", "On the host where homeWatch runs: no open port, the proxy joins homewatch's own network. "
                 "Enter socket-proxy as its address below."),
        ("code", "services:\n"
                 "  socket-proxy:\n"
                 "    image: lscr.io/linuxserver/socket-proxy:3.4.7-r0-ls104\n"
                 "    container_name: socket-proxy\n"
                 "    restart: unless-stopped\n"
                 "    environment: {CONTAINERS: 1, INFO: 1, POST: 0}   # read containers; nothing can be changed\n"
                 "    volumes: [\"/var/run/docker.sock:/var/run/docker.sock:ro\"]\n"
                 "    read_only: true\n"
                 "    tmpfs: [/run]\n"
                 "    networks: [homewatch]\n"
                 "networks:\n"
                 "  homewatch: {name: homewatch_default, external: true}"),
        ("text", "On other hosts: the same service with network_mode: host instead of networks (port 2375 on the host, "
                 "so the host's firewall applies; a published Docker port would bypass ufw). Then allow the port only "
                 "from homewatch:"),
        ("code", "# ufw (Ubuntu)\n"
                 "sudo ufw allow from <homewatch ip> to any port 2375 proto tcp\n"
                 "sudo ufw deny 2375/tcp\n"
                 "# nftables (Debian): in the input chain of /etc/nftables.conf, then: sudo systemctl reload nftables\n"
                 "tcp dport 2375 ip saddr != <homewatch ip> drop"),
        ("text", "Check from another machine that port 2375 is closed, then list the hosts below."),
    ]
    fields = [
        Field("hosts", "Docker hosts", "192.168.1.20, 192.168.1.21:2375",
              help=f"Addresses of the proxies, separated by commas; port {PORT} when none is given."),
    ]
    more_title = "Additional configuration"
    more_help = "After connecting: the containers homeWatch watches."
    filter_title = "Containers to watch"
    filter_help = ("Containers with a restart policy, per host, with their compose project. Unticked ones are not checked. "
                   "New containers with a restart policy are watched.")

    def __init__(self):
        super().__init__()
        self.restart_hist: dict[str, list[tuple[float, int]]] = {}  # per container: (time, restart count), last hour
        self.seen: dict[str, str] = {}  # watched containers seen since start: key -> label

    def validate(self, form, old):
        out, err = super().validate(form, old)
        if err:
            return out, err
        hs = [h for h in re.split(r"[\s,]+", out["hosts"]) if h]
        if not hs or not all(_HOST.match(h) for h in hs):
            return out, "Docker hosts: IP addresses or host names (optionally :port), separated by commas"
        return out | {"hosts": ", ".join(hs)}, ""

    async def fetch(self, s, secret, now):
        out, errors = [], {}
        for host in hosts(s["hosts"]):
            async with httpx.AsyncClient(timeout=10) as http:
                try:
                    out.append(await self._host(http, f"http://{host}", host))
                except ApiError as e:
                    errors[host] = str(e)
        if not out:
            raise ApiError("; ".join(f"{h}: {e}" for h, e in errors.items()) or "no hosts")
        n = sum(len(h["containers"]) for h in out)
        return {"hosts": out, "errors": errors,
                "_summary": f"{len(out)} host(s), {n} containers: " + ", ".join(h["name"] for h in out)}

    async def _host(self, http, base: str, host: str) -> dict:
        async def get(path):
            try:
                r = await http.get(f"{base}/{path}")
            except httpx.HTTPError as e:
                raise ApiError(f"can't reach the Docker proxy ({why(e)}); is it running and port open to homeWatch?") \
                    from None
            if r.status_code == 403:
                raise ApiError(f"the proxy refuses {path.split('?')[0]}: set CONTAINERS=1 and INFO=1")
            if r.status_code >= 300:
                raise ApiError(f"HTTP {r.status_code} for {path.split('?')[0]}")
            return r.json()

        info = await get("info")
        listed = await get("containers/json?all=1")
        gate = asyncio.Semaphore(6)

        async def inspect(c):
            async with gate:
                try:
                    return await get(f"containers/{c['Id']}/json")
                except ApiError:
                    return None  # removed between the list and the inspect

        details = await asyncio.gather(*(inspect(c) for c in listed))
        containers = []
        for c, d in zip(listed, details, strict=True):
            if d is None:
                continue
            st, hc = d.get("State") or {}, d.get("HostConfig") or {}
            labels = (d.get("Config") or {}).get("Labels") or {}
            containers.append({
                "name": (d.get("Name") or "").lstrip("/") or c["Id"][:12],
                "project": labels.get("com.docker.compose.project", ""),
                "image": (d.get("Config") or {}).get("Image") or c.get("Image", ""),
                "state": st.get("Status", ""),  # created, running, paused, restarting, removing, exited, dead
                "health": (st.get("Health") or {}).get("Status", ""),
                "exit": st.get("ExitCode"),
                "policy": (hc.get("RestartPolicy") or {}).get("Name") or "no",
                "restarts": int(d.get("RestartCount") or 0),
                "since": st.get("StartedAt", "") if st.get("Status") == "running" else st.get("FinishedAt", ""),
            })
        return {"host": host, "name": info.get("Name") or host, "version": info.get("ServerVersion", ""),
                "containers": sorted(containers, key=lambda c: (c["project"], c["name"]))}

    @staticmethod
    def _key(h, c):
        return f"{h['host']}/{c['name']}"

    def filters(self, snap):
        # one group per host; the compose project goes in the detail, so a project of one container needs no group
        return [Choice(self._key(h, c), c["name"],
                       " · ".join(x for x in (c["project"], "" if c["state"] == "running" else c["state"]) if x),
                       group=h["name"])
                for h in snap["hosts"] for c in h["containers"] if c["policy"] in AUTO]

    def load(self, rt) -> dict:
        s = super().load(rt)
        # before 0.28 the list was grouped per "host · project": such a skipped project becomes its containers
        old = {m for m in s.get("skipped", []) if m.startswith("group:") and " · " in m}
        if old and self.snap:
            mine = {self._key(h, c) for h in self.snap["hosts"] for c in h["containers"]
                    if f"group:{h['name']} · {c['project'] or 'no project'}" in old}
            s["skipped"] = sorted((set(s["skipped"]) - old) | mine)
        return s

    def _watched(self, s, snap):
        skipped = set(s.get("skipped", []))
        return [(h, c) for h in snap["hosts"] for c in h["containers"] if c["policy"] in AUTO
                and self._key(h, c) not in skipped and f"group:{h['name']}" not in skipped
                and f"group:{h['name']} · {c['project'] or 'no project'}" not in skipped]  # set before 0.28

    def series(self, site: str) -> list[str]:
        snap = self.snap
        if not snap:
            return []
        return lines("hw_docker_container", [
            ({"site": site, "host": h["name"], "project": c["project"], "container": c["name"], "image": c["image"]},
             {"running": float(c["state"] == "running"), "healthy": {"healthy": 1.0, "unhealthy": 0.0}.get(c["health"]),
              "restarts": float(c["restarts"])}) for h in snap["hosts"] for c in h["containers"]], self.snap_ts)

    def check(self, s, th, snap, now):
        out = []

        def add(cid, status, detail, value=None):
            dd = next(c for c in CHECKS if c.id == cid)
            out.append(CheckResult(f"{self.id}.{cid}", dd.name, status, detail, value, dd.unit))

        if snap["errors"]:
            add("each", ERROR, "Can't read " + "; ".join(f"{h}: {e}" for h, e in snap["errors"].items()))
        else:
            add("each", OK, f"all {len(snap['hosts'])} read")

        watched = self._watched(s, snap)
        multi = len(snap["hosts"]) > 1

        def label(h, c):
            return f"{h['name']}/{c['name']}" if multi else c["name"]

        down = [f"{label(h, c)} ({c['state']}" + (f", exit {c['exit']}" if c["state"] == "exited" else "") + ")"
                for h, c in watched if c["state"] not in ("running", "restarting")]
        add("stopped", ERROR if down else OK,
            "not running: " + ", ".join(down) if down else f"{len(watched)} watched containers running", len(down))

        total, worst, worst_n = 0, "", 0
        for h, c in watched:
            hist = self.restart_hist.setdefault(self._key(h, c), [])
            if hist and c["restarts"] < hist[-1][1]:
                hist.clear()  # recreated container: count from here
            hist.append((now, c["restarts"]))
            while len(hist) > 1 and hist[1][0] <= now - RESTART_WINDOW:
                hist.pop(0)
            n = c["restarts"] - hist[0][1]
            total += n
            if n > worst_n:
                worst, worst_n = label(h, c), n
        add("restarts", level(total, th["restarts"]) if total else OK,
            f"{total} restarts in the last hour, most by {worst}" if total else "no restarts in the last hour", total)

        sick = [label(h, c) for h, c in watched if c["health"] == "unhealthy"]
        checked = sum(1 for _, c in watched if c["health"])
        add("unhealthy", WARNING if sick else OK, "unhealthy: " + ", ".join(sick) if sick
            else f"{checked} health checks passing" if checked else "no watched container has a health check")

        read_hosts = {h["host"] for h in snap["hosts"]}
        now_keys = {self._key(h, c): label(h, c) for h, c in watched}
        gone = [lbl for k, lbl in self.seen.items() if k not in now_keys and k.split("/", 1)[0] in read_hosts
                and k not in {self._key(h, c) for h in snap["hosts"] for c in h["containers"]}]
        # forget unticked ones; keep removed ones until they come back or are unticked
        all_keys = {self._key(h, c) for h in snap["hosts"] for c in h["containers"]}
        self.seen = {k: v for k, v in self.seen.items() if k not in all_keys} | now_keys
        add("removed", WARNING if gone else OK, "removed: " + ", ".join(gone) if gone else "none removed", len(gone))
        return out

    def save(self, rt, form, user):
        self.seen = {}  # saving the page accepts removed containers
        return super().save(rt, form, user)

    def tables(self, snap, s):
        watched = {self._key(h, c) for h, c in self._watched(s, snap)}
        return [Table(f"Containers on {h['name']} (Docker {h['version']})",
                      ["Container", "Project", "State", "Health", "Restart policy", "Restarts", "Watched", "Image"],
                      [[c["name"], c["project"], c["state"], c["health"] or "-", c["policy"], c["restarts"],
                        "yes" if self._key(h, c) in watched else "no", short_image(c["image"])] for c in h["containers"]],
                      widths=["18%", "11%", "8%", "8%", "13%", "7%", "7%", "28%"], min_width=860)
                for h in snap["hosts"]]


plugin = DockerModule()
