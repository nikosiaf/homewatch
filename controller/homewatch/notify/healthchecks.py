"""healthchecks.io heartbeat: homewatch pings a URL after every successful check run.

Not a message channel: healthchecks.io notices when the pings stop (host, power or internet down)
and alerts you through its own channels.
"""

from __future__ import annotations

from urllib.parse import urlsplit

import httpx

TITLE = "healthchecks.io heartbeat"
DESCRIPTION = ("Tells you when homewatch itself goes quiet. If this host, its power or your internet is down, "
               "homewatch can't send anything; healthchecks.io notices that its pings stopped and alerts you "
               "by email, app or chat. Create a free check with a 5-minute period and paste its ping URL.")
LINK = "https://healthchecks.io"
FIELDS = [("url", "Ping URL", "https://hc-ping.com/your-check-uuid")]
SECRET_LABEL = ""


def normalize(config: dict) -> tuple[dict, str]:
    url = (config.get("url") or "").strip()
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.netloc or not parts.path.strip("/"):
        return config, "healthchecks.io: paste the check's ping URL, e.g. https://hc-ping.com/…"
    return {"url": url}, ""


async def ping(http: httpx.AsyncClient, config: dict) -> None:
    try:
        r = await http.get(config["url"], timeout=10)
    except httpx.HTTPError as e:
        raise RuntimeError(f"healthchecks.io unreachable ({type(e).__name__})") from None
    if r.status_code >= 300:
        raise RuntimeError(f"healthchecks.io answered HTTP {r.status_code}")
