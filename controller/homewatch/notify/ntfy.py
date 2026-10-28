"""ntfy (ntfy.sh or self-hosted). Secret = optional access token; config = {"server", "topic"}."""

from __future__ import annotations

from urllib.parse import urlsplit

import httpx

FIELDS = [("server", "Server URL", "https://ntfy.sh or your own server"),
          ("topic", "Topic", "A hard-to-guess topic name")]
SECRET_LABEL = "Access token (optional)"
DEFAULT_SERVER = "https://ntfy.sh"
PRIORITY = {0: "default", 1: "high", 2: "urgent"}
TAGS = {0: "white_check_mark", 1: "warning", 2: "rotating_light"}


def normalize(config: dict) -> tuple[dict, str]:
    """Accept the usual ways people write a server and topic; return (config, error)."""
    server = (config.get("server") or "").strip()
    topic = (config.get("topic") or "").strip()
    if "://" in topic or ("/" in topic and "." in topic.split("/")[0]):  # a full topic link pasted into "topic"
        link = topic if "://" in topic else "https://" + topic
        parts = urlsplit(link)
        server, topic = f"{parts.scheme}://{parts.netloc}", parts.path.strip("/")
    if not server:
        server = DEFAULT_SERVER
    elif "://" not in server:
        server = "https://" + server
    parts = urlsplit(server)
    if parts.scheme not in ("https", "http") or not parts.netloc:
        return config, "ntfy server: a web address like https://ntfy.sh"
    topic = topic.strip("/")
    if not topic or "/" in topic or len(topic) > 64:
        return config, "ntfy topic: one word (letters, digits, - or _), up to 64 characters"
    return {**config, "server": f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}", "topic": topic}, ""


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
