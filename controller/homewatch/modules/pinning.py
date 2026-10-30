"""Trusting a device's self-signed certificate: the first one seen is pinned, and logins go only to that one.

The certificate's own dates and name are not checked, because the admin decided to trust this exact
certificate: an expired one keeps working, and only a different one is refused.
"""

from __future__ import annotations

import asyncio
import hashlib
import ssl
from urllib.parse import urlsplit

from ..auth import audit

CHANGED = "pinned certificate changed"


def fingerprint(pem: str) -> str:
    digest = hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest().upper()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


async def fetch_certificate(host: str, port: int = 443, timeout: float = 10) -> str:
    """The certificate presented now, as PEM. Unverified, and nothing else is sent."""
    return await asyncio.to_thread(ssl.get_server_certificate, (host, port), timeout=timeout)


def pinned_context(pem: str | None) -> ssl.SSLContext:
    """TLS that completes only with exactly this certificate; with None, with no certificate at all."""
    want = ssl.PEM_cert_to_DER_cert(pem) if pem else None

    class Pinned(ssl.SSLObject):
        def do_handshake(self):
            super().do_handshake()
            if self.getpeercert(binary_form=True) != want:  # before anything is sent on this connection
                raise ssl.SSLCertVerificationError(f"CERTIFICATE_VERIFY_FAILED: {CHANGED}")

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # the check is in do_handshake above
    ctx.sslobject_class = Pinned
    return ctx


def host_port(url: str) -> str:
    p = urlsplit(url if "://" in url else f"https://{url}")
    return f"{p.hostname}:{p.port or 443}"


def verify_for(settings: dict, url: str):
    """What to pass as httpx's verify= for this address. Without a pin it refuses: never an unchecked connection."""
    if settings.get("verify"):
        return True
    return settings.get("_tls", {}).get(host_port(url)) or pinned_context(None)


class PinnedTLS:
    """For modules that talk HTTPS to devices the admin may not have a trusted certificate for."""

    def tls_hosts(self, settings: dict) -> list[str]:
        """host:port of every HTTPS address the settings point at."""
        url = settings.get("url") or ""
        return [host_port(url)] if url.startswith("https://") else []

    def _pins_key(self) -> str:
        return f"tls.{self.id}"

    def pins(self, rt) -> dict:
        return rt.db.get(self._pins_key()) or {}

    async def pin_settings(self, rt, settings: dict) -> dict:
        """The settings plus "_tls": a context per pinned address. A new address gets its certificate pinned now."""
        if settings.get("verify"):
            return settings
        pins = self.pins(rt)
        out = {}
        for hp in self.tls_hosts(settings):
            if hp not in pins:
                host, _, port = hp.rpartition(":")
                try:
                    pem = await fetch_certificate(host, int(port))
                except (OSError, ValueError):
                    continue  # unreachable: nothing to pin, and verify_for refuses
                pins[hp] = {"cert": pem}
                rt.db.set(self._pins_key(), pins)
                audit(rt.db, "homewatch", self.id, f"trusting the certificate of {hp}: {fingerprint(pem)}")
            out[hp] = pinned_context(pins[hp]["cert"])
        return settings | {"_tls": out}

    async def note_changes(self, rt, settings: dict, error: str) -> None:
        """After a refused certificate: keep what is presented now, for the admin to compare and accept."""
        if CHANGED not in error and "certificate changed" not in error:
            return
        pins = self.pins(rt)
        for hp in self.tls_hosts(settings):
            if hp in pins:
                host, _, port = hp.rpartition(":")
                try:
                    pem = await fetch_certificate(host, int(port))
                except (OSError, ValueError):
                    continue
                if pem != pins[hp]["cert"]:
                    pins[hp]["new"] = pem
        rt.db.set(self._pins_key(), pins)

    def trust_certificate(self, rt, hp: str, fp: str, user) -> str:
        """Accept the changed certificate the admin was shown, and no other."""
        pins = self.pins(rt)
        new = pins.get(hp, {}).get("new")
        if not new or fingerprint(new) != fp:
            return "The certificate changed again, check the new fingerprint"
        pins[hp] = {"cert": new}
        rt.db.set(self._pins_key(), pins)
        audit(rt.db, user.username, self.id, f"trusting the new certificate of {hp}: {fp}")
        self.snap, self.snap_ts, self.error = None, 0.0, ""
        rt.wake.set()
        return ""

    def forget_pins(self, rt) -> None:
        rt.db.run("DELETE FROM settings WHERE key=?", (self._pins_key(),))

    def cert_rows(self, rt) -> list[dict]:
        return [{"host": hp, "fp": fingerprint(p["cert"]), "new_fp": fingerprint(p["new"]) if p.get("new") else ""}
                for hp, p in self.pins(rt).items()]

    def transport(self, rt) -> tuple[str, bool]:
        """How the connection is protected, and whether that is good enough."""
        s = self.load(rt)
        hosts = self.tls_hosts(s)
        if not hosts:
            return "Plain HTTP: credentials cross the network unencrypted", False
        if s.get("verify"):
            return "HTTPS, certificate checked", True
        pins = self.pins(rt)
        if all(hp in pins for hp in hosts):
            return "HTTPS, certificate pinned", True
        return "HTTPS, certificate not pinned yet", False
