"""Local accounts (admin / viewer), sessions, CSRF tokens and login rate limiting."""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from .db import DB

SESSION_IDLE = 7 * 24 * 3600
MAX_FAILS = 5
LOCK_SECONDS = 300
ROLES = ("admin", "viewer")
MIN_PASSWORD = 10

_ph = PasswordHasher()
# a hash to verify against when the user doesn't exist, so timing doesn't reveal valid names
_DUMMY = _ph.hash("not-a-real-password")


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


class Auth:
    def __init__(self, db: DB):
        self.db = db
        self.fails: dict[str, list[float]] = {}

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

    def locked(self, client: str, now: float | None = None) -> bool:
        now = now or time.time()
        recent = [t for t in self.fails.get(client, []) if now - t < LOCK_SECONDS]
        self.fails[client] = recent
        return len(recent) >= MAX_FAILS

    def login(self, username: str, password: str, client: str) -> str:
        """Return a new session token, or raise AuthError."""
        if self.locked(client):
            raise AuthError("Too many failed attempts. Try again in a few minutes.")
        row = self.db.one("SELECT * FROM users WHERE username=?", (username.strip(),))
        try:
            _ph.verify(row["pw_hash"] if row else _DUMMY, password)
            if row is None:
                raise VerifyMismatchError
        except (VerifyMismatchError, InvalidHashError):
            self.fails.setdefault(client, []).append(time.time())
            raise AuthError("Wrong username or password") from None
        self.fails.pop(client, None)
        if _ph.check_needs_rehash(row["pw_hash"]):
            self.db.run("UPDATE users SET pw_hash=? WHERE id=?", (_ph.hash(password), row["id"]))
        token = secrets.token_urlsafe(32)
        now = time.time()
        self.db.run("INSERT INTO sessions(token,user_id,csrf,created,last_seen) VALUES(?,?,?,?,?)",
                    (token, row["id"], secrets.token_urlsafe(24), now, now))
        return token

    def session(self, token: str | None) -> User | None:
        if not token:
            return None
        now = time.time()
        row = self.db.one("SELECT s.csrf, s.last_seen, u.id, u.username, u.role FROM sessions s "
                          "JOIN users u ON u.id = s.user_id WHERE s.token=?", (token,))
        if row is None:
            return None
        if now - row["last_seen"] > SESSION_IDLE:
            self.logout(token)
            return None
        if now - row["last_seen"] > 60:
            self.db.run("UPDATE sessions SET last_seen=? WHERE token=?", (now, token))
        return User(row["id"], row["username"], row["role"], row["csrf"])

    def logout(self, token: str) -> None:
        self.db.run("DELETE FROM sessions WHERE token=?", (token,))


def audit(db: DB, username: str, action: str, detail: str = "") -> None:
    db.run("INSERT INTO audit(ts,username,action,detail) VALUES(?,?,?,?)", (time.time(), username, action, detail))
    from .versions import record  # every audited change may have changed the configuration

    record(db, username, action)
