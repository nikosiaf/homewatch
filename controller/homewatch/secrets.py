"""Secrets (tokens, passwords, URLs with keys) encrypted at rest with AES-GCM.

The key comes from HW_MASTER_KEY in .env. Secrets are write-only from the UI: once saved,
the page only shows whether one is set.
"""

from __future__ import annotations

import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .db import DB


class Secrets:
    def __init__(self, db: DB, key: bytes):
        self.db = db
        self.aes = AESGCM(key)

    def put(self, name: str, value: str) -> None:
        nonce = os.urandom(12)
        blob = nonce + self.aes.encrypt(nonce, value.encode(), name.encode())
        self.db.run(
            "INSERT INTO secrets(name,value) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value",
            (name, blob),
        )

    def get(self, name: str) -> str | None:
        row = self.db.one("SELECT value FROM secrets WHERE name=?", (name,))
        if row is None:
            return None
        blob = bytes(row["value"])
        return self.aes.decrypt(blob[:12], blob[12:], name.encode()).decode()

    def has(self, name: str) -> bool:
        return self.db.one("SELECT 1 FROM secrets WHERE name=?", (name,)) is not None

    def delete(self, name: str) -> None:
        self.db.run("DELETE FROM secrets WHERE name=?", (name,))
