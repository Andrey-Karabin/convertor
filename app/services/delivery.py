"""Sending results: OCR for PDFs, splitting files that are too big for Telegram."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiogram.exceptions import TelegramAPIError
from aiogram.types import FSInputFile

from app import texts
from app.converters import ConversionError, ocr, pdf
from app.formats import safe_stem
from app.models import JobResult, Output

if TYPE_CHECKING:
    from app.services.queue import JobContext

log = logging.getLogger(__name__)
MB = 1024 * 1024


async def deliver(ctx: "JobContext", result: JobResult) -> tuple[bool, int]:
    """Send every output. Returns (all sent, total bytes sent)."""
    settings = ctx.settings
    sent_all = True
    sent_bytes = 0

    for output in result.outputs:
        if output.ocr and settings.ocr_enabled and output.path.suffix.lower() == ".pdf":
            await ctx.progress(texts.STAGE_OCR)
            _, recognised = await ocr.try_add_text_layer(
                output.path,
                languages=settings.ocr_languages,
                jobs=settings.ocr_jobs,
                rotate_pages=settings.ocr_rotate_pages,
                timeout=settings.ocr_timeout,
            )
            if not recognised:
                output.caption = f"{output.caption}\n{texts.OCR_SKIPPED}".strip()

    if result.outputs:
        await ctx.progress(texts.STAGE_SENDING)
    for output in result.outputs:
        ok, size = await _send(ctx, output)
        sent_all = sent_all and ok
        sent_bytes += size

    if result.text:
        await ctx.bot.send_message(ctx.job.chat_id, result.text, reply_markup=result.reply_markup)
    return sent_all, sent_bytes


async def _send(ctx: "JobContext", output: Output) -> tuple[bool, int]:
    limit = ctx.settings.effective_max_output_mb * MB
    size = output.path.stat().st_size

    if size <= limit and await _send_file(ctx, output.path, output.filename, output.caption):
        return True, size

    if output.path.suffix.lower() != ".pdf":
        await ctx.bot.send_message(ctx.job.chat_id, texts.result_too_big(size / MB, limit // MB))
        return False, 0

    # A PDF that is too big: send it in parts.
    try:
        parts = await pdf.split_by_size(output.path, output.path.parent / "parts",
                                        safe_stem(output.filename), limit)
    except ConversionError as exc:
        await ctx.bot.send_message(ctx.job.chat_id, f"{texts.result_too_big(size / MB, limit // MB)}\n{exc}")
        return False, 0

    await ctx.bot.send_message(ctx.job.chat_id, texts.sending_in_parts(size / MB, len(parts)))
    total = 0
    for number, part in enumerate(parts, start=1):
        caption = f"{output.caption}\n{texts.part_caption(number, len(parts))}".strip()
        if not await _send_file(ctx, part, part.name, caption):
            return False, total
        total += part.stat().st_size
    return True, total


async def _send_file(ctx: "JobContext", path, filename: str, caption: str) -> bool:
    try:
        await ctx.bot.send_document(
            ctx.job.chat_id,
            FSInputFile(path, filename=filename),
            caption=caption[:1024] or None,
            request_timeout=int(ctx.settings.telegram_request_timeout),
        )
        return True
    except TelegramAPIError as exc:
        log.warning("Sending %s failed: %s", filename, exc)
        return False
