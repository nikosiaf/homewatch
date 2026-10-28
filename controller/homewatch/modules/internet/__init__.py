"""Internet line module: public ping, gateway ping, DNS lookup, speed test."""

from __future__ import annotations

import copy
import ipaddress
import re

from .checks import CHECKS, MODULE  # noqa: F401

SETTINGS_KEY = f"module.{MODULE}"

DEFAULTS = {
    "public": {"enabled": True, "targets": ["1.1.1.1", "8.8.8.8"]},
    "gateway": {"enabled": True, "target": ""},
    "dns": {"enabled": True, "server": "", "domain": "cloudflare.com"},
    "speed": {"enabled": True, "engine": "cloudflare", "interval_h": 3, "ookla_accepted": False},
    # the line's contracted speed: used by the speed test and by modem modules, whatever is switched on
    "line": {"contract_down": 0, "contract_up": 0},
}

_HOST = re.compile(r"^(?=.{1,253}$)([a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)*[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$")


def valid_host(value: str) -> bool:
    """An IP address or a DNS name; nothing that could break out of the Telegraf config."""
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return bool(_HOST.match(value))


def load(db, gateway_hint: str = "", dns_hint: str = "") -> dict:
    s = copy.deepcopy(DEFAULTS)
    saved = db.get(SETTINGS_KEY) or {}
    for k, v in saved.items():
        if k in s and isinstance(v, dict):
            s[k].update(v)
    if "line" not in saved:  # settings saved before 0.1.0 kept the contract under "speed"
        old = saved.get("speed", {})
        s["line"] = {"contract_down": old.get("contract_down", 0), "contract_up": old.get("contract_up", 0)}
    for k in ("contract_down", "contract_up"):
        s["speed"].pop(k, None)
    if not saved:
        s["gateway"]["target"] = gateway_hint
        s["dns"]["server"] = dns_hint or gateway_hint
    return s


def save(db, settings: dict) -> None:
    db.set(SETTINGS_KEY, settings)
