"""Settings that come from the environment (.env). Everything else lives in SQLite."""

from __future__ import annotations

import base64
import binascii
import os
from dataclasses import dataclass


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    data_dir: str
    vm_url: str
    master_key: bytes
    site: str
    telegraf_dir: str
    gateway_hint: str
    dns_hint: str
    secure_cookies: bool
    ookla_dir: str
    update_dir: str = "/update"  # shared with the host's updater (install.sh); missing = no updater

    @property
    def db_path(self) -> str:
        return os.path.join(self.data_dir, "homewatch.db")


def _master_key(raw: str) -> bytes:
    if not raw:
        raise ConfigError("HW_MASTER_KEY is not set; run install.sh or see the README")
    try:
        key = base64.b64decode(raw, validate=True)
    except binascii.Error as e:
        raise ConfigError("HW_MASTER_KEY is not valid base64") from e
    if len(key) != 32:
        raise ConfigError("HW_MASTER_KEY must decode to 32 bytes")
    return key


def load(env: dict[str, str] | None = None) -> Config:
    env = dict(os.environ if env is None else env)
    data_dir = env.get("HW_DATA_DIR", "/data")
    return Config(
        data_dir=data_dir,
        vm_url=env.get("HW_VM_URL", "http://victoriametrics:8428").rstrip("/"),
        master_key=_master_key(env.get("HW_MASTER_KEY", "")),
        site=env.get("HW_SITE", "home"),
        telegraf_dir=env.get("HW_TELEGRAF_DIR", "/telegraf"),
        gateway_hint=env.get("HW_GATEWAY_HINT", ""),
        dns_hint=env.get("HW_DNS_HINT", ""),
        secure_cookies=env.get("HW_SECURE_COOKIES", "1") != "0",
        ookla_dir=env.get("HW_OOKLA_DIR", os.path.join(data_dir, "ookla")),
        update_dir=env.get("HW_UPDATE_DIR", "/update"),
    )
