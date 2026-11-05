"""Job queue: every heavy operation goes through here.

* a fixed number of workers (QUEUE_WORKERS) => the server is never overloaded;
* users see their place in the queue and the current stage in one message,
  with a "Cancel" button;
* jobs are stored in SQLite, so after a restart unfinished jobs continue
  (Telegram file ids stay valid, the files are simply downloaded again).
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import BaseStorage, StorageKey
from aiogram.types import InlineKeyboardMarkup

from app import texts
from app.config import Settings
from app.converters import ConversionError, ToolMissingError
from app.models import IncomingFile, JobResult
from app.services import delivery
from app.services.billing import Billing
from app.services.db import Database

log = logging.getLogger(__name__)

POSITION_UPDATES = 10  # only the first N waiting jobs get "your place" updates


class TooManyJobs(Exception):
    pass


@dataclass
class Job:
    id: str
    user_id: int
    chat_id: int
    kind: str
    payload: dict[str, Any]
    cost: int
    status_message_id: int | None = None
    task: asyncio.Task | None = field(default=None, repr=False)
    cancelled: bool = False  # cancelled by the user
    stopping: bool = False  # the bot is shutting down; the job resumes on next start

    @property
    def title(self) -> str:
        return self.payload.get("title", "Задача")

    @property
    def files(self) -> list[IncomingFile]:
        return [IncomingFile.from_dict(f) for f in self.payload.get("files", [])]


class JobContext:
    """What a job handler gets: a temp folder, downloads, progress messages."""

    def __init__(self, queue: "JobQueue", job: Job, workdir: Path) -> None:
        self.queue = queue
        self.job = job
        self.workdir = workdir
        self.settings = queue.settings
        self.bot = queue.bot

    async def progress(self, stage: str) -> None:
        await self.queue.show_status(self.job, texts.job_running(self.job.title, stage), cancel=True)

    async def download(self, file: IncomingFile, prefix: str = "") -> Path:
        # Keep the original extension: converters rely on it.
        from app.formats import extension

        target = self.workdir / f"{prefix}in_{uuid.uuid4().hex[:8]}{extension(file.name) or '.bin'}"
        await self.bot.download(file.file_id, destination=target, timeout=600)
        if not target.exists():
            raise ConversionError("Не удалось скачать файл из Telegram")
        return target

    def state(self) -> FSMContext:
        key = StorageKey(bot_id=self.bot.id, chat_id=self.job.chat_id, user_id=self.job.user_id)
        return FSMContext(storage=self.queue.storage, key=key)


Handler = Callable[[JobContext], Awaitable[JobResult]]


class JobQueue:
    def __init__(
        self,
        bot: Bot,
        db: Database,
        billing: Billing,
        settings: Settings,
        storage: BaseStorage,
        handlers: dict[str, Handler],
        cancel_markup: Callable[[str], InlineKeyboardMarkup],
    ) -> None:
        self.bot = bot
        self.db = db
        self.billing = billing
        self.settings = settings
        self.storage = storage
        self.handlers = dict(handlers)
        self.cancel_markup = cancel_markup
        self._waiting: list[Job] = []
        self._running: dict[str, Job] = {}
        self._wakeup = asyncio.Condition()
        self._workers: list[asyncio.Task] = []

    # --- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        self.settings.work_dir.mkdir(parents=True, exist_ok=True)
        # Leftover job folders after a crash (only folders named like job ids).
        for child in self.settings.work_dir.iterdir():
            if child.is_dir() and len(child.name) == 16 and all(ch in "0123456789abcdef" for ch in child.name):
                shutil.rmtree(child, ignore_errors=True)

        for job in await self._load_unfinished():
            if job.kind not in self.handlers:
                await self._finish(job, "failed", "unknown job kind")
                continue
            self._waiting.append(job)
            await self._send_new_status(job, texts.job_resumed(job.title))
        if self._waiting:
            log.info("Resumed %d unfinished jobs", len(self._waiting))

        self._workers = [asyncio.create_task(self._worker(i)) for i in range(self.settings.queue_workers)]

    async def stop(self) -> None:
        for worker in self._workers:
            worker.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        # Jobs that were running stay "running" in the DB and resume on next start.

    # --- public API ----------------------------------------------------------

    def active_count(self, user_id: int) -> int:
        jobs = [*self._waiting, *self._running.values()]
        return sum(1 for job in jobs if job.user_id == user_id)

    async def submit(self, user_id: int, chat_id: int, kind: str, payload: dict[str, Any], cost: int) -> Job:
        """Queue a job. Raises TooManyJobs or billing.LimitReached."""
        if kind not in self.handlers:
            raise ValueError(f"unknown job kind {kind}")
        if self.active_count(user_id) >= self.settings.user_max_jobs and not self.settings.is_admin(user_id):
            raise TooManyJobs()

        job = Job(uuid.uuid4().hex[:16], user_id, chat_id, kind, payload, cost)
        await self.billing.reserve(user_id, job.id, cost)
        await self.db.run(self._insert, job)
        position = len(self._waiting) + 1
        await self._send_new_status(job, texts.job_queued(job.title, position, len(self._running)))
        async with self._wakeup:
            self._waiting.append(job)
            self._wakeup.notify()
        return job

    async def cancel(self, job_id: str, user_id: int) -> bool:
        job = self._running.get(job_id) or next((j for j in self._waiting if j.id == job_id), None)
        if job is None or (job.user_id != user_id and not self.settings.is_admin(user_id)):
            return False
        job.cancelled = True
        if job in self._waiting:
            self._waiting.remove(job)
            await self._finish(job, "cancelled")
            await self.show_status(job, texts.job_cancelled(job.title))
            await self._update_positions()
        elif job.task is not None:
            job.task.cancel()  # the worker finishes the bookkeeping
        return True

    # --- status message ------------------------------------------------------

    async def _send_new_status(self, job: Job, text: str) -> None:
        try:
            message = await self.bot.send_message(job.chat_id, text, reply_markup=self.cancel_markup(job.id))
        except (TelegramBadRequest, TelegramForbiddenError):
            return
        job.status_message_id = message.message_id
        await self.db.run(
            lambda c: c.execute("UPDATE jobs SET status_message_id=? WHERE id=?", (job.status_message_id, job.id))
        )

    async def show_status(self, job: Job, text: str, cancel: bool = False) -> None:
        if job.status_message_id is None:
            return
        try:
            await self.bot.edit_message_text(
                text=text,
                chat_id=job.chat_id,
                message_id=job.status_message_id,
                reply_markup=self.cancel_markup(job.id) if cancel else None,
            )
        except TelegramBadRequest as exc:  # "message is not modified" etc.
            if "not modified" not in str(exc):
                log.debug("Status update failed: %s", exc)
        except TelegramForbiddenError:
            pass

    async def _delete_status(self, job: Job) -> None:
        if job.status_message_id is None:
            return
        try:
            await self.bot.delete_message(job.chat_id, job.status_message_id)
        except (TelegramBadRequest, TelegramForbiddenError):
            pass

    async def _update_positions(self) -> None:
        for index, job in enumerate(self._waiting[:POSITION_UPDATES], start=1):
            await self.show_status(job, texts.job_queued(job.title, index, len(self._running)), cancel=True)

    # --- workers -------------------------------------------------------------

    async def _worker(self, number: int) -> None:
        while True:
            async with self._wakeup:
                await self._wakeup.wait_for(lambda: bool(self._waiting))
                job = self._waiting.pop(0)
            self._running[job.id] = job
            try:
                await self._update_positions()
                job.task = asyncio.create_task(self._execute(job))
                try:
                    await asyncio.shield(job.task)
                except asyncio.CancelledError:
                    if not job.cancelled:  # the worker itself is being stopped
                        job.stopping = True
                        job.task.cancel()
                        await asyncio.gather(job.task, return_exceptions=True)
                        raise
            finally:
                self._running.pop(job.id, None)

    async def _execute(self, job: Job) -> None:
        workdir = self.settings.work_dir / job.id
        workdir.mkdir(parents=True, exist_ok=True)
        await self.db.run(self._set_status, job.id, "running", None)
        ctx = JobContext(self, job, workdir)
        input_bytes = sum(f.size for f in job.files)
        output_bytes = 0
        try:
            await ctx.progress(texts.STAGE_STARTING)
            result = await self.handlers[job.kind](ctx)
            sent_all, output_bytes = await delivery.deliver(ctx, result)
        except asyncio.CancelledError:
            if not job.stopping:
                await self._finish(job, "cancelled")
                await self.show_status(job, texts.job_cancelled(job.title))
            raise
        except ToolMissingError as exc:
            log.error("Job %s (%s): tool missing: %s", job.id, job.kind, exc)
            await self._fail(job, texts.ERROR_TOOL_MISSING, input_bytes)
        except ConversionError as exc:
            await self._fail(job, str(exc), input_bytes)
        except Exception:
            log.exception("Job %s (%s) crashed", job.id, job.kind)
            await self._fail(job, texts.ERROR_UNEXPECTED, input_bytes)
        else:
            if sent_all:
                await self._finish(job, "done")
                await self.db.record_event(job.user_id, job.kind, input_bytes, output_bytes, "success")
                await self._delete_status(job)
            else:
                await self._fail(job, texts.ERROR_SEND_FAILED, input_bytes)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    async def _fail(self, job: Job, reason: str, input_bytes: int) -> None:
        await self._finish(job, "failed", reason)
        await self.db.record_event(job.user_id, job.kind, input_bytes, 0, "failed")
        await self.show_status(job, texts.job_failed(job.title, reason, refunded=job.cost > 0))

    async def _finish(self, job: Job, status: str, error: str | None = None) -> None:
        await self.billing.settle(job.id, success=status == "done")
        await self.db.run(self._set_status, job.id, status, error)

    # --- persistence ---------------------------------------------------------

    @staticmethod
    def _insert(c: sqlite3.Connection, job: Job) -> None:
        now = int(time.time())
        c.execute(
            "INSERT INTO jobs(id, user_id, chat_id, kind, payload, cost, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?)",
            (job.id, job.user_id, job.chat_id, job.kind, json.dumps(job.payload, ensure_ascii=False),
             job.cost, now, now),
        )

    @staticmethod
    def _set_status(c: sqlite3.Connection, job_id: str, status: str, error: str | None) -> None:
        c.execute("UPDATE jobs SET status=?, error=?, updated_at=? WHERE id=?",
                  (status, error, int(time.time()), job_id))

    async def _load_unfinished(self) -> list[Job]:
        def q(c: sqlite3.Connection) -> list[Job]:
            rows = c.execute(
                "SELECT * FROM jobs WHERE status IN ('queued', 'running') ORDER BY created_at, rowid"
            ).fetchall()
            c.execute("UPDATE jobs SET status='queued' WHERE status='running'")
            return [
                Job(r["id"], r["user_id"], r["chat_id"], r["kind"], json.loads(r["payload"]), r["cost"])
                for r in rows
            ]

        return await self.db.run(q)
