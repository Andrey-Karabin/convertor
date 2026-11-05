"""Typed inline-button payloads (instead of hand-made "prefix:value" strings)."""

from aiogram.filters.callback_data import CallbackData


class FileCb(CallbackData, prefix="f"):
    """Action for one file the user sent. `token` points into FSM data."""

    token: str
    action: str  # to_pdf | compress | arch_all | arch_pick | merge | split | delete | pdf_compress


class GroupCb(CallbackData, prefix="g"):
    """Action for several files sent together (an album)."""

    token: str
    action: str  # each | merge | zip | pdf_merge


class ArchiveCb(CallbackData, prefix="a"):
    action: str  # page | pick
    value: int


class ToolCb(CallbackData, prefix="t"):
    action: str  # merge | split | delete | compress | merge_done | split_all | cancel


class JobCb(CallbackData, prefix="j"):
    job_id: str


class BuyCb(CallbackData, prefix="b"):
    code: str


class AdminCb(CallbackData, prefix="adm"):
    action: str
