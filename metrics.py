import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path


SECONDS_IN_WEEK = 7 * 24 * 60 * 60


@dataclass(frozen=True)
class LimitStatus:
    allowed: bool
    message: str | None = None


class BotDatabase:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    full_name TEXT,
                    first_seen_at INTEGER NOT NULL,
                    last_seen_at INTEGER NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS conversion_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    conversion_type TEXT NOT NULL,
                    input_bytes INTEGER NOT NULL DEFAULT 0,
                    output_bytes INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_conversion_events_user_time
                ON conversion_events(user_id, created_at)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_conversion_events_time
                ON conversion_events(created_at)
                """
            )

    def touch_user(
        self,
        user_id: int,
        username: str | None,
        full_name: str | None,
    ) -> None:
        now = int(time.time())
        with self._locked_connection() as connection:
            connection.execute(
                """
                INSERT INTO users(user_id, username, full_name, first_seen_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    full_name = excluded.full_name,
                    last_seen_at = excluded.last_seen_at
                """,
                (user_id, username, full_name, now, now),
            )

    def record_conversion(
        self,
        user_id: int,
        conversion_type: str,
        input_bytes: int | None,
        output_bytes: int | None,
        status: str,
    ) -> None:
        now = int(time.time())
        with self._locked_connection() as connection:
            connection.execute(
                """
                INSERT INTO conversion_events(
                    user_id,
                    conversion_type,
                    input_bytes,
                    output_bytes,
                    status,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    conversion_type,
                    int(input_bytes or 0),
                    int(output_bytes or 0),
                    status,
                    now,
                ),
            )

    def check_weekly_limit(
        self,
        user_id: int,
        incoming_bytes: int | None,
        weekly_file_limit: int,
        weekly_mb_limit: int,
        incoming_files: int = 1,
    ) -> LimitStatus:
        if weekly_file_limit <= 0 and weekly_mb_limit <= 0:
            return LimitStatus(True)

        stats = self.user_weekly_usage(user_id)
        used_files = int(stats["successful_conversions"])
        used_bytes = int(stats["input_bytes"])
        next_bytes = int(incoming_bytes or 0)

        if weekly_file_limit > 0 and used_files + incoming_files > weekly_file_limit:
            return LimitStatus(
                False,
                (
                    "Недельный лимит конвертаций исчерпан: "
                    f"{used_files}/{weekly_file_limit}. Эта операция добавит еще {incoming_files}. "
                    "Попробуй позже."
                ),
            )

        if weekly_mb_limit > 0:
            limit_bytes = weekly_mb_limit * 1024 * 1024
            if used_bytes + next_bytes > limit_bytes:
                used_mb = used_bytes / (1024 * 1024)
                next_mb = next_bytes / (1024 * 1024)
                return LimitStatus(
                    False,
                    (
                        "Недельный лимит объема исчерпан. "
                        f"Использовано: {used_mb:.1f} МБ, файл: {next_mb:.1f} МБ, "
                        f"лимит: {weekly_mb_limit} МБ."
                    ),
                )

        return LimitStatus(True)

    def user_weekly_usage(self, user_id: int) -> dict[str, int]:
        since = int(time.time()) - SECONDS_IN_WEEK
        with self._locked_connection() as connection:
            row = connection.execute(
                """
                SELECT
                    COUNT(*) AS successful_conversions,
                    COALESCE(SUM(input_bytes), 0) AS input_bytes,
                    COALESCE(SUM(output_bytes), 0) AS output_bytes
                FROM conversion_events
                WHERE user_id = ?
                  AND status = 'success'
                  AND created_at >= ?
                """,
                (user_id, since),
            ).fetchone()

        return {
            "successful_conversions": int(row["successful_conversions"]),
            "input_bytes": int(row["input_bytes"]),
            "output_bytes": int(row["output_bytes"]),
        }

    def admin_summary(self) -> dict[str, object]:
        now = int(time.time())
        since = now - SECONDS_IN_WEEK
        with self._locked_connection() as connection:
            users_total = connection.execute(
                "SELECT COUNT(*) AS value FROM users"
            ).fetchone()["value"]
            users_week = connection.execute(
                "SELECT COUNT(*) AS value FROM users WHERE last_seen_at >= ?",
                (since,),
            ).fetchone()["value"]
            events_total = connection.execute(
                "SELECT COUNT(*) AS value FROM conversion_events"
            ).fetchone()["value"]
            events_week = connection.execute(
                "SELECT COUNT(*) AS value FROM conversion_events WHERE created_at >= ?",
                (since,),
            ).fetchone()["value"]
            success_week = connection.execute(
                """
                SELECT COUNT(*) AS value
                FROM conversion_events
                WHERE created_at >= ? AND status = 'success'
                """,
                (since,),
            ).fetchone()["value"]
            failed_week = connection.execute(
                """
                SELECT COUNT(*) AS value
                FROM conversion_events
                WHERE created_at >= ? AND status = 'failed'
                """,
                (since,),
            ).fetchone()["value"]
            bytes_week = connection.execute(
                """
                SELECT
                    COALESCE(SUM(input_bytes), 0) AS input_bytes,
                    COALESCE(SUM(output_bytes), 0) AS output_bytes
                FROM conversion_events
                WHERE created_at >= ? AND status = 'success'
                """,
                (since,),
            ).fetchone()
            by_type = connection.execute(
                """
                SELECT conversion_type, COUNT(*) AS count
                FROM conversion_events
                WHERE created_at >= ? AND status = 'success'
                GROUP BY conversion_type
                ORDER BY count DESC
                """,
                (since,),
            ).fetchall()
            top_users = connection.execute(
                """
                SELECT
                    users.user_id,
                    users.username,
                    users.full_name,
                    COUNT(conversion_events.id) AS count,
                    COALESCE(SUM(conversion_events.input_bytes), 0) AS input_bytes
                FROM conversion_events
                LEFT JOIN users ON users.user_id = conversion_events.user_id
                WHERE conversion_events.created_at >= ?
                  AND conversion_events.status = 'success'
                GROUP BY conversion_events.user_id
                ORDER BY count DESC, input_bytes DESC
                LIMIT 5
                """,
                (since,),
            ).fetchall()

        return {
            "users_total": int(users_total),
            "users_week": int(users_week),
            "events_total": int(events_total),
            "events_week": int(events_week),
            "success_week": int(success_week),
            "failed_week": int(failed_week),
            "input_bytes_week": int(bytes_week["input_bytes"]),
            "output_bytes_week": int(bytes_week["output_bytes"]),
            "by_type": [dict(row) for row in by_type],
            "top_users": [dict(row) for row in top_users],
        }

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _locked_connection(self) -> "_LockedConnection":
        self._lock.acquire()
        connection = self._connect()
        return _LockedConnection(self._lock, connection)


class _LockedConnection:
    def __init__(self, lock: threading.Lock, connection: sqlite3.Connection) -> None:
        self.lock = lock
        self.connection = connection

    def __enter__(self) -> sqlite3.Connection:
        return self.connection

    def __exit__(self, exc_type, exc, traceback) -> None:
        try:
            if exc_type is None:
                self.connection.commit()
            else:
                self.connection.rollback()
            self.connection.close()
        finally:
            self.lock.release()
