"""SQLite storage: users, statistics, balance, payments and the job queue.

sqlite3 is synchronous, so every call runs in a worker thread and the bot's
event loop never blocks. One connection + a lock is plenty for a single bot.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, TypeVar

T = TypeVar("T")
WEEK = 7 * 24 * 3600

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id       INTEGER PRIMARY KEY,
    username      TEXT,
    full_name     TEXT,
    first_seen_at INTEGER NOT NULL,
    last_seen_at  INTEGER NOT NULL
);

-- One row per finished operation (kept compatible with the old bot's table).
CREATE TABLE IF NOT EXISTS conversion_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL,
    conversion_type TEXT NOT NULL,
    input_bytes     INTEGER NOT NULL DEFAULT 0,
    output_bytes    INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL,
    created_at      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_user_time ON conversion_events(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_events_time ON conversion_events(created_at);

-- Operations reserved/spent from the user's quota.
CREATE TABLE IF NOT EXISTS usage (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    job_id     TEXT NOT NULL,
    units      INTEGER NOT NULL,
    source     TEXT NOT NULL,        -- free | credits | premium | admin | unlimited
    status     TEXT NOT NULL,        -- reserved | spent | refunded
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_user_time ON usage(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_usage_job ON usage(job_id);

CREATE TABLE IF NOT EXISTS payments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    charge_id  TEXT NOT NULL UNIQUE,  -- telegram_payment_charge_id, needed for refunds
    product    TEXT NOT NULL,
    stars      INTEGER NOT NULL,
    credits    INTEGER NOT NULL DEFAULT 0,
    days       INTEGER NOT NULL DEFAULT 0,
    refunded   INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL,
    chat_id     INTEGER NOT NULL,
    kind        TEXT NOT NULL,
    payload     TEXT NOT NULL,
    cost        INTEGER NOT NULL,
    status      TEXT NOT NULL,        -- queued | running | done | failed | cancelled
    status_message_id INTEGER,
    error       TEXT,
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
"""

# Columns added to tables that may already exist in an old database.
MIGRATIONS = {
    "users": {
        "credits": "INTEGER NOT NULL DEFAULT 0",
        "premium_until": "INTEGER NOT NULL DEFAULT 0",
    },
}


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

    def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.executescript(SCHEMA)
        for table, columns in MIGRATIONS.items():
            existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            for column, definition in columns.items():
                if column not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        self._conn = conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _tx(self, fn: Callable[..., T], *args: Any) -> T:
        assert self._conn is not None, "Database.open() was not called"
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                result = fn(self._conn, *args)
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")
            return result

    async def run(self, fn: Callable[..., T], *args: Any) -> T:
        """Run `fn(connection, *args)` in one transaction, off the event loop."""
        return await asyncio.to_thread(self._tx, fn, *args)

    # --- users & statistics ------------------------------------------------

    async def touch_user(self, user_id: int, username: str | None, full_name: str | None) -> None:
        def q(c: sqlite3.Connection) -> None:
            now = int(time.time())
            c.execute(
                """INSERT INTO users(user_id, username, full_name, first_seen_at, last_seen_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,
                       full_name=excluded.full_name, last_seen_at=excluded.last_seen_at""",
                (user_id, username, full_name, now, now),
            )

        await self.run(q)

    async def record_event(self, user_id: int, kind: str, input_bytes: int, output_bytes: int, status: str) -> None:
        def q(c: sqlite3.Connection) -> None:
            c.execute(
                """INSERT INTO conversion_events(user_id, conversion_type, input_bytes,
                       output_bytes, status, created_at) VALUES (?, ?, ?, ?, ?, ?)""",
                (user_id, kind, int(input_bytes), int(output_bytes), status, int(time.time())),
            )

        await self.run(q)

    async def admin_summary(self) -> dict[str, Any]:
        def q(c: sqlite3.Connection) -> dict[str, Any]:
            since = int(time.time()) - WEEK

            def one(sql: str, *args: Any) -> Any:
                return c.execute(sql, args).fetchone()[0]

            return {
                "users_total": one("SELECT COUNT(*) FROM users"),
                "users_week": one("SELECT COUNT(*) FROM users WHERE last_seen_at >= ?", since),
                "success_week": one(
                    "SELECT COUNT(*) FROM conversion_events WHERE created_at >= ? AND status='success'", since),
                "failed_week": one(
                    "SELECT COUNT(*) FROM conversion_events WHERE created_at >= ? AND status='failed'", since),
                "input_bytes_week": one(
                    "SELECT COALESCE(SUM(input_bytes),0) FROM conversion_events WHERE created_at >= ?", since),
                "queued": one("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')"),
                "stars_total": one("SELECT COALESCE(SUM(stars),0) FROM payments WHERE refunded=0"),
                "stars_week": one(
                    "SELECT COALESCE(SUM(stars),0) FROM payments WHERE refunded=0 AND created_at >= ?", since),
                "by_type": [tuple(r) for r in c.execute(
                    """SELECT conversion_type, COUNT(*) AS n FROM conversion_events
                       WHERE created_at >= ? AND status='success'
                       GROUP BY conversion_type ORDER BY n DESC""", (since,))],
                "top_users": [dict(r) for r in c.execute(
                    """SELECT e.user_id, u.username, u.full_name, COUNT(*) AS n
                       FROM conversion_events e LEFT JOIN users u ON u.user_id = e.user_id
                       WHERE e.created_at >= ? AND e.status='success'
                       GROUP BY e.user_id ORDER BY n DESC LIMIT 5""", (since,))],
            }

        return await self.run(q)
