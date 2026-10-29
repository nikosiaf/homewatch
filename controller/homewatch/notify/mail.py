"""Email through any SMTP server. Secret = SMTP password; config = {"server", "username", "sender", "to"}.

Port 465 uses TLS from the start; any other port upgrades with STARTTLS. A password is never sent
over a connection that isn't encrypted.
"""

from __future__ import annotations

import asyncio
import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

import httpx

TITLE = "Email"
DESCRIPTION = ("Mail through an SMTP server: your provider's (e.g. smtp.gmail.com:587 with an app password) or a "
               "relay on your network. Port 465 uses TLS; other ports switch to TLS with STARTTLS.")
LINK = "https://support.google.com/accounts/answer/185833"
FIELDS = [("server", "SMTP server", "smtp.example.com:587"),
          ("username", "Username (empty: no login)", "you@example.com"),
          ("sender", "From", "homewatch@example.com"),
          ("to", "To (comma-separated)", "you@example.com, partner@example.com")]
SECRET_LABEL = "SMTP password (empty: no login)"
SECRET_REQUIRED = False
TIMEOUT = 20
_ADDR = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")


def normalize(config: dict) -> tuple[dict, str]:
    server = (config.get("server") or "").strip().removeprefix("smtp://").removeprefix("smtps://").rstrip("/")
    host, _, port = server.rpartition(":") if ":" in server else (server, "", "587")
    if not host or not port.isdigit() or not 0 < int(port) < 65536:
        return config, "Email: SMTP server as host:port, e.g. smtp.gmail.com:587"
    sender = (config.get("sender") or "").strip()
    to = [a.strip() for a in re.split(r"[,;]", config.get("to") or "") if a.strip()]
    if not _ADDR.match(sender):
        return config, "Email: the From address looks wrong"
    if not to or not all(_ADDR.match(a) for a in to):
        return config, "Email: enter at least one valid To address"
    return {"server": f"{host}:{port}", "username": (config.get("username") or "").strip(), "sender": sender,
            "to": ", ".join(to)}, ""


def build(config: dict, title: str, body: str) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = title
    msg["From"] = config["sender"]
    msg["To"] = config["to"]
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=config["sender"].rsplit("@", 1)[1])
    msg.set_content(body)
    return msg


def _send_sync(config: dict, secret: str, msg: EmailMessage) -> None:
    host, port = config["server"].rsplit(":", 1)
    port = int(port)
    ctx = ssl.create_default_context()
    try:
        if port == 465:
            smtp = smtplib.SMTP_SSL(host, port, timeout=TIMEOUT, context=ctx)
        else:
            smtp = smtplib.SMTP(host, port, timeout=TIMEOUT)
        with smtp:
            smtp.ehlo()
            if port != 465:
                if smtp.has_extn("starttls"):
                    smtp.starttls(context=ctx)
                    smtp.ehlo()
                elif config.get("username"):
                    raise RuntimeError("the server offers no STARTTLS; homewatch won't send the password unencrypted")
            if config.get("username"):
                smtp.login(config["username"], secret)
            smtp.send_message(msg)
    except smtplib.SMTPAuthenticationError:
        raise RuntimeError("the SMTP server rejected the username or password") from None
    except smtplib.SMTPRecipientsRefused:
        raise RuntimeError("the SMTP server refused the To address") from None
    except (smtplib.SMTPException, OSError) as e:
        raise RuntimeError(f"SMTP failed ({type(e).__name__}: {str(e)[:120]})") from None


async def send(http: httpx.AsyncClient, config: dict, secret: str, level: int, title: str, body: str) -> None:
    if not config.get("server") or not config.get("to"):
        raise RuntimeError("Email needs an SMTP server and a To address")
    await asyncio.to_thread(_send_sync, config, secret, build(config, title, body))
