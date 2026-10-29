"""Generic webhook: POST a JSON message. Secret = the URL (webhook URLs usually carry a token); config = {}.

The JSON has the fields most receivers look for: "text" (Slack, Mattermost, Rocket.Chat) and
"content" (Discord) hold the whole message; the rest is for your own automations.
"""

from __future__ import annotations

import time
from urllib.parse import urlsplit

import httpx

from ..modules import LEVEL_WORD

TITLE = "Webhook"
DESCRIPTION = ("POSTs each message as JSON to an address of yours: Slack, Discord or Mattermost incoming webhooks, "
               "a Home Assistant or Node-RED webhook, n8n… Fields: text, content, title, body, level, level_name, "
               "sent (Unix time).")
LINK = "https://github.com/nikosiaf/homeWatch#webhook"
FIELDS: list = []
SECRET_LABEL = "Webhook URL"
SECRET_REQUIRED = True
DISCORD_MAX = 2000


def check_secret(url: str) -> str:
    p = urlsplit(url.strip())
    if p.scheme not in ("https", "http") or not p.netloc:
        return "Webhook: enter the full address, e.g. https://hooks.slack.com/services/…"
    return ""


def payload(level: int, title: str, body: str, now: float | None = None) -> dict:
    text = f"{title}\n\n{body}"
    return {"text": text, "content": text[:DISCORD_MAX], "title": title, "body": body, "level": level,
            "level_name": LEVEL_WORD.get(level, str(level)), "sent": int(now or time.time())}


async def send(http: httpx.AsyncClient, config: dict, secret: str, level: int, title: str, body: str) -> None:
    if not secret or check_secret(secret):
        raise RuntimeError("Webhook needs its URL")
    try:
        r = await http.post(secret.strip(), json=payload(level, title, body))
    except httpx.HTTPError as e:
        # error type only: the URL (which may hold a token) must not end up in logs or on the page
        raise RuntimeError(f"webhook unreachable ({type(e).__name__})") from None
    if r.status_code >= 300:
        raise RuntimeError(f"webhook answered HTTP {r.status_code}")
