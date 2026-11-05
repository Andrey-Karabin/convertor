"""/start, main menu buttons, /cancel, cancelling queued jobs."""

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app import texts
from app.bot import keyboards
from app.bot.callbacks import JobCb
from app.bot.flow import drop_keyboard, reset_tool
from app.config import Settings
from app.services.billing import Billing
from app.services.queue import JobQueue

router = Router(name="common")


@router.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    await reset_tool(state)
    await message.answer(texts.START, reply_markup=keyboards.main_menu())


@router.message(Command("help"))
@router.message(F.text == texts.BTN_HELP)
async def help_(message: Message) -> None:
    await message.answer(texts.FEATURES, reply_markup=keyboards.main_menu())


@router.message(F.text == texts.BTN_PDF_TOOLS)
async def pdf_tools(message: Message, state: FSMContext) -> None:
    await reset_tool(state)
    await message.answer(texts.PDF_TOOLS, reply_markup=keyboards.pdf_tools())


@router.message(Command("balance"))
@router.message(F.text == texts.BTN_BALANCE)
async def balance(message: Message, billing: Billing, settings: Settings) -> None:
    bal = await billing.balance(message.from_user.id)
    markup = keyboards.buy_button() if settings.billing_enabled and settings.products and not bal.unlimited else None
    await message.answer(texts.balance_text(bal, settings.billing_enabled), reply_markup=markup)


@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext) -> None:
    await reset_tool(state)
    await message.answer(texts.CANCELLED, reply_markup=keyboards.main_menu())


@router.callback_query(JobCb.filter())
async def cancel_job(callback: CallbackQuery, callback_data: JobCb, queue: JobQueue) -> None:
    if await queue.cancel(callback_data.job_id, callback.from_user.id):
        await callback.answer(texts.CANCELLED)
    else:
        await callback.answer(texts.JOB_NOT_FOUND)
        await drop_keyboard(callback.message)
