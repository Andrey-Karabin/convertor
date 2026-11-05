"""All keyboards in one place."""

from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app import texts
from app.bot.callbacks import AdminCb, ArchiveCb, BuyCb, FileCb, GroupCb, JobCb, ToolCb
from app.config import Product
from app.formats import FileKind

ARCHIVE_PAGE_SIZE = 8


def _column(builder: InlineKeyboardBuilder) -> InlineKeyboardMarkup:
    builder.adjust(1)
    return builder.as_markup()


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=texts.BTN_HELP), KeyboardButton(text=texts.BTN_PDF_TOOLS)],
            [KeyboardButton(text=texts.BTN_BALANCE)],
        ],
        resize_keyboard=True,
    )


def file_actions(token: str, kind: FileKind) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()

    def add(text: str, action: str) -> None:
        b.button(text=text, callback_data=FileCb(token=token, action=action))

    if kind is FileKind.PDF:
        add(texts.BTN_MERGE_WITH, "merge")
        add(texts.BTN_SPLIT, "split")
        add(texts.BTN_DELETE, "delete")
        add(texts.BTN_COMPRESS_PDF, "pdf_compress")
    elif kind is FileKind.ARCHIVE:
        add(texts.BTN_ARCHIVE_ALL, "arch_all")
        add(texts.BTN_ARCHIVE_PICK, "arch_pick")
    else:
        add(texts.BTN_TO_PDF, "to_pdf")
        if kind is FileKind.IMAGE:
            add(texts.BTN_COMPRESS_PHOTO, "compress")
    return _column(b)


def group_actions(token: str, all_images: bool, all_pdfs: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if all_pdfs:
        b.button(text=texts.BTN_MERGE, callback_data=GroupCb(token=token, action="pdf_merge"))
    else:
        b.button(text=texts.BTN_EACH_TO_PDF, callback_data=GroupCb(token=token, action="each"))
        b.button(text=texts.BTN_MERGE_TO_PDF, callback_data=GroupCb(token=token, action="merge"))
        if all_images:
            b.button(text=texts.BTN_PHOTOS_ZIP, callback_data=GroupCb(token=token, action="zip"))
    return _column(b)


def pdf_tools() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=texts.BTN_MERGE, callback_data=ToolCb(action="merge"))
    b.button(text=texts.BTN_SPLIT, callback_data=ToolCb(action="split"))
    b.button(text=texts.BTN_DELETE, callback_data=ToolCb(action="delete"))
    b.button(text=texts.BTN_COMPRESS_PDF, callback_data=ToolCb(action="compress"))
    return _column(b)


def merge_session() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=texts.BTN_MERGE_DONE, callback_data=ToolCb(action="merge_done"))
    b.button(text=texts.BTN_CANCEL, callback_data=ToolCb(action="cancel"))
    return _column(b)


def split_session() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=texts.BTN_SPLIT_ALL, callback_data=ToolCb(action="split_all"))
    b.button(text=texts.BTN_CANCEL, callback_data=ToolCb(action="cancel"))
    return _column(b)


def cancel() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=texts.BTN_CANCEL, callback_data=ToolCb(action="cancel"))
    return b.as_markup()


def cancel_job(job_id: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=texts.BTN_CANCEL_JOB, callback_data=JobCb(job_id=job_id))
    return b.as_markup()


def archive_entries(names: list[str], page: int) -> InlineKeyboardMarkup:
    pages = max(1, -(-len(names) // ARCHIVE_PAGE_SIZE))
    page = min(max(0, page), pages - 1)
    start = page * ARCHIVE_PAGE_SIZE
    b = InlineKeyboardBuilder()
    for index, name in enumerate(names[start:start + ARCHIVE_PAGE_SIZE], start=start):
        label = name if len(name) <= 48 else f"…{name[-45:]}"
        b.button(text=label, callback_data=ArchiveCb(action="pick", value=index))
    rows = [1] * len(names[start:start + ARCHIVE_PAGE_SIZE])
    nav = 0
    if page > 0:
        b.button(text=texts.BTN_BACK, callback_data=ArchiveCb(action="page", value=page - 1))
        nav += 1
    if page + 1 < pages:
        b.button(text=texts.BTN_NEXT, callback_data=ArchiveCb(action="page", value=page + 1))
        nav += 1
    b.adjust(*rows, nav) if nav else b.adjust(1)
    return b.as_markup()


def products(items: tuple[Product, ...]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for product in items:
        b.button(text=f"{texts.product_title(product.kind, product.amount)} — {product.stars} ⭐",
                 callback_data=BuyCb(code=product.code))
    return _column(b)


def buy_button() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=texts.BTN_BUY, callback_data=BuyCb(code="menu"))
    return b.as_markup()


def admin() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="📊 Статистика", callback_data=AdminCb(action="stats"))
    b.button(text="⚙️ Настройки", callback_data=AdminCb(action="settings"))
    return _column(b)
