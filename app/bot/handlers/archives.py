"""Picking one file from an archive (the list is built by the `archive_list` job)."""

from aiogram import Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from app import texts
from app.bot import keyboards
from app.bot.callbacks import ArchiveCb
from app.bot.flow import submit
from app.models import IncomingFile
from app.services.queue import JobQueue

router = Router(name="archives")


@router.callback_query(ArchiveCb.filter())
async def on_archive(callback: CallbackQuery, callback_data: ArchiveCb, state: FSMContext, queue: JobQueue) -> None:
    archive = (await state.get_data()).get("archive")
    if not archive:
        await callback.answer(texts.ARCHIVE_EXPIRED, show_alert=True)
        return
    names: list[str] = archive["entries"]

    if callback_data.action == "page":
        await callback.answer()
        await callback.message.edit_reply_markup(reply_markup=keyboards.archive_entries(names, callback_data.value))
        return

    if not 0 <= callback_data.value < len(names):
        await callback.answer(texts.ARCHIVE_EXPIRED, show_alert=True)
        return
    await callback.answer()
    name = names[callback_data.value]
    file = IncomingFile.from_dict(archive["file"])
    await submit(callback.message, callback.from_user.id, queue, "archive_extract", [file],
                 texts.title_for("archive_extract", name), wanted=[name])
