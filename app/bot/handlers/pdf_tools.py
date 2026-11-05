"""PDF tools menu: merge / split / delete pages / compress."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app import texts
from app.bot import keyboards
from app.bot.callbacks import ToolCb
from app.bot.flow import Tool, drop_keyboard, reset_tool, submit
from app.formats import parse_page_ranges
from app.models import IncomingFile
from app.services.queue import JobQueue

router = Router(name="pdf_tools")

PROMPTS = {
    "merge": (Tool.merge, texts.ASK_MERGE_FILES, keyboards.merge_session),
    "split": (Tool.split_wait_file, texts.ASK_SPLIT_FILE, keyboards.cancel),
    "delete": (Tool.delete_wait_file, texts.ASK_DELETE_FILE, keyboards.cancel),
    "compress": (Tool.compress_wait_file, texts.ASK_COMPRESS_FILE, keyboards.cancel),
}


@router.callback_query(ToolCb.filter())
async def on_tool(callback: CallbackQuery, callback_data: ToolCb, state: FSMContext, queue: JobQueue) -> None:
    action, msg, user_id = callback_data.action, callback.message, callback.from_user.id

    if action in PROMPTS:
        new_state, prompt, markup = PROMPTS[action]
        await reset_tool(state)
        await state.set_state(new_state)
        await callback.answer()
        await msg.answer(prompt, reply_markup=markup())
        return

    if action == "cancel":
        await reset_tool(state)
        await callback.answer(texts.CANCELLED)
        await drop_keyboard(msg)
        await msg.answer(texts.CANCELLED, reply_markup=keyboards.main_menu())
        return

    data = await state.get_data()
    if action == "merge_done":
        files = [IncomingFile.from_dict(f) for f in data.get("merge", [])]
        if await state.get_state() != Tool.merge.state or len(files) < 2:
            await callback.answer(texts.MERGE_NEED_TWO, show_alert=True)
            return
        await callback.answer()
        if await submit(msg, user_id, queue, "pdf_merge", files, texts.title_for("pdf_merge", f"{len(files)} шт.")):
            await drop_keyboard(msg)
            await reset_tool(state)
        return

    if action == "split_all":
        target = data.get("target")
        if not target:
            await callback.answer(texts.ASK_SPLIT_FILE, show_alert=True)
            return
        await callback.answer()
        file = IncomingFile.from_dict(target)
        if await submit(msg, user_id, queue, "pdf_split_all", [file], texts.title_for("pdf_split_all", file.name)):
            await drop_keyboard(msg)
            await reset_tool(state)


@router.message(F.text, Tool.split_ranges)
@router.message(F.text, Tool.delete_ranges)
async def on_ranges(message: Message, state: FSMContext, queue: JobQueue) -> None:
    try:
        pages = parse_page_ranges(message.text)
    except ValueError:
        await message.answer(texts.BAD_RANGES, reply_markup=keyboards.cancel())
        return
    data = await state.get_data()
    target = data.get("target")
    if not target:
        await reset_tool(state)
        await message.answer(texts.BUTTON_EXPIRED)
        return
    delete = await state.get_state() == Tool.delete_ranges.state
    file = IncomingFile.from_dict(target)
    title = texts.title_for("pdf_delete" if delete else "pdf_extract", file.name)
    if await submit(message, message.from_user.id, queue, "pdf_pages", [file], title, pages=pages, delete=delete):
        await reset_tool(state)
