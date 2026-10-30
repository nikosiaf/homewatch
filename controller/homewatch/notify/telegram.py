"""Telegram via the Bot API. Secret = bot token; config = {"chat_id": "..."}."""

from __future__ import annotations

import httpx

from .base import RateLimited

TITLE = "Telegram"
DESCRIPTION = ("Messages from your own Telegram bot. Create a bot with @BotFather to get its token, send the bot "
               "a message, then use your chat ID (for example from @userinfobot).")
LINK = "https://core.telegram.org/bots#how-do-i-create-a-bot"
FIELDS = [("chat_id", "Chat ID", "Your chat or group ID, e.g. 123456789 or -100…")]
SECRET_LABEL = "Bot token"
SECRET_REQUIRED = True
MAX = 4000
MAX_TEXT = MAX  # longer messages are sent as several


async def send(http: httpx.AsyncClient, config: dict, secret: str, level: int, title: str, body: str) -> None:
    if not secret or not config.get("chat_id"):
        raise RuntimeError("Telegram needs a bot token and a chat ID")
    text = f"{title}\n\n{body}"
    if len(text) > MAX:
        text = text[: MAX - 20] + "\n… (shortened)"
    try:
        r = await http.post(f"https://api.telegram.org/bot{secret}/sendMessage",
                            json={"chat_id": config["chat_id"], "text": text, "disable_web_page_preview": True})
    except httpx.HTTPError as e:
        # report the error type only: some httpx errors carry the request URL, which holds the token
        raise RuntimeError(f"Telegram unreachable ({type(e).__name__})") from None
    if r.status_code == 429:
        try:
            after = float(r.json().get("parameters", {}).get("retry_after", 60))
        except (ValueError, TypeError, AttributeError):
            after = 60.0
        raise RateLimited("Telegram asks to slow down", max(1.0, after))
    if r.status_code != 200:
        # never echo the URL: it holds the token
        try:
            desc = r.json().get("description", "")
        except ValueError:
            desc = ""
        raise RuntimeError(f"Telegram answered HTTP {r.status_code} {desc}".strip())
