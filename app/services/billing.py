"""Limits and paid operations.

How a user pays for an operation (one input file = one operation):
1. admins and users with active premium: free and unlimited;
2. otherwise the free weekly quota (FREE_WEEKLY_OPS) is used first;
3. then purchased credits.

Operations are *reserved* when a job is queued and *refunded* if the job fails
or is cancelled, so users never pay for errors.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass

from app.config import Product, Settings
from app.services.db import WEEK, Database


@dataclass(frozen=True)
class Balance:
    free_left: int
    free_total: int
    credits: int
    premium_until: int
    unlimited: bool

    @property
    def has_premium(self) -> bool:
        return self.premium_until > time.time()

    @property
    def available(self) -> int | None:
        """How many operations the user can run now (None = unlimited)."""
        if self.unlimited or self.has_premium:
            return None
        return self.free_left + self.credits


class LimitReached(Exception):
    def __init__(self, balance: Balance, needed: int) -> None:
        super().__init__("limit reached")
        self.balance = balance
        self.needed = needed


def _ensure_user(c: sqlite3.Connection, user_id: int) -> None:
    now = int(time.time())
    c.execute(
        "INSERT OR IGNORE INTO users(user_id, first_seen_at, last_seen_at) VALUES (?, ?, ?)",
        (user_id, now, now),
    )


class Billing:
    def __init__(self, db: Database, settings: Settings) -> None:
        self.db = db
        self.settings = settings

    def _unlimited(self, user_id: int) -> bool:
        return not self.settings.billing_enabled or self.settings.is_admin(user_id)

    def _balance(self, c: sqlite3.Connection, user_id: int) -> Balance:
        _ensure_user(c, user_id)
        row = c.execute("SELECT credits, premium_until FROM users WHERE user_id=?", (user_id,)).fetchone()
        used_free = c.execute(
            """SELECT COALESCE(SUM(units), 0) FROM usage
               WHERE user_id=? AND source='free' AND status!='refunded' AND created_at >= ?""",
            (user_id, int(time.time()) - WEEK),
        ).fetchone()[0]
        total = self.settings.free_weekly_ops
        return Balance(
            free_left=max(0, total - int(used_free)),
            free_total=total,
            credits=int(row["credits"]),
            premium_until=int(row["premium_until"]),
            unlimited=self._unlimited(user_id),
        )

    async def balance(self, user_id: int) -> Balance:
        return await self.db.run(self._balance, user_id)

    async def reserve(self, user_id: int, job_id: str, units: int) -> None:
        """Take `units` operations for a job or raise LimitReached."""

        def q(c: sqlite3.Connection) -> None:
            now = int(time.time())
            bal = self._balance(c, user_id)

            def add(source: str, amount: int) -> None:
                c.execute(
                    "INSERT INTO usage(user_id, job_id, units, source, status, created_at) "
                    "VALUES (?, ?, ?, ?, 'reserved', ?)",
                    (user_id, job_id, amount, source, now),
                )

            if units <= 0:
                return
            if bal.unlimited:
                add("admin" if self.settings.is_admin(user_id) else "unlimited", units)
                return
            if bal.has_premium:
                add("premium", units)
                return
            if bal.free_left + bal.credits < units:
                raise LimitReached(bal, units)
            from_free = min(bal.free_left, units)
            from_credits = units - from_free
            if from_free:
                add("free", from_free)
            if from_credits:
                c.execute("UPDATE users SET credits = credits - ? WHERE user_id=?", (from_credits, user_id))
                add("credits", from_credits)

        await self.db.run(q)

    async def settle(self, job_id: str, success: bool) -> None:
        """Mark a job's reservation as spent, or give it back."""

        def q(c: sqlite3.Connection) -> None:
            rows = c.execute(
                "SELECT id, user_id, units, source FROM usage WHERE job_id=? AND status='reserved'",
                (job_id,),
            ).fetchall()
            for row in rows:
                if success:
                    c.execute("UPDATE usage SET status='spent' WHERE id=?", (row["id"],))
                    continue
                c.execute("UPDATE usage SET status='refunded' WHERE id=?", (row["id"],))
                if row["source"] == "credits":
                    c.execute("UPDATE users SET credits = credits + ? WHERE user_id=?",
                              (row["units"], row["user_id"]))

        await self.db.run(q)

    async def apply_payment(self, user_id: int, charge_id: str, product: Product) -> bool:
        """Give the user what they paid for. Returns False if already applied."""

        def q(c: sqlite3.Connection) -> bool:
            _ensure_user(c, user_id)
            credits = product.amount if product.kind == "credits" else 0
            days = product.amount if product.kind == "premium" else 0
            try:
                c.execute(
                    "INSERT INTO payments(user_id, charge_id, product, stars, credits, days, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (user_id, charge_id, product.code, product.stars, credits, days, int(time.time())),
                )
            except sqlite3.IntegrityError:
                return False  # Telegram delivered the same payment twice
            if credits:
                c.execute("UPDATE users SET credits = credits + ? WHERE user_id=?", (credits, user_id))
            if days:
                now = int(time.time())
                c.execute(
                    "UPDATE users SET premium_until = MAX(premium_until, ?) + ? WHERE user_id=?",
                    (now, days * 86400, user_id),
                )
            return True

        return await self.db.run(q)

    async def mark_refunded(self, charge_id: str) -> int | None:
        """Take back what a refunded payment gave. Returns the user id."""

        def q(c: sqlite3.Connection) -> int | None:
            row = c.execute("SELECT * FROM payments WHERE charge_id=? AND refunded=0", (charge_id,)).fetchone()
            if row is None:
                return None
            c.execute("UPDATE payments SET refunded=1 WHERE id=?", (row["id"],))
            c.execute(
                "UPDATE users SET credits = MAX(0, credits - ?), premium_until = MAX(0, premium_until - ?) "
                "WHERE user_id=?",
                (row["credits"], row["days"] * 86400, row["user_id"]),
            )
            return int(row["user_id"])

        return await self.db.run(q)

    async def find_payment_user(self, charge_id: str) -> int | None:
        def q(c: sqlite3.Connection) -> int | None:
            row = c.execute("SELECT user_id FROM payments WHERE charge_id=?", (charge_id,)).fetchone()
            return int(row[0]) if row else None

        return await self.db.run(q)

    async def last_payments(self, user_id: int, limit: int = 5) -> list[dict]:
        def q(c: sqlite3.Connection) -> list[dict]:
            return [dict(r) for r in c.execute(
                "SELECT charge_id, product, stars, refunded, created_at FROM payments "
                "WHERE user_id=? ORDER BY id DESC LIMIT ?", (user_id, limit))]

        return await self.db.run(q)

    async def grant(self, user_id: int, credits: int) -> int:
        def q(c: sqlite3.Connection) -> int:
            _ensure_user(c, user_id)
            c.execute("UPDATE users SET credits = MAX(0, credits + ?) WHERE user_id=?", (credits, user_id))
            return int(c.execute("SELECT credits FROM users WHERE user_id=?", (user_id,)).fetchone()[0])

        return await self.db.run(q)

    def product(self, code: str) -> Product | None:
        return next((p for p in self.settings.products if p.code == code), None)
