"""SQLite storage: one file, numbered migrations, a few typed helpers."""

from __future__ import annotations

import json
import sqlite3
import threading
from typing import Any

MIGRATIONS: list[str] = [
    # 1: initial schema
    """
    CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE checks (
        site TEXT NOT NULL, check_key TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1, warn REAL, error REAL,
        PRIMARY KEY (site, check_key));
    CREATE TABLE secrets (name TEXT PRIMARY KEY, value BLOB NOT NULL);
    CREATE TABLE check_state (
        site TEXT NOT NULL, check_key TEXT NOT NULL,
        status INTEGER NOT NULL DEFAULT 0, pending INTEGER, pending_count INTEGER NOT NULL DEFAULT 0,
        pending_since REAL, since REAL, detail TEXT NOT NULL DEFAULT '', value REAL,
        PRIMARY KEY (site, check_key));
    CREATE TABLE issues (
        id INTEGER PRIMARY KEY, site TEXT NOT NULL, check_key TEXT NOT NULL,
        level INTEGER NOT NULL, worst INTEGER NOT NULL, started REAL NOT NULL, resolved REAL,
        detail TEXT NOT NULL DEFAULT '', resolved_detail TEXT NOT NULL DEFAULT '',
        notified INTEGER NOT NULL DEFAULT 0);
    CREATE INDEX issues_open ON issues (site, check_key, resolved);
    CREATE INDEX issues_started ON issues (started);
    CREATE TABLE snoozes (
        id INTEGER PRIMARY KEY, site TEXT NOT NULL, check_key TEXT NOT NULL,
        until REAL NOT NULL, reason TEXT NOT NULL DEFAULT '', created_by TEXT NOT NULL, created REAL NOT NULL);
    CREATE TABLE maintenance (
        id INTEGER PRIMARY KEY, site TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL,
        start_ts REAL, end_ts REAL, weekday INTEGER, start_min INTEGER, dur_min INTEGER,
        checks TEXT, created_by TEXT NOT NULL, created REAL NOT NULL);
    CREATE TABLE channels (
        id INTEGER PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
        levels TEXT NOT NULL DEFAULT '[1,2]', quiet_start TEXT, quiet_end TEXT,
        weekly_report INTEGER NOT NULL DEFAULT 0, config TEXT NOT NULL DEFAULT '{}');
    CREATE TABLE outbox (
        id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL, created REAL NOT NULL,
        level INTEGER NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0, next_try REAL NOT NULL, last_error TEXT NOT NULL DEFAULT '');
    CREATE TABLE users (
        id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE, pw_hash TEXT NOT NULL,
        role TEXT NOT NULL, created REAL NOT NULL);
    CREATE TABLE sessions (
        token TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        csrf TEXT NOT NULL, created REAL NOT NULL, last_seen REAL NOT NULL);
    CREATE TABLE audit (
        id INTEGER PRIMARY KEY, ts REAL NOT NULL, username TEXT NOT NULL, action TEXT NOT NULL,
        detail TEXT NOT NULL DEFAULT '');
    CREATE TABLE speedtests (
        id INTEGER PRIMARY KEY, site TEXT NOT NULL, ts REAL NOT NULL, engine TEXT NOT NULL,
        down REAL, up REAL, latency REAL, ok INTEGER NOT NULL, error TEXT NOT NULL DEFAULT '');
    CREATE INDEX speedtests_ts ON speedtests (site, ts);
    """,
]


class DB:
    """A single shared connection guarded by a lock; the app is small and mostly idle."""

    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.lock = threading.RLock()
        self.migrate()

    def migrate(self) -> None:
        with self.lock:
            version = self.conn.execute("PRAGMA user_version").fetchone()[0]
            for i, sql in enumerate(MIGRATIONS[version:], start=version + 1):
                self.conn.execute("BEGIN")
                try:
                    # statement by statement: executescript() would commit on its own
                    for stmt in _split(sql):
                        self.conn.execute(stmt)
                    self.conn.execute(f"PRAGMA user_version={i}")
                    self.conn.execute("COMMIT")
                except Exception:
                    self.conn.execute("ROLLBACK")
                    raise

    def all(self, sql: str, args: tuple | dict = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, args).fetchall()

    def one(self, sql: str, args: tuple | dict = ()) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute(sql, args).fetchone()

    def run(self, sql: str, args: tuple | dict = ()) -> int:
        with self.lock:
            cur = self.conn.execute(sql, args)
            return cur.lastrowid or cur.rowcount

    def tx(self):
        return _Tx(self)

    # settings: JSON values by key
    def get(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return default if row is None else json.loads(row["value"])

    def set(self, key: str, value: Any) -> None:
        self.run(
            "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )


class _Tx:
    def __init__(self, db: DB):
        self.db = db

    def __enter__(self):
        self.db.lock.acquire()
        self.db.conn.execute("BEGIN")
        return self.db

    def __exit__(self, exc_type, *_):
        try:
            self.db.conn.execute("ROLLBACK" if exc_type else "COMMIT")
        finally:
            self.db.lock.release()


def _split(sql: str) -> list[str]:
    return [s.strip() for s in sql.split(";") if s.strip()]
