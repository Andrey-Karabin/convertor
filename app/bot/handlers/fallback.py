"""Anything nobody else handled."""

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message

from app import texts
from app.bot import keyboards

router = Router(name="fallback")


@router.message(Command("admin", "grant", "payments", "refund"))
async def admin_only(message: Message) -> None:
    await message.answer(texts.ADMIN_ONLY)


@router.message(F.text)
async def text_without_file(message: Message) -> None:
    await message.answer(texts.TEXT_WITHOUT_FILE, reply_markup=keyboards.main_menu())


@router.message(F.document | F.photo | F.video | F.audio | F.voice | F.sticker)
async def unsupported(message: Message) -> None:
    await message.answer(texts.UNSUPPORTED, reply_markup=keyboards.main_menu())
