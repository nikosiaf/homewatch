"""Local accounts (admin / viewer), sessions, CSRF tokens and login rate limiting."""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import re
import time
from dataclasses import dataclass

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from .db import DB

SESSION_IDLE = 7 * 24 * 3600
MAX_FAILS = 5
LOCK_SECONDS = 300
MAX_ACCOUNT_FAILS = 20  # from any address; higher, so that one visitor can't easily lock the admin out
MAX_VERIFYING = 2  # Argon2 checks at the same time
MAX_WAITING = 8  # more than this at once are turned away rather than queued
ROLES = ("admin", "viewer")
MIN_PASSWORD = 10

_ph = PasswordHasher()
# a hash to verify against when the user doesn't exist, so timing doesn't reveal valid names
_DUMMY = _ph.hash("not-a-real-password")


def _token_hash(token: str) -> str:
    """Tokens are long and random, so a plain hash is enough: a copy of the database holds no usable session."""
    return hashlib.sha256(token.encode()).hexdigest()


def _matches(pw_hash: str, password: str) -> bool:
    try:
        return _ph.verify(pw_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


@dataclass
class User:
    id: int
    username: str
    role: str
    csrf: str = ""

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


class AuthError(Exception):
    pass


class Throttled(AuthError):
    pass


class Auth:
    def __init__(self, db: DB):
        self.db = db
        self.fails: dict[str, list[float]] = {}
        self.slots = asyncio.Semaphore(MAX_VERIFYING)
        self.waiting = 0

    def has_users(self) -> bool:
        return self.db.one("SELECT 1 FROM users LIMIT 1") is not None

    def create_user(self, username: str, password: str, role: str) -> int:
        username = username.strip()
        if not username or len(username) > 64 or not username.replace("-", "").replace("_", "").replace(".", "").isalnum():
            raise AuthError("Username: letters, digits, - _ . only")
        if role not in ROLES:
            raise AuthError("Unknown role")
        if len(password) < MIN_PASSWORD:
            raise AuthError(f"Password must have at least {MIN_PASSWORD} characters")
        if self.db.one("SELECT 1 FROM users WHERE username=?", (username,)):
            raise AuthError("That username exists already")
        return self.db.run("INSERT INTO users(username,pw_hash,role,created) VALUES(?,?,?,?)",
                           (username, _ph.hash(password), role, time.time()))

    def set_password(self, user_id: int, password: str) -> None:
        if len(password) < MIN_PASSWORD:
            raise AuthError(f"Password must have at least {MIN_PASSWORD} characters")
        self.db.run("UPDATE users SET pw_hash=? WHERE id=?", (_ph.hash(password), user_id))
        self.db.run("DELETE FROM sessions WHERE user_id=?", (user_id,))

    def locked(self, client: str, now: float | None = None, limit: int = MAX_FAILS) -> bool:
        now = now or time.time()
        recent = [t for t in self.fails.get(client, []) if now - t < LOCK_SECONDS]
        if recent:
            self.fails[client] = recent
        else:
            self.fails.pop(client, None)
        return len(recent) >= limit

    async def verify(self, username: str, password: str, client: str):
        """Check a password under the login limits; returns the user's row, or raises AuthError."""
        row = self.db.one("SELECT * FROM users WHERE username=?", (username.strip(),))
        # by user id: names match in any capitalization, so "ADMIN" must count against the same account
        account = f"account:{row['id']}" if row else ""
        if self.locked(client) or (account and self.locked(account, limit=MAX_ACCOUNT_FAILS)):
            raise Throttled("Too many failed attempts. Try again in a few minutes.")
        if self.waiting >= MAX_WAITING:
            raise Throttled("Too many logins at once. Try again in a moment.")
        # counted before the slow check, so that guesses sent in parallel can't all pass the limit above
        now = time.time()
        self.fails.setdefault(client, []).append(now)
        if account:  # only real accounts, so made-up names can't fill this up
            self.fails.setdefault(account, []).append(now)
        self.waiting += 1
        try:
            async with self.slots:  # Argon2 takes real CPU and memory: off the event loop, a few at a time
                ok = await asyncio.to_thread(_matches, row["pw_hash"] if row else _DUMMY, password)
                ok = ok and row is not None
                if ok and _ph.check_needs_rehash(row["pw_hash"]):
                    new = await asyncio.to_thread(_ph.hash, password)
                    self.db.run("UPDATE users SET pw_hash=? WHERE id=?", (new, row["id"]))
        finally:
            self.waiting -= 1
        if not ok:
            raise AuthError("Wrong username or password")
        self.fails.pop(client, None)
        if account:
            self.fails.pop(account, None)
        return row

    async def login(self, username: str, password: str, client: str) -> str:
        """Return a new session token, or raise AuthError."""
        row = await self.verify(username, password, client)
        token = secrets.token_urlsafe(32)
        now = time.time()
        self.db.run("INSERT INTO sessions(token,user_id,csrf,created,last_seen) VALUES(?,?,?,?,?)",
                    (_token_hash(token), row["id"], secrets.token_urlsafe(24), now, now))
        return token

    def session(self, token: str | None) -> User | None:
        if not token:
            return None
        now = time.time()
        row = self.db.one("SELECT s.csrf, s.last_seen, u.id, u.username, u.role FROM sessions s "
                          "JOIN users u ON u.id = s.user_id WHERE s.token=?", (_token_hash(token),))
        if row is None:
            return None
        if now - row["last_seen"] > SESSION_IDLE:
            self.logout(token)
            return None
        if now - row["last_seen"] > 60:
            self.db.run("UPDATE sessions SET last_seen=? WHERE token=?", (now, _token_hash(token)))
        return User(row["id"], row["username"], row["role"], row["csrf"])

    def logout(self, token: str) -> None:
        self.db.run("DELETE FROM sessions WHERE token=?", (_token_hash(token),))


_CREDENTIAL = re.compile(r'"(api_key|apikey|password|secret|token)": "[^"]*"')


def redact(detail: str) -> str:
    """Credentials in a settings dump never reach the audit log, whichever module wrote it."""
    return _CREDENTIAL.sub(r'"\1": "(hidden)"', detail)


def scrub_audit(db: DB) -> None:
    """Hide credentials in rows written before redact() existed."""
    for row in db.all("SELECT id, detail FROM audit"):
        clean = redact(row["detail"])
        if clean != row["detail"]:
            db.run("UPDATE audit SET detail=? WHERE id=?", (clean, row["id"]))


def audit(db: DB, username: str, action: str, detail: str = "") -> None:
    db.run("INSERT INTO audit(ts,username,action,detail) VALUES(?,?,?,?)", (time.time(), username, action, redact(detail)))
    from .versions import record  # every audited change may have changed the configuration

    record(db, username, action)
