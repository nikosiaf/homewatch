"""Speed test engines.

cloudflare: no install, no account. Uses speed.cloudflare.com's public download/upload endpoints,
    4 parallel streams for a fixed time per direction.
ookla: the Ookla Speedtest CLI, which can't be bundled in an MIT package. The admin places the
    binary in the data folder and accepts Ookla's license in the web page.
"""

from __future__ import annotations

import asyncio
import json
import os
import statistics
import time
from dataclasses import dataclass

import httpx

CF = "https://speed.cloudflare.com"
STREAMS = 4
SECONDS = 8.0
CHUNK = 25_000_000  # download: streamed, cut off at the deadline
UP_CHUNK = 1_000_000  # upload: small, so a slow uplink finishes each piece quickly


@dataclass
class Result:
    ok: bool
    down: float | None = None  # Mbit/s
    up: float | None = None
    latency: float | None = None  # ms
    error: str = ""


async def cloudflare(seconds: float = SECONDS, streams: int = STREAMS) -> Result:
    limits = httpx.Limits(max_connections=streams + 1)
    async with httpx.AsyncClient(timeout=60, limits=limits, headers={"User-Agent": "homewatch"}) as http:
        try:
            lat = []
            for _ in range(5):
                t0 = time.perf_counter()
                (await http.get(f"{CF}/__down", params={"bytes": 0})).raise_for_status()
                lat.append((time.perf_counter() - t0) * 1000)
            down = await _run(http, seconds, streams, upload=False)
            up = await _run(http, seconds, streams, upload=True)
            return Result(True, down, up, statistics.median(lat))
        except (httpx.HTTPError, OSError) as e:
            return Result(False, error=f"{type(e).__name__}: {e}"[:200])


async def _run(http: httpx.AsyncClient, seconds: float, streams: int, upload: bool) -> float:
    total = 0
    payload = b"0" * UP_CHUNK
    start = time.perf_counter()
    deadline = start + seconds

    async def worker():
        nonlocal total
        while time.perf_counter() < deadline:
            if upload:
                (await http.post(f"{CF}/__up", content=payload)).raise_for_status()
                total += UP_CHUNK
            else:
                async with http.stream("GET", f"{CF}/__down", params={"bytes": CHUNK}) as r:
                    r.raise_for_status()
                    async for part in r.aiter_bytes():
                        total += len(part)
                        if time.perf_counter() >= deadline:
                            break

    await asyncio.gather(*(worker() for _ in range(streams)))
    elapsed = time.perf_counter() - start
    return total * 8 / elapsed / 1e6


OOKLA_VERSION = "1.2.0"
OOKLA_URL = "https://install.speedtest.net/app/cli/ookla-speedtest-{v}-linux-{arch}.tgz"
OOKLA_ARCH = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64", "armv7l": "armhf"}


def ookla_path(ookla_dir: str) -> str:
    return os.path.join(ookla_dir, "speedtest")


async def install_ookla(ookla_dir: str, http: httpx.AsyncClient | None = None) -> str:
    """Download the official Ookla CLI from Ookla's site into the data volume. Returns '' or an error.

    It is fetched by this install at the admin's request after they accept Ookla's license, not shipped
    with homeWatch (its license does not allow redistribution)."""
    import io
    import platform
    import tarfile

    arch = OOKLA_ARCH.get(platform.machine())
    if arch is None:
        return f"No Ookla CLI build for this processor ({platform.machine()})"
    url = OOKLA_URL.format(v=OOKLA_VERSION, arch=arch)
    own = http is None
    http = http or httpx.AsyncClient(timeout=60, follow_redirects=True)
    try:
        r = await http.get(url)
        r.raise_for_status()
    except httpx.HTTPError as e:
        return f"Download from Ookla failed ({type(e).__name__})"
    finally:
        if own:
            await http.aclose()
    try:
        with tarfile.open(fileobj=io.BytesIO(r.content), mode="r:gz") as tar:
            member = tar.getmember("speedtest")
            data = tar.extractfile(member).read()
    except (tarfile.TarError, KeyError) as e:
        return f"Unexpected download from Ookla: {e}"
    os.makedirs(ookla_dir, exist_ok=True)
    tmp = ookla_path(ookla_dir) + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.chmod(tmp, 0o755)
    os.replace(tmp, ookla_path(ookla_dir))
    return ""


async def ookla(ookla_dir: str) -> Result:
    path = ookla_path(ookla_dir)
    if not os.access(path, os.X_OK):
        return Result(False, error=f"Ookla CLI not found at {path}")
    proc = await asyncio.create_subprocess_exec(
        path, "--accept-license", "--accept-gdpr", "-f", "json",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=120)
    except TimeoutError:
        proc.kill()
        return Result(False, error="Ookla CLI timed out")
    if proc.returncode != 0:
        return Result(False, error=(err or out).decode(errors="replace").strip()[:200])
    return parse_ookla(out.decode())


def parse_ookla(text: str) -> Result:
    d = json.loads(text)
    return Result(
        True,
        down=d["download"]["bandwidth"] * 8 / 1e6,
        up=d["upload"]["bandwidth"] * 8 / 1e6,
        latency=d.get("ping", {}).get("latency"),
    )
