"""Middlewares: albums arrive as one handler call; users are recorded in the DB."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.services.db import Database


class AlbumMiddleware(BaseMiddleware):
    """Telegram sends an album (several files at once) as separate messages with the
    same media_group_id. Collect them for a moment and call the handler once with
    `album=[...]`. One mechanism instead of three copies in the old bot."""

    def __init__(self, latency: float = 1.0) -> None:
        self.latency = latency
        self._albums: dict[str, list[Message]] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Message) or not event.media_group_id:
            return await handler(event, data)
        key = f"{event.chat.id}:{event.media_group_id}"
        if key in self._albums:
            self._albums[key].append(event)
            return None
        self._albums[key] = [event]
        await asyncio.sleep(self.latency)
        album = sorted(self._albums.pop(key), key=lambda m: m.message_id)
        data["album"] = album
        return await handler(album[0], data)


class TouchUserMiddleware(BaseMiddleware):
    """Remember who uses the bot (for statistics). At most one DB write per user per 10 minutes."""

    def __init__(self, db: Database, every: int = 600) -> None:
        self.db = db
        self.every = every
        self._seen: dict[int, float] = {}

    async def __call__(self, handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        user = event.from_user if isinstance(event, (Message, CallbackQuery)) else None
        if user is not None:
            now = time.monotonic()
            if now - self._seen.get(user.id, -self.every) >= self.every:
                self._seen[user.id] = now
                await self.db.touch_user(user.id, user.username, user.full_name)
        return await handler(event, data)
