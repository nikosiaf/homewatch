"""ntfy (ntfy.sh or self-hosted). Secret = optional access token; config = {"server", "topic"}."""

from __future__ import annotations

from urllib.parse import urlsplit

import httpx

TITLE = "ntfy"
DESCRIPTION = ("Push notifications through the free ntfy app (ntfy.sh or your own server). Pick a hard-to-guess "
               "topic, subscribe to it in the app, and paste the topic's full address here.")
LINK = "https://ntfy.sh"
FIELDS = [("url", "Topic URL", "https://ntfy.sh/your-hard-to-guess-topic")]
SECRET_LABEL = "Access token (optional, for protected topics)"
DEFAULT_SERVER = "https://ntfy.sh"
PRIVATE = ("url", "topic")  # a hard-to-guess topic is its own password
DESTINATION = ("server",)
PRIORITY = {0: "default", 1: "high", 2: "urgent"}
TAGS = {0: "white_check_mark", 1: "warning", 2: "rotating_light"}


def normalize(config: dict) -> tuple[dict, str]:
    """Turn the topic URL (or the older server + topic fields) into {url, server, topic}; return (config, error)."""
    url = (config.get("url") or "").strip()
    if not url and config.get("topic"):
        url = f"{(config.get('server') or DEFAULT_SERVER).rstrip('/')}/{config['topic'].strip('/')}"
    if url and "://" not in url:
        url = "https://" + url
    parts = urlsplit(url)
    path = parts.path.strip("/")
    if parts.scheme not in ("https", "http") or not parts.netloc:
        return config, "ntfy: enter the topic's full address, e.g. https://ntfy.sh/my-home-alerts"
    if not path:
        return config, "ntfy: the address needs a topic at the end, e.g. https://ntfy.sh/my-home-alerts"
    *prefix, topic = path.split("/")
    if len(topic) > 64:
        return config, "ntfy: the topic name can have up to 64 characters"
    server = f"{parts.scheme}://{parts.netloc}" + ("/" + "/".join(prefix) if prefix else "")
    return {"url": f"{server}/{topic}", "server": server, "topic": topic}, ""


def check_imported(config: dict) -> str:
    return normalize({"server": config.get("server") or DEFAULT_SERVER, "topic": "t"})[1]


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
