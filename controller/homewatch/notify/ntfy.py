"""ntfy (ntfy.sh or self-hosted). Secret = optional access token; config = {"server", "topic"}."""

from __future__ import annotations

import httpx

FIELDS = [("server", "Server URL", "https://ntfy.sh or your own server"),
          ("topic", "Topic", "A hard-to-guess topic name")]
SECRET_LABEL = "Access token (optional)"
PRIORITY = {0: "default", 1: "high", 2: "urgent"}
TAGS = {0: "white_check_mark", 1: "warning", 2: "rotating_light"}


async def send(http: httpx.AsyncClient, config: dict, secret: str, level: int, title: str, body: str) -> None:
    server = (config.get("server") or "https://ntfy.sh").rstrip("/")
    topic = config.get("topic", "").strip("/")
    if not topic:
        raise RuntimeError("ntfy needs a topic")
    headers = {
        # header values must be latin-1: drop the leading emoji, the tag carries the icon
        "Title": title.encode("ascii", "ignore").decode().strip(),
        "Priority": PRIORITY.get(level, "default"),
        "Tags": TAGS.get(level, ""),
    }
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
    r = await http.post(f"{server}/{topic}", content=body.encode(), headers=headers)
    if r.status_code >= 300:
        raise RuntimeError(f"ntfy answered HTTP {r.status_code}")
