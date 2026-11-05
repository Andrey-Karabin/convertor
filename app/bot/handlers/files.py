"""Incoming files and the buttons under them."""

from __future__ import annotations

from html import escape

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app import texts
from app.bot import keyboards
from app.bot.callbacks import FileCb, GroupCb
from app.bot.flow import Tool, drop_keyboard, forget, recall, remember, reset_tool, submit
from app.config import Settings
from app.formats import FileKind
from app.models import IncomingFile
from app.services.queue import JobQueue

router = Router(name="files")

TOOL_STATES = {s.state for s in Tool.__all_states__}


def _parse(messages: list[Message], settings: Settings) -> tuple[list[IncomingFile], list[str]]:
    files, problems, unsupported = [], [], []
    limit = settings.effective_max_input_mb
    for msg in messages:
        file = IncomingFile.from_message(msg)
        if file is None:
            if msg.document is not None:
                unsupported.append(msg.document.file_name or "файл")
            continue
        if file.size > limit * 1024 * 1024:
            problems.append(texts.TOO_BIG.format(name=escape(file.name), size=file.size / 1024 / 1024, limit=limit))
            continue
        files.append(file)
    if unsupported:
        problems.append(texts.UNSUPPORTED if not files and len(messages) == 1
                        else texts.SKIPPED_FILES.format(names=escape(", ".join(unsupported))))
    return files, problems


@router.message(F.document | F.photo)
async def on_files(
    message: Message,
    state: FSMContext,
    settings: Settings,
    queue: JobQueue,
    album: list[Message] | None = None,
) -> None:
    files, problems = _parse(album or [message], settings)
    for problem in problems:
        await message.answer(problem)
    if not files:
        return

    if await state.get_state() in TOOL_STATES:
        await _feed_tool(message, state, queue, files)
        return

    archives = [f for f in files if f.kind is FileKind.ARCHIVE]
    others = [f for f in files if f.kind is not FileKind.ARCHIVE]
    for archive in archives:  # archives are always handled one by one
        token = await remember(state, [archive])
        await message.answer(texts.CHOOSE_ACTION.format(name=escape(archive.name)),
                             reply_markup=keyboards.file_actions(token, archive.kind))
    if len(others) == 1:
        file = others[0]
        token = await remember(state, others)
        text = texts.PDF_CHOOSE_ACTION if file.kind is FileKind.PDF else texts.CHOOSE_ACTION
        await message.answer(text.format(name=escape(file.name)), reply_markup=keyboards.file_actions(token, file.kind))
    elif others:
        token = await remember(state, others)
        await message.answer(
            texts.CHOOSE_GROUP_ACTION.format(count=len(others)),
            reply_markup=keyboards.group_actions(
                token,
                all_images=all(f.kind is FileKind.IMAGE for f in others),
                all_pdfs=all(f.kind is FileKind.PDF for f in others),
            ),
        )


async def _feed_tool(message: Message, state: FSMContext, queue: JobQueue, files: list[IncomingFile]) -> None:
    """The user is inside a PDF tool dialog and sent PDFs for it."""
    pdfs = [f for f in files if f.kind is FileKind.PDF]
    if not pdfs:
        await message.answer(texts.NEED_PDF, reply_markup=keyboards.cancel())
        return
    current = await state.get_state()
    if current == Tool.merge.state:
        data = await state.get_data()
        merge = [*data.get("merge", []), *(f.to_dict() for f in pdfs)]
        await state.update_data(merge=merge)
        await message.answer(texts.MERGE_ADDED.format(count=len(merge)), reply_markup=keyboards.merge_session())
        return

    target = pdfs[-1]
    if current == Tool.compress_wait_file.state:
        await reset_tool(state)
        await submit(message, message.from_user.id, queue, "pdf_compress", [target],
                     texts.title_for("pdf_compress", target.name))
        return
    await state.update_data(target=target.to_dict())
    if current in {Tool.split_wait_file.state, Tool.split_ranges.state}:
        await state.set_state(Tool.split_ranges)
        await message.answer(texts.ASK_SPLIT_RANGES, reply_markup=keyboards.split_session())
    else:
        await state.set_state(Tool.delete_ranges)
        await message.answer(texts.ASK_DELETE_RANGES, reply_markup=keyboards.cancel())


@router.callback_query(FileCb.filter())
async def on_file_action(callback: CallbackQuery, callback_data: FileCb, state: FSMContext, queue: JobQueue) -> None:
    token = callback_data.token
    files = await recall(state, token, forget=False)
    if not files:
        await callback.answer(texts.BUTTON_EXPIRED, show_alert=True)
        return
    await callback.answer()
    file, user_id, msg = files[0], callback.from_user.id, callback.message
    action = callback_data.action

    jobs = {
        "to_pdf": ("convert", texts.title_convert([file.name], False), {"mode": "each"}),
        "compress": ("compress_images", texts.title_for("compress_images", file.name), {}),
        "arch_all": ("archive_extract", texts.title_for("archive_extract", file.name), {}),
        "arch_pick": ("archive_list", texts.title_for("archive_list", file.name), {"cost": 0}),
        "pdf_compress": ("pdf_compress", texts.title_for("pdf_compress", file.name), {}),
    }
    if action in jobs:
        kind, title, extra = jobs[action]
        # If the limit is reached the buttons stay, so the user can press again after buying.
        if await submit(msg, user_id, queue, kind, files, title, **extra):
            await forget(state, token)
            await drop_keyboard(msg)
        return

    await forget(state, token)
    await drop_keyboard(msg)
    if action == "merge":
        await state.set_state(Tool.merge)
        await state.update_data(merge=[file.to_dict()])
        await msg.answer(texts.ASK_MORE_FOR_MERGE, reply_markup=keyboards.merge_session())
    elif action in {"split", "delete"}:
        await state.update_data(target=file.to_dict())
        if action == "split":
            await state.set_state(Tool.split_ranges)
            await msg.answer(texts.ASK_SPLIT_RANGES, reply_markup=keyboards.split_session())
        else:
            await state.set_state(Tool.delete_ranges)
            await msg.answer(texts.ASK_DELETE_RANGES, reply_markup=keyboards.cancel())


@router.callback_query(GroupCb.filter())
async def on_group_action(callback: CallbackQuery, callback_data: GroupCb, state: FSMContext, queue: JobQueue) -> None:
    token = callback_data.token
    files = await recall(state, token, forget=False)
    if not files:
        await callback.answer(texts.BUTTON_EXPIRED, show_alert=True)
        return
    await callback.answer()
    msg, user_id, names = callback.message, callback.from_user.id, [f.name for f in files]

    if callback_data.action == "zip":
        ok = await submit(msg, user_id, queue, "compress_images", files,
                          texts.title_for("compress_images", f"{len(files)} фото"))
    elif callback_data.action == "pdf_merge":
        ok = await submit(msg, user_id, queue, "pdf_merge", files, texts.title_for("pdf_merge", f"{len(files)} шт."))
    else:
        merge = callback_data.action == "merge"
        ok = await submit(msg, user_id, queue, "convert", files, texts.title_convert(names, merge),
                          mode="merge" if merge else "each")
    if ok:
        await forget(state, token)
        await drop_keyboard(msg)
