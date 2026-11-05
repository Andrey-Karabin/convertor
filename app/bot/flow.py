"""Helpers shared by handlers: dialog states, remembering files, queueing jobs."""

from __future__ import annotations

import secrets
from contextlib import suppress
from typing import Any

from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message

from app import texts
from app.bot import keyboards
from app.models import IncomingFile
from app.services.billing import LimitReached
from app.services.queue import JobQueue, TooManyJobs

MAX_PENDING = 20  # how many "what to do with this file?" prompts we remember per user


class Tool(StatesGroup):
    """Dialog states of the PDF tools (stored in Redis, survive restarts)."""

    merge = State()  # collecting PDFs to merge
    split_wait_file = State()
    delete_wait_file = State()
    compress_wait_file = State()
    split_ranges = State()  # waiting for page numbers
    delete_ranges = State()


# --- files waiting for the user's choice -------------------------------------

async def remember(state: FSMContext, files: list[IncomingFile]) -> str:
    data = await state.get_data()
    pending: dict[str, Any] = dict(data.get("pending", {}))
    while len(pending) >= MAX_PENDING:
        pending.pop(next(iter(pending)))
    token = secrets.token_hex(4)
    pending[token] = [f.to_dict() for f in files]
    await state.update_data(pending=pending)
    return token


async def recall(state: FSMContext, token: str, *, forget: bool = True) -> list[IncomingFile] | None:
    data = await state.get_data()
    pending: dict[str, Any] = dict(data.get("pending", {}))
    raw = pending.pop(token, None) if forget else pending.get(token)
    if forget and raw is not None:
        await state.update_data(pending=pending)
    return [IncomingFile.from_dict(f) for f in raw] if raw else None


async def forget(state: FSMContext, token: str) -> None:
    data = await state.get_data()
    pending: dict[str, Any] = dict(data.get("pending", {}))
    if pending.pop(token, None) is not None:
        await state.update_data(pending=pending)


async def drop_keyboard(message: Any) -> None:
    """Remove buttons from a message so they can't be pressed twice."""
    with suppress(TelegramAPIError, AttributeError):
        await message.edit_reply_markup(reply_markup=None)


async def reset_tool(state: FSMContext) -> None:
    """Leave the PDF-tool dialog but keep other remembered files."""
    await state.set_state(None)
    data = await state.get_data()
    data.pop("merge", None)
    data.pop("target", None)
    await state.set_data(data)


# --- queueing ----------------------------------------------------------------

async def submit(
    message: Message,
    user_id: int,
    queue: JobQueue,
    kind: str,
    files: list[IncomingFile],
    title: str,
    *,
    cost: int | None = None,
    **extra: Any,
) -> bool:
    """Put a job into the queue and explain to the user if it's not possible."""
    payload = {"title": title, "files": [f.to_dict() for f in files], **extra}
    try:
        await queue.submit(user_id, message.chat.id, kind, payload, len(files) if cost is None else cost)
    except LimitReached as exc:
        await message.answer(
            texts.LIMIT_REACHED.format(needed=exc.needed, available=exc.balance.available or 0),
            reply_markup=keyboards.buy_button(),
        )
        return False
    except TooManyJobs:
        await message.answer(texts.TOO_MANY_JOBS.format(count=queue.active_count(user_id)))
        return False
    return True
