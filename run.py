import asyncio
import logging
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from converter import ConversionError, ConversionToolMissingError, convert_djvu_to_pdf


SUPPORTED_EXTENSIONS = {".djvu", ".djv"}
DEFAULT_MAX_INPUT_MB = 50
DEFAULT_WORKDIR = "/tmp/convertor"
CONVERT_CALLBACK_PREFIX = "convert_pdf:"


@dataclass(frozen=True)
class Settings:
    bot_token: str
    max_input_mb: int
    workdir: Path


@dataclass(frozen=True)
class PendingDocument:
    file_id: str
    file_name: str
    file_size: int | None
    chat_id: int
    user_id: int


pending_documents: dict[str, PendingDocument] = {}


def load_env_file(path: Path = Path(".env")) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def load_settings() -> Settings:
    load_env_file()

    bot_token = os.getenv("BOT_TOKEN")
    if not bot_token:
        raise RuntimeError("BOT_TOKEN is not set. Create .env from .env.example.")

    max_input_mb = int(os.getenv("MAX_INPUT_MB", str(DEFAULT_MAX_INPUT_MB)))
    workdir = Path(os.getenv("WORKDIR", DEFAULT_WORKDIR))

    return Settings(
        bot_token=bot_token,
        max_input_mb=max_input_mb,
        workdir=workdir,
    )


def is_supported_document(file_name: str | None) -> bool:
    if not file_name:
        return False
    return Path(file_name).suffix.lower() in SUPPORTED_EXTENSIONS


def safe_pdf_name(file_name: str) -> str:
    stem = Path(file_name).stem
    stem = re.sub(r"[^\w._-]+", "_", stem).strip("._-")
    if not stem:
        stem = "converted"
    return f"{stem}.pdf"


def conversion_keyboard(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Конвертировать в PDF",
                    callback_data=f"{CONVERT_CALLBACK_PREFIX}{job_id}",
                )
            ]
        ]
    )


async def handle_start(message: Message) -> None:
    await message.answer(
        "Привет! Пришли файл .djvu или .djv, а я покажу кнопку для конвертации "
        "в PDF. В первой версии OCR не используется, поэтому PDF будет для "
        "чтения и печати без распознавания текста."
    )


async def handle_document(message: Message, settings: Settings) -> None:
    document = message.document
    if document is None:
        return

    if not is_supported_document(document.file_name):
        await message.answer("Пока поддерживаю только файлы .djvu и .djv.")
        return

    max_bytes = settings.max_input_mb * 1024 * 1024
    if document.file_size and document.file_size > max_bytes:
        await message.answer(
            f"Файл слишком большой. Максимум: {settings.max_input_mb} МБ."
        )
        return

    if message.from_user is None:
        await message.answer("Не удалось определить отправителя файла.")
        return

    job_id = uuid.uuid4().hex
    pending_documents[job_id] = PendingDocument(
        file_id=document.file_id,
        file_name=document.file_name or "document.djvu",
        file_size=document.file_size,
        chat_id=message.chat.id,
        user_id=message.from_user.id,
    )

    await message.answer(
        f"Файл принят: {document.file_name}.",
        reply_markup=conversion_keyboard(job_id),
    )


async def handle_conversion(
    callback: CallbackQuery,
    bot: Bot,
    settings: Settings,
) -> None:
    if callback.data is None or not callback.data.startswith(CONVERT_CALLBACK_PREFIX):
        return

    job_id = callback.data.removeprefix(CONVERT_CALLBACK_PREFIX)
    pending = pending_documents.pop(job_id, None)

    if pending is None:
        await callback.answer("Файл уже обработан или бот был перезапущен.", show_alert=True)
        return

    if callback.from_user.id != pending.user_id:
        await callback.answer("Эта кнопка относится к файлу другого пользователя.", show_alert=True)
        pending_documents[job_id] = pending
        return

    if callback.message is None:
        await callback.answer("Не удалось отправить результат в этот чат.", show_alert=True)
        return

    await callback.answer("Начинаю конвертацию...")

    job_dir = settings.workdir / job_id
    input_path = job_dir / f"input{Path(pending.file_name).suffix.lower()}"
    output_name = safe_pdf_name(pending.file_name)
    output_path = job_dir / output_name

    try:
        job_dir.mkdir(parents=True, exist_ok=True)

        await callback.message.answer("Скачиваю файл...")
        await bot.download(pending.file_id, destination=input_path)

        await callback.message.answer("Конвертирую в PDF...")
        await convert_djvu_to_pdf(input_path, output_path)

        await callback.message.answer_document(
            FSInputFile(output_path, filename=output_name),
            caption="Готово. PDF создан без OCR.",
        )
    except ConversionToolMissingError:
        await callback.message.answer(
            "Не найдена утилита ddjvu. Запусти бота через Docker или установи DjVuLibre."
        )
    except ConversionError as exc:
        await callback.message.answer(f"Не удалось конвертировать файл: {exc}")
    except Exception:
        logging.exception("Unexpected conversion failure")
        await callback.message.answer("Произошла неожиданная ошибка при конвертации.")
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    settings = load_settings()
    settings.workdir.mkdir(parents=True, exist_ok=True)

    if shutil.which("ddjvu") is None:
        logging.warning("ddjvu is not available. Conversions will fail outside Docker.")

    bot = Bot(token=settings.bot_token)
    dp = Dispatcher()
    dp["settings"] = settings

    dp.message.register(handle_start, Command("start", "help"))
    dp.message.register(handle_document, F.document)
    dp.callback_query.register(
        handle_conversion,
        F.data.startswith(CONVERT_CALLBACK_PREFIX),
    )

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
