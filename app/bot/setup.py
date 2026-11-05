"""Building the Bot and Dispatcher."""

from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.fsm.storage.base import BaseStorage
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from app.bot.handlers import admin, archives, common, fallback, files, payments, pdf_tools
from app.bot.middlewares import AlbumMiddleware, TouchUserMiddleware
from app.config import Settings
from app.services.db import Database

log = logging.getLogger(__name__)

COMMANDS = [
    BotCommand(command="start", description="Начать"),
    BotCommand(command="help", description="Что умеет бот"),
    BotCommand(command="balance", description="Баланс и лимиты"),
    BotCommand(command="buy", description="Купить операции"),
    BotCommand(command="cancel", description="Отменить текущее действие"),
    BotCommand(command="terms", description="Условия"),
    BotCommand(command="paysupport", description="Помощь с оплатой"),
]


def create_bot(settings: Settings) -> Bot:
    session = None
    if settings.telegram_api_base:
        session = AiohttpSession(
            api=TelegramAPIServer.from_base(settings.telegram_api_base, is_local=settings.telegram_api_is_local),
            timeout=settings.telegram_request_timeout,
        )
        log.info("Using Bot API server %s (local=%s)", settings.telegram_api_base, settings.telegram_api_is_local)
    return Bot(settings.bot_token, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))


def create_storage(settings: Settings) -> BaseStorage:
    """Dialog state (FSM) storage. Redis keeps it across restarts."""
    if settings.redis_url.lower() in {"", "memory"}:
        log.warning("REDIS_URL is empty: dialog state is kept in memory and lost on restart")
        return MemoryStorage()
    from aiogram.fsm.storage.redis import RedisStorage

    # Unfinished dialogs expire after a day so Redis doesn't grow forever.
    return RedisStorage.from_url(settings.redis_url, state_ttl=86400, data_ttl=86400)


def create_dispatcher(storage: BaseStorage, db: Database) -> Dispatcher:
    dp = Dispatcher(storage=storage)
    dp.message.outer_middleware(TouchUserMiddleware(db))
    dp.callback_query.outer_middleware(TouchUserMiddleware(db))
    dp.message.middleware(AlbumMiddleware())
    # Order matters: the first router whose filters match handles the update.
    dp.include_routers(
        common.router,
        payments.router,
        admin.router,
        pdf_tools.router,
        archives.router,
        files.router,
        fallback.router,
    )
    return dp
