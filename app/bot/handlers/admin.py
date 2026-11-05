"""Admin commands: /admin, /grant, /payments, /refund."""

from __future__ import annotations

import time

from aiogram import Bot, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message, TelegramObject

from app import texts
from app.bot import keyboards
from app.bot.callbacks import AdminCb
from app.config import Settings
from app.services.billing import Billing
from app.services.db import Database

router = Router(name="admin")


def _is_admin(event: TelegramObject, settings: Settings) -> bool:
    return event.from_user is not None and settings.is_admin(event.from_user.id)


router.message.filter(_is_admin)
router.callback_query.filter(_is_admin)


@router.message(Command("admin"))
async def admin(message: Message) -> None:
    await message.answer(texts.ADMIN_TITLE, reply_markup=keyboards.admin())


@router.callback_query(AdminCb.filter())
async def admin_action(callback: CallbackQuery, callback_data: AdminCb, db: Database, settings: Settings) -> None:
    await callback.answer()
    if callback_data.action == "stats":
        await callback.message.answer(texts.admin_stats(await db.admin_summary()), reply_markup=keyboards.admin())
    else:
        await callback.message.answer(texts.admin_settings(settings), reply_markup=keyboards.admin())


@router.message(Command("grant"))
async def grant(message: Message, command: CommandObject, billing: Billing) -> None:
    try:
        user_id, amount = (int(x) for x in (command.args or "").split())
    except ValueError:
        await message.answer(texts.GRANT_USAGE)
        return
    total = await billing.grant(user_id, amount)
    await message.answer(f"Готово. У пользователя {user_id} теперь {total} операций.")


@router.message(Command("payments"))
async def payments(message: Message, command: CommandObject, billing: Billing) -> None:
    try:
        user_id = int((command.args or "").strip())
    except ValueError:
        await message.answer("Использование: <code>/payments user_id</code>")
        return
    rows = await billing.last_payments(user_id)
    if not rows:
        await message.answer("Платежей нет.")
        return
    lines = [
        f"{time.strftime('%d.%m.%Y', time.localtime(r['created_at']))} — {r['product']}, {r['stars']}⭐"
        f"{' (возврат)' if r['refunded'] else ''}\n<code>{r['charge_id']}</code>"
        for r in rows
    ]
    await message.answer("\n\n".join(lines))


@router.message(Command("refund"))
async def refund(message: Message, command: CommandObject, bot: Bot, billing: Billing) -> None:
    charge_id = (command.args or "").strip()
    user_id = await billing.find_payment_user(charge_id) if charge_id else None
    if user_id is None:
        await message.answer(texts.REFUND_USAGE)
        return
    try:
        await bot.refund_star_payment(user_id=user_id, telegram_payment_charge_id=charge_id)
    except TelegramAPIError as exc:
        await message.answer(f"Telegram не выполнил возврат: {exc.message}")
        return
    await billing.mark_refunded(charge_id)
    await message.answer(f"Stars возвращены пользователю {user_id}, купленное списано.")
