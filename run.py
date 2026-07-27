import asyncio
import logging
import os
import re
import shutil
import uuid
from dataclasses import dataclass, field
from itertools import count
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from converter import (
    ConversionError,
    ConversionToolMissingError,
    convert_djvu_to_pdf,
    convert_image_to_pdf,
    convert_office_to_pdf,
    delete_pdf_pages,
    extract_pdf_pages,
    merge_pdfs,
    split_pdf_to_pdf_parts,
    split_pdf_to_zip_parts,
)
from metrics import BotDatabase


DJVU_EXTENSIONS = {".djvu", ".djv"}
DOCUMENT_EXTENSIONS = {
    ".csv",
    ".doc",
    ".docx",
    ".html",
    ".htm",
    ".odp",
    ".ods",
    ".odt",
    ".ppt",
    ".pptx",
    ".rtf",
    ".txt",
    ".xls",
    ".xlsx",
}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
SUPPORTED_EXTENSIONS = DJVU_EXTENSIONS | DOCUMENT_EXTENSIONS | IMAGE_EXTENSIONS

DEFAULT_MAX_INPUT_MB = 200
DEFAULT_MAX_OUTPUT_MB = 49
DEFAULT_WORKDIR = "/tmp/convertor"
DEFAULT_DB_PATH = "/tmp/convertor/bot.sqlite3"
DOWNLOAD_TIMEOUT_SECONDS = 300
TELEGRAM_SAFE_OUTPUT_MB = 45

MAIN_BUTTON_HELP = "Описание функционала"
MAIN_BUTTON_PDF_TOOLS = "Интересные функции"
ADMIN_CALLBACK_PREFIX = "admin:"

CONVERT_CALLBACK_PREFIX = "convert_pdf:"
PDF_TOOL_CALLBACK_PREFIX = "pdf_tool:"
PDF_FILE_CALLBACK_PREFIX = "pdf_file_tool:"
ALBUM_CONVERT_CALLBACK_PREFIX = "album_convert:"

PDF_TOOL_MERGE = "merge"
PDF_TOOL_SPLIT = "split"
PDF_TOOL_DELETE = "delete"
PDF_TOOL_MERGE_DONE = "merge_done"
PDF_TOOL_SPLIT_ALL = "split_all"
PDF_TOOL_CANCEL = "cancel"
ALBUM_ACTION_INDIVIDUAL = "individual"
ALBUM_ACTION_MERGE = "merge"


@dataclass(frozen=True)
class Settings:
    bot_token: str
    max_input_mb: int
    max_output_mb: int
    workdir: Path
    db_path: Path
    admin_ids: set[int]
    weekly_limit_files: int
    weekly_limit_mb: int
    telegram_api_base: str | None
    telegram_api_is_local: bool
    telegram_request_timeout: float


@dataclass(frozen=True)
class PendingFile:
    file_id: str
    file_name: str
    file_size: int | None
    user_id: int
    conversion_type: str
    message_id: int = 0
    media_group_id: str | None = None
    sequence: int = 0


@dataclass(frozen=True)
class PdfToolFile:
    file_id: str
    file_name: str
    file_size: int | None
    message_id: int
    media_group_id: str | None = None
    sequence: int = 0


@dataclass(frozen=True)
class PendingPdfUpload:
    file_id: str
    file_name: str
    file_size: int | None
    user_id: int
    message_id: int
    media_group_id: str | None = None
    sequence: int = 0


@dataclass
class PdfToolSession:
    action: str
    files: list[PdfToolFile] = field(default_factory=list)
    waiting_for_ranges: bool = False


pending_files: dict[str, PendingFile] = {}
pending_pdf_uploads: dict[str, PendingPdfUpload] = {}
album_conversion_sessions: dict[str, list[PendingFile]] = {}
pdf_tool_sessions: dict[int, PdfToolSession] = {}
merge_album_tasks: dict[tuple[int, str], asyncio.Task] = {}
pdf_upload_album_tasks: dict[tuple[int, str], asyncio.Task] = {}
conversion_album_tasks: dict[tuple[int, str], asyncio.Task] = {}
pdf_upload_albums: dict[tuple[int, str], list[PendingPdfUpload]] = {}
conversion_albums: dict[tuple[int, str], list[PendingFile]] = {}
file_sequence = count(1)


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
    max_output_mb = int(os.getenv("MAX_OUTPUT_MB", str(DEFAULT_MAX_OUTPUT_MB)))
    workdir = Path(os.getenv("WORKDIR", DEFAULT_WORKDIR))
    db_path = Path(os.getenv("DB_PATH", DEFAULT_DB_PATH))
    admin_ids = parse_admin_ids(os.getenv("ADMIN_IDS", ""))
    weekly_limit_files = int(os.getenv("WEEKLY_LIMIT_FILES", "0"))
    weekly_limit_mb = int(os.getenv("WEEKLY_LIMIT_MB", "0"))
    telegram_api_base = os.getenv("TELEGRAM_API_BASE", "").strip() or None
    telegram_api_is_local = os.getenv("TELEGRAM_API_IS_LOCAL", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    telegram_request_timeout = float(os.getenv("TELEGRAM_REQUEST_TIMEOUT", "3600"))

    return Settings(
        bot_token=bot_token,
        max_input_mb=max_input_mb,
        max_output_mb=max_output_mb,
        workdir=workdir,
        db_path=db_path,
        admin_ids=admin_ids,
        weekly_limit_files=weekly_limit_files,
        weekly_limit_mb=weekly_limit_mb,
        telegram_api_base=telegram_api_base,
        telegram_api_is_local=telegram_api_is_local,
        telegram_request_timeout=telegram_request_timeout,
    )


def parse_admin_ids(raw_value: str) -> set[int]:
    admin_ids = set()
    for raw_part in raw_value.replace(";", ",").split(","):
        part = raw_part.strip()
        if not part:
            continue
        try:
            admin_ids.add(int(part))
        except ValueError:
            logging.warning("Ignoring invalid ADMIN_IDS value: %s", part)
    return admin_ids


def create_bot(settings: Settings) -> Bot:
    if not settings.telegram_api_base:
        return Bot(token=settings.bot_token)

    api_server = TelegramAPIServer.from_base(
        settings.telegram_api_base,
        is_local=settings.telegram_api_is_local,
    )
    session = AiohttpSession(
        api=api_server,
        timeout=settings.telegram_request_timeout,
    )
    logging.info(
        "Using custom Telegram Bot API endpoint: %s (local=%s)",
        settings.telegram_api_base,
        settings.telegram_api_is_local,
    )
    return Bot(token=settings.bot_token, session=session)


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text=MAIN_BUTTON_HELP),
                KeyboardButton(text=MAIN_BUTTON_PDF_TOOLS),
            ]
        ],
        resize_keyboard=True,
    )


def feature_description() -> str:
    return (
        "Что умеет бот:\n"
        "\n"
        "- DJVU/DJV -> PDF\n"
        "- DOC/DOCX/ODT/RTF/TXT/HTML -> PDF\n"
        "- XLS/XLSX/ODS/CSV -> PDF\n"
        "- PPT/PPTX/ODP -> PDF\n"
        "- JPG/PNG/WebP/BMP/TIFF -> PDF\n"
        "- Фото, отправленное прямо в чат -> PDF\n"
        "\n"
        "Просто отправь файл или фото, затем нажми кнопку конвертации. "
        "OCR пока не используется, поэтому текст внутри сканов не распознается."
    )


def pdf_tools_description() -> str:
    return (
        "Интересные функции для PDF:\n"
        "\n"
        "- Склеить несколько PDF в один файл\n"
        "- Разделить PDF на отдельные страницы или выбрать определенные в один файл\n"
        "- Удалить выбранные страницы из PDF\n"        "\n"
        "Выбери действие ниже."
    )


def conversion_keyboard(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Конвертировать в PDF",
                    callback_data=f"{CONVERT_CALLBACK_PREFIX}{job_id}",
                )
            ],
        ]
    )


def album_conversion_keyboard(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Перевести каждый файл в PDF",
                    callback_data=f"{ALBUM_CONVERT_CALLBACK_PREFIX}{ALBUM_ACTION_INDIVIDUAL}:{job_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Перевести в PDF и склеить",
                    callback_data=f"{ALBUM_CONVERT_CALLBACK_PREFIX}{ALBUM_ACTION_MERGE}:{job_id}",
                )
            ],
        ]
    )


def pdf_tools_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Склеить PDF",
                    callback_data=f"{PDF_TOOL_CALLBACK_PREFIX}{PDF_TOOL_MERGE}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Разделить / вырезать страницы",
                    callback_data=f"{PDF_TOOL_CALLBACK_PREFIX}{PDF_TOOL_SPLIT}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Удалить страницы",
                    callback_data=f"{PDF_TOOL_CALLBACK_PREFIX}{PDF_TOOL_DELETE}",
                )
            ],
        ]
    )


def pdf_file_keyboard(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Склеить с другими PDF",
                    callback_data=f"{PDF_FILE_CALLBACK_PREFIX}{PDF_TOOL_MERGE}:{job_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Разделить / вырезать страницы",
                    callback_data=f"{PDF_FILE_CALLBACK_PREFIX}{PDF_TOOL_SPLIT}:{job_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Удалить страницы",
                    callback_data=f"{PDF_FILE_CALLBACK_PREFIX}{PDF_TOOL_DELETE}:{job_id}",
                )
            ],
        ]
    )


def merge_session_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Склеить выбранные PDF",
                    callback_data=f"{PDF_TOOL_CALLBACK_PREFIX}{PDF_TOOL_MERGE_DONE}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Отмена",
                    callback_data=f"{PDF_TOOL_CALLBACK_PREFIX}{PDF_TOOL_CANCEL}",
                )
            ],
        ]
    )


def split_session_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Разделить все страницы в ZIP",
                    callback_data=f"{PDF_TOOL_CALLBACK_PREFIX}{PDF_TOOL_SPLIT_ALL}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Отмена",
                    callback_data=f"{PDF_TOOL_CALLBACK_PREFIX}{PDF_TOOL_CANCEL}",
                )
            ],
        ]
    )


def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Отмена",
                    callback_data=f"{PDF_TOOL_CALLBACK_PREFIX}{PDF_TOOL_CANCEL}",
                )
            ]
        ]
    )


def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Метрики",
                    callback_data=f"{ADMIN_CALLBACK_PREFIX}stats",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Лимиты",
                    callback_data=f"{ADMIN_CALLBACK_PREFIX}limits",
                )
            ],
        ]
    )


def is_admin(user_id: int, settings: Settings) -> bool:
    return user_id in settings.admin_ids


def format_mb(bytes_value: int) -> str:
    return f"{bytes_value / (1024 * 1024):.1f} МБ"


def user_display_name(row: dict[str, object]) -> str:
    username = row.get("username")
    if username:
        return f"@{username}"
    full_name = row.get("full_name")
    if full_name:
        return str(full_name)
    return str(row.get("user_id", "unknown"))


def admin_stats_text(usage_db: BotDatabase) -> str:
    summary = usage_db.admin_summary()
    by_type = summary["by_type"]
    top_users = summary["top_users"]

    type_lines = ["по типам: нет данных"]
    if by_type:
        type_lines = [
            f"- {item['conversion_type']}: {item['count']}"
            for item in by_type
        ]

    top_lines = ["топ пользователей: нет данных"]
    if top_users:
        top_lines = [
            (
                f"- {user_display_name(item)}: {item['count']} конв., "
                f"{format_mb(int(item['input_bytes']))}"
            )
            for item in top_users
        ]

    return (
        "Метрики бота\n\n"
        f"Пользователей всего: {summary['users_total']}\n"
        f"Активных за 7 дней: {summary['users_week']}\n"
        f"Событий всего: {summary['events_total']}\n"
        f"Событий за 7 дней: {summary['events_week']}\n"
        f"Успешных за 7 дней: {summary['success_week']}\n"
        f"Ошибок за 7 дней: {summary['failed_week']}\n"
        f"Входящий объем за 7 дней: {format_mb(int(summary['input_bytes_week']))}\n"
        f"Исходящий объем за 7 дней: {format_mb(int(summary['output_bytes_week']))}\n\n"
        + "\n".join(type_lines)
        + "\n\n"
        + "\n".join(top_lines)
    )


def admin_limits_text(settings: Settings) -> str:
    file_limit = (
        str(settings.weekly_limit_files)
        if settings.weekly_limit_files > 0
        else "выключен"
    )
    mb_limit = (
        f"{settings.weekly_limit_mb} МБ"
        if settings.weekly_limit_mb > 0
        else "выключен"
    )
    admins = ", ".join(str(admin_id) for admin_id in sorted(settings.admin_ids)) or "не заданы"
    return (
        "Лимиты\n\n"
        f"Админы: {admins}\n"
        f"Файлов в неделю: {file_limit}\n"
        f"МБ входящих файлов в неделю: {mb_limit}\n\n"
        "Админы не ограничиваются недельными лимитами."
    )


def get_conversion_type(file_name: str | None) -> str | None:
    if not file_name:
        return None

    extension = Path(file_name).suffix.lower()
    if extension in DJVU_EXTENSIONS:
        return "djvu"
    if extension in DOCUMENT_EXTENSIONS:
        return "office"
    if extension in IMAGE_EXTENSIONS:
        return "image"
    return None


def safe_stem(file_name: str) -> str:
    stem = Path(file_name).stem
    stem = re.sub(r"[^\w._-]+", "_", stem).strip("._-")
    return stem or "converted"


def safe_pdf_name(file_name: str) -> str:
    return f"{safe_stem(file_name)}.pdf"


def is_pdf(file_name: str | None) -> bool:
    return bool(file_name and Path(file_name).suffix.lower() == ".pdf")


def input_name(job_id: str, file_name: str) -> str:
    extension = Path(file_name).suffix.lower()
    if not extension:
        extension = ".bin"
    return f"input_{job_id}{extension}"


def parse_page_ranges(text: str) -> list[int]:
    page_numbers: set[int] = set()
    for raw_part in text.replace(" ", "").split(","):
        if not raw_part:
            continue

        if "-" in raw_part:
            start_text, end_text = raw_part.split("-", 1)
            if not start_text.isdigit() or not end_text.isdigit():
                raise ValueError
            start = int(start_text)
            end = int(end_text)
            if start < 1 or end < start:
                raise ValueError
            page_numbers.update(range(start - 1, end))
        else:
            if not raw_part.isdigit():
                raise ValueError
            page = int(raw_part)
            if page < 1:
                raise ValueError
            page_numbers.add(page - 1)

    if not page_numbers:
        raise ValueError
    return sorted(page_numbers)


def store_pending_file(
    file_id: str,
    file_name: str,
    file_size: int | None,
    user_id: int,
    conversion_type: str,
    message_id: int,
    media_group_id: str | None,
) -> str:
    job_id = uuid.uuid4().hex
    pending_files[job_id] = PendingFile(
        file_id=file_id,
        file_name=file_name,
        file_size=file_size,
        user_id=user_id,
        conversion_type=conversion_type,
        message_id=message_id,
        media_group_id=media_group_id,
        sequence=next(file_sequence),
    )
    return job_id


def store_pending_pdf_upload(
    file_id: str,
    file_name: str,
    file_size: int | None,
    user_id: int,
    message_id: int,
    media_group_id: str | None,
) -> str:
    job_id = uuid.uuid4().hex
    pending_pdf_uploads[job_id] = PendingPdfUpload(
        file_id=file_id,
        file_name=file_name,
        file_size=file_size,
        user_id=user_id,
        message_id=message_id,
        media_group_id=media_group_id,
        sequence=next(file_sequence),
    )
    return job_id


def pending_pdf_to_tool_file(pending: PendingPdfUpload) -> PdfToolFile:
    return PdfToolFile(
        file_id=pending.file_id,
        file_name=pending.file_name,
        file_size=pending.file_size,
        message_id=pending.message_id,
        media_group_id=pending.media_group_id,
        sequence=pending.sequence,
    )


async def download_telegram_file(bot: Bot, file_id: str, destination: Path) -> None:
    await bot.download(
        file_id,
        destination=destination,
        timeout=DOWNLOAD_TIMEOUT_SECONDS,
    )


def file_size_mb(path: Path) -> float:
    return path.stat().st_size / (1024 * 1024)


def pdf_part_max_bytes(settings: Settings) -> int:
    if settings.telegram_api_base and settings.telegram_api_is_local:
        part_limit_mb = settings.max_output_mb
    else:
        part_limit_mb = min(settings.max_output_mb, TELEGRAM_SAFE_OUTPUT_MB)
    return max(1, part_limit_mb) * 1024 * 1024


def should_split_pdf_for_telegram(path: Path, settings: Settings) -> bool:
    return (
        path.suffix.lower() == ".pdf"
        and path.stat().st_size > pdf_part_max_bytes(settings)
    )


def output_size_error(path: Path, settings: Settings) -> str | None:
    max_bytes = settings.max_output_mb * 1024 * 1024
    if path.stat().st_size <= max_bytes:
        return None

    return (
        f"Результат получился слишком большим для отправки через Telegram: "
        f"{file_size_mb(path):.1f} МБ. Лимит сейчас: {settings.max_output_mb} МБ.\n\n"
        "Файл обработался, но бот не может отправить такой большой результат. "
        "Можно попробовать вырезать меньший диапазон страниц или разделить PDF на ZIP-части."
    )


async def send_pdf_result_in_parts(
    message: Message,
    path: Path,
    filename: str,
    caption: str,
    settings: Settings,
    reason: str,
) -> bool:
    if path.suffix.lower() != ".pdf":
        await message.answer(reason, reply_markup=main_menu_keyboard())
        return False

    parts_dir = path.parent / f"{path.stem}_parts"
    try:
        part_paths = await split_pdf_to_pdf_parts(
            path,
            parts_dir,
            safe_stem(filename),
            pdf_part_max_bytes(settings),
        )
    except ConversionError as exc:
        await message.answer(
            f"{reason}\n\nЕще я попробовал разделить PDF на части, но не получилось: {exc}",
            reply_markup=main_menu_keyboard(),
        )
        return False

    await message.answer(
        f"{reason}\n\nРазделяю PDF на {len(part_paths)} части и отправляю по очереди.",
        reply_markup=main_menu_keyboard(),
    )

    for index, part_path in enumerate(part_paths, start=1):
        part_caption = f"{caption}. Часть {index} из {len(part_paths)}."
        try:
            await message.answer_document(
                FSInputFile(part_path, filename=part_path.name),
                caption=part_caption,
                reply_markup=main_menu_keyboard(),
            )
        except Exception:
            logging.exception("Failed to send PDF part")
            await message.answer(
                f"Не получилось отправить часть {index} из {len(part_paths)}. "
                "Режим сброшен, можно попробовать меньший диапазон страниц или временно поставить `DJVU_PDF_QUALITY=screen` для меньшего размера.",
                reply_markup=main_menu_keyboard(),
            )
            return False

    return True


async def send_document_result(
    message: Message,
    path: Path,
    filename: str,
    caption: str,
    settings: Settings,
) -> bool:
    if should_split_pdf_for_telegram(path, settings):
        split_reason = (
            f"Файл создан ({file_size_mb(path):.1f} МБ), но для обычной отправки через Telegram он великоват."
        )
        return await send_pdf_result_in_parts(
            message,
            path,
            filename,
            caption,
            settings,
            split_reason,
        )

    size_error = output_size_error(path, settings)
    if size_error:
        return await send_pdf_result_in_parts(
            message,
            path,
            filename,
            caption,
            settings,
            size_error,
        )

    try:
        await message.answer_document(
            FSInputFile(path, filename=filename),
            caption=caption,
            reply_markup=main_menu_keyboard(),
        )
    except Exception:
        logging.exception("Failed to send result document")
        send_error = (
            f"Файл создан ({file_size_mb(path):.1f} МБ), но Telegram не принял его на отправку.\n\n"
            "Если бот работает через обычный Telegram Bot API, лучше держать `MAX_OUTPUT_MB=49`. "
            "Для лучшего качества с локальным Bot API используй `DJVU_PDF_QUALITY=printer`, а для меньшего размера - `screen`."
        )
        return await send_pdf_result_in_parts(
            message,
            path,
            filename,
            caption,
            settings,
            send_error,
        )
    return True


def check_size(file_size: int | None, settings: Settings) -> str | None:
    max_bytes = settings.max_input_mb * 1024 * 1024
    if file_size and file_size > max_bytes:
        return f"Файл слишком большой. Максимум: {settings.max_input_mb} МБ."
    return None


def touch_user_metrics(message: Message, usage_db: BotDatabase) -> None:
    if message.from_user is None:
        return
    usage_db.touch_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.full_name,
    )


def weekly_limit_error(
    user_id: int,
    file_size: int | None,
    settings: Settings,
    usage_db: BotDatabase,
    incoming_files: int = 1,
) -> str | None:
    if is_admin(user_id, settings):
        return None

    status = usage_db.check_weekly_limit(
        user_id,
        file_size,
        settings.weekly_limit_files,
        settings.weekly_limit_mb,
        incoming_files,
    )
    return status.message


def record_conversion_metric(
    usage_db: BotDatabase,
    pending: PendingFile,
    output_path: Path | None,
    status: str,
) -> None:
    output_bytes = 0
    if output_path is not None and output_path.exists():
        output_bytes = output_path.stat().st_size

    usage_db.record_conversion(
        pending.user_id,
        pending.conversion_type,
        pending.file_size,
        output_bytes,
        status,
    )


def ordered_pdf_files(files: list[PdfToolFile]) -> list[PdfToolFile]:
    return sorted(files, key=lambda item: (item.message_id, item.sequence))


def ordered_pending_files(files: list[PendingFile]) -> list[PendingFile]:
    return sorted(files, key=lambda item: (item.message_id, item.sequence))


async def send_merge_album_summary(
    message: Message,
    user_id: int,
    media_group_id: str,
) -> None:
    await asyncio.sleep(1.2)
    merge_album_tasks.pop((user_id, media_group_id), None)

    session = pdf_tool_sessions.get(user_id)
    if session is None or session.action != PDF_TOOL_MERGE:
        return

    await message.answer(
        f"PDF-файлы добавлены. Сейчас файлов: {len(session.files)}.",
        reply_markup=merge_session_keyboard(),
    )


def schedule_merge_album_summary(message: Message, user_id: int, media_group_id: str) -> None:
    key = (user_id, media_group_id)
    previous_task = merge_album_tasks.get(key)
    if previous_task is not None:
        previous_task.cancel()

    merge_album_tasks[key] = asyncio.create_task(
        send_merge_album_summary(message, user_id, media_group_id)
    )


async def send_pdf_upload_album_summary(
    message: Message,
    user_id: int,
    media_group_id: str,
) -> None:
    await asyncio.sleep(1.2)
    key = (user_id, media_group_id)
    pdf_upload_album_tasks.pop(key, None)
    uploads = pdf_upload_albums.pop(key, [])
    if not uploads:
        return

    files = [pending_pdf_to_tool_file(upload) for upload in uploads]
    pdf_tool_sessions[user_id] = PdfToolSession(
        action=PDF_TOOL_MERGE,
        files=ordered_pdf_files(files),
    )
    await message.answer(
        f"Получил PDF-файлы: {len(files)}. Можно сразу склеить их в один PDF.",
        reply_markup=merge_session_keyboard(),
    )


def schedule_pdf_upload_album_summary(
    message: Message,
    user_id: int,
    media_group_id: str,
) -> None:
    key = (user_id, media_group_id)
    previous_task = pdf_upload_album_tasks.get(key)
    if previous_task is not None:
        previous_task.cancel()

    pdf_upload_album_tasks[key] = asyncio.create_task(
        send_pdf_upload_album_summary(message, user_id, media_group_id)
    )


async def send_conversion_album_summary(
    message: Message,
    user_id: int,
    media_group_id: str,
) -> None:
    await asyncio.sleep(1.2)
    key = (user_id, media_group_id)
    conversion_album_tasks.pop(key, None)
    files = conversion_albums.pop(key, [])
    if not files:
        return

    job_id = uuid.uuid4().hex
    album_conversion_sessions[job_id] = ordered_pending_files(files)
    await message.answer(
        f"Получил файлов: {len(files)}. Что сделать?",
        reply_markup=album_conversion_keyboard(job_id),
    )


def schedule_conversion_album_summary(
    message: Message,
    user_id: int,
    media_group_id: str,
) -> None:
    key = (user_id, media_group_id)
    previous_task = conversion_album_tasks.get(key)
    if previous_task is not None:
        previous_task.cancel()

    conversion_album_tasks[key] = asyncio.create_task(
        send_conversion_album_summary(message, user_id, media_group_id)
    )


async def handle_start(message: Message) -> None:
    await message.answer(
        "Привет! Отправь документ, DJVU-файл или фото, а я превращу его в PDF.",
        reply_markup=main_menu_keyboard(),
    )


async def send_features(message: Message) -> None:
    await message.answer(feature_description(), reply_markup=main_menu_keyboard())


async def send_pdf_tools(message: Message) -> None:
    await message.answer(
        pdf_tools_description(),
        reply_markup=pdf_tools_keyboard(),
    )


async def handle_admin(
    message: Message,
    settings: Settings,
    usage_db: BotDatabase,
) -> None:
    touch_user_metrics(message, usage_db)
    if message.from_user is None or not is_admin(message.from_user.id, settings):
        await message.answer(
            "Админ-панель доступна только администратору.",
            reply_markup=main_menu_keyboard(),
        )
        return

    await message.answer("Админ-панель", reply_markup=admin_keyboard())


async def handle_admin_callback(
    callback: CallbackQuery,
    settings: Settings,
    usage_db: BotDatabase,
) -> None:
    if callback.data is None or not callback.data.startswith(ADMIN_CALLBACK_PREFIX):
        return

    if not is_admin(callback.from_user.id, settings):
        await callback.answer("Недостаточно прав.", show_alert=True)
        return

    if callback.message is None:
        await callback.answer("Не удалось открыть админ-панель.", show_alert=True)
        return

    action = callback.data.removeprefix(ADMIN_CALLBACK_PREFIX)
    await callback.answer()

    if action == "stats":
        await callback.message.answer(
            admin_stats_text(usage_db),
            reply_markup=admin_keyboard(),
        )
        return

    if action == "limits":
        await callback.message.answer(
            admin_limits_text(settings),
            reply_markup=admin_keyboard(),
        )
        return

    await callback.answer("Неизвестное действие.", show_alert=True)


async def handle_document(
    message: Message,
    bot: Bot,
    settings: Settings,
    usage_db: BotDatabase,
) -> None:
    document = message.document
    if document is None:
        return

    if message.from_user is None:
        await message.answer(
            "Не удалось определить отправителя файла.",
            reply_markup=main_menu_keyboard(),
        )
        return

    user_id = message.from_user.id
    touch_user_metrics(message, usage_db)
    if is_pdf(document.file_name) and user_id in pdf_tool_sessions:
        await handle_pdf_tool_document(message, bot, settings)
        return

    if is_pdf(document.file_name):
        size_error = check_size(document.file_size, settings)
        if size_error:
            await message.answer(size_error, reply_markup=main_menu_keyboard())
            return
        limit_error = weekly_limit_error(
            user_id,
            document.file_size,
            settings,
            usage_db,
        )
        if limit_error:
            await message.answer(limit_error, reply_markup=main_menu_keyboard())
            return

        file_name = document.file_name or "file.pdf"
        if message.media_group_id:
            key = (user_id, message.media_group_id)
            pdf_upload_albums.setdefault(key, []).append(
                PendingPdfUpload(
                    file_id=document.file_id,
                    file_name=file_name,
                    file_size=document.file_size,
                    user_id=user_id,
                    message_id=message.message_id,
                    media_group_id=message.media_group_id,
                    sequence=next(file_sequence),
                )
            )
            schedule_pdf_upload_album_summary(message, user_id, message.media_group_id)
            return

        job_id = store_pending_pdf_upload(
            file_id=document.file_id,
            file_name=file_name,
            file_size=document.file_size,
            user_id=user_id,
            message_id=message.message_id,
            media_group_id=message.media_group_id,
        )
        await message.answer(
            "PDF загружен. Что нужно сделать с этим файлом?",
            reply_markup=pdf_file_keyboard(job_id),
        )
        return

    conversion_type = get_conversion_type(document.file_name)
    if conversion_type is None:
        await message.answer(
            "Этот формат пока не поддерживается. Нажми кнопку описания, чтобы посмотреть список.",
            reply_markup=main_menu_keyboard(),
        )
        return

    size_error = check_size(document.file_size, settings)
    if size_error:
        await message.answer(size_error, reply_markup=main_menu_keyboard())
        return
    limit_error = weekly_limit_error(
        user_id,
        document.file_size,
        settings,
        usage_db,
    )
    if limit_error:
        await message.answer(limit_error, reply_markup=main_menu_keyboard())
        return

    file_name = document.file_name or "document"
    if message.media_group_id:
        key = (user_id, message.media_group_id)
        conversion_albums.setdefault(key, []).append(
            PendingFile(
                file_id=document.file_id,
                file_name=file_name,
                file_size=document.file_size,
                user_id=user_id,
                conversion_type=conversion_type,
                message_id=message.message_id,
                media_group_id=message.media_group_id,
                sequence=next(file_sequence),
            )
        )
        schedule_conversion_album_summary(message, user_id, message.media_group_id)
        return

    job_id = store_pending_file(
        file_id=document.file_id,
        file_name=file_name,
        file_size=document.file_size,
        user_id=user_id,
        conversion_type=conversion_type,
        message_id=message.message_id,
        media_group_id=message.media_group_id,
    )

    await message.answer(
        "Выберите действие:",
        reply_markup=conversion_keyboard(job_id),
    )


async def handle_photo(
    message: Message,
    settings: Settings,
    usage_db: BotDatabase,
) -> None:
    if not message.photo:
        return

    photo = message.photo[-1]
    size_error = check_size(photo.file_size, settings)
    if size_error:
        await message.answer(size_error, reply_markup=main_menu_keyboard())
        return

    if message.from_user is None:
        await message.answer(
            "Не удалось определить отправителя фото.",
            reply_markup=main_menu_keyboard(),
        )
        return

    user_id = message.from_user.id
    touch_user_metrics(message, usage_db)
    limit_error = weekly_limit_error(
        user_id,
        photo.file_size,
        settings,
        usage_db,
    )
    if limit_error:
        await message.answer(limit_error, reply_markup=main_menu_keyboard())
        return

    if message.media_group_id:
        key = (user_id, message.media_group_id)
        conversion_albums.setdefault(key, []).append(
            PendingFile(
                file_id=photo.file_id,
                file_name=f"photo_{message.message_id}.jpg",
                file_size=photo.file_size,
                user_id=user_id,
                conversion_type="image",
                message_id=message.message_id,
                media_group_id=message.media_group_id,
                sequence=next(file_sequence),
            )
        )
        schedule_conversion_album_summary(message, user_id, message.media_group_id)
        return

    job_id = store_pending_file(
        file_id=photo.file_id,
        file_name="photo.jpg",
        file_size=photo.file_size,
        user_id=user_id,
        conversion_type="image",
        message_id=message.message_id,
        media_group_id=message.media_group_id,
    )

    await message.answer(
        "Выберите действие:",
        reply_markup=conversion_keyboard(job_id),
    )


async def handle_conversion(
    callback: CallbackQuery,
    bot: Bot,
    settings: Settings,
    usage_db: BotDatabase,
) -> None:
    if callback.data is None or not callback.data.startswith(CONVERT_CALLBACK_PREFIX):
        return

    job_id = callback.data.removeprefix(CONVERT_CALLBACK_PREFIX)
    pending = pending_files.pop(job_id, None)

    if pending is None:
        await callback.answer("Файл уже обработан или бот был перезапущен.", show_alert=True)
        return

    if callback.from_user.id != pending.user_id:
        await callback.answer(
            "Эта кнопка относится к файлу другого пользователя.",
            show_alert=True,
        )
        pending_files[job_id] = pending
        return

    if callback.message is None:
        await callback.answer("Не удалось отправить результат в этот чат.", show_alert=True)
        return

    await callback.answer("Начинаю конвертацию...")

    job_dir = settings.workdir / job_id
    input_path = job_dir / input_name(job_id, pending.file_name)
    output_name = safe_pdf_name(pending.file_name)
    output_path = job_dir / output_name

    try:
        job_dir.mkdir(parents=True, exist_ok=True)

        await callback.message.answer("Скачиваю файл...")
        await download_telegram_file(bot, pending.file_id, input_path)

        if pending.conversion_type == "djvu":
            await callback.message.answer("Конвертирую в PDF...")
            await convert_djvu_to_pdf(input_path, output_path)
        elif pending.conversion_type == "office":
            await callback.message.answer("Конвертирую в PDF...")
            await convert_office_to_pdf(input_path, output_path)
        elif pending.conversion_type == "image":
            await callback.message.answer("Конвертирую в PDF...")
            await convert_image_to_pdf(input_path, output_path)
        else:
            raise ConversionError("Unsupported conversion type.")

        sent = await send_document_result(
            callback.message,
            output_path,
            output_name,
            "Готово",
            settings,
        )
        record_conversion_metric(
            usage_db,
            pending,
            output_path,
            "success" if sent else "failed",
        )
    except ConversionToolMissingError as exc:
        record_conversion_metric(usage_db, pending, output_path, "failed")
        await callback.message.answer(
            f"Не хватает инструмента для конвертации: {exc}",
            reply_markup=main_menu_keyboard(),
        )
    except ConversionError as exc:
        record_conversion_metric(usage_db, pending, output_path, "failed")
        await callback.message.answer(
            f"Не удалось конвертировать файл: {exc}",
            reply_markup=main_menu_keyboard(),
        )
    except Exception:
        record_conversion_metric(usage_db, pending, output_path, "failed")
        logging.exception("Unexpected conversion failure")
        await callback.message.answer(
            "Произошла неожиданная ошибка при конвертации.",
            reply_markup=main_menu_keyboard(),
        )
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)


async def convert_pending_file_to_pdf(
    bot: Bot,
    pending: PendingFile,
    job_dir: Path,
    index: int,
    usage_db: BotDatabase,
) -> tuple[Path, str]:
    input_path = job_dir / f"{index}_{input_name(str(pending.sequence), pending.file_name)}"
    output_name = f"{index}_{safe_pdf_name(pending.file_name)}"
    output_path = job_dir / output_name

    try:
        await download_telegram_file(bot, pending.file_id, input_path)

        if pending.conversion_type == "djvu":
            await convert_djvu_to_pdf(input_path, output_path)
        elif pending.conversion_type == "office":
            await convert_office_to_pdf(input_path, output_path)
        elif pending.conversion_type == "image":
            await convert_image_to_pdf(input_path, output_path)
        else:
            raise ConversionError("Unsupported conversion type.")
    except Exception:
        record_conversion_metric(usage_db, pending, output_path, "failed")
        raise

    record_conversion_metric(usage_db, pending, output_path, "success")

    return output_path, output_name


async def handle_album_conversion(
    callback: CallbackQuery,
    bot: Bot,
    settings: Settings,
    usage_db: BotDatabase,
) -> None:
    if callback.data is None or not callback.data.startswith(ALBUM_CONVERT_CALLBACK_PREFIX):
        return

    payload = callback.data.removeprefix(ALBUM_CONVERT_CALLBACK_PREFIX)
    action, _, job_id = payload.partition(":")
    files = album_conversion_sessions.pop(job_id, None)

    if not files:
        await callback.answer("Эта пачка файлов уже обработана или бот был перезапущен.", show_alert=True)
        return

    if callback.from_user.id not in {file.user_id for file in files}:
        album_conversion_sessions[job_id] = files
        await callback.answer(
            "Эта кнопка относится к файлам другого пользователя.",
            show_alert=True,
        )
        return

    if callback.message is None:
        await callback.answer("Не удалось отправить результат в этот чат.", show_alert=True)
        return

    if action not in {ALBUM_ACTION_INDIVIDUAL, ALBUM_ACTION_MERGE}:
        await callback.answer("Неизвестное действие.", show_alert=True)
        return

    total_input_bytes = sum(file.file_size or 0 for file in files)
    limit_error = weekly_limit_error(
        callback.from_user.id,
        total_input_bytes,
        settings,
        usage_db,
        incoming_files=len(files),
    )
    if limit_error:
        album_conversion_sessions[job_id] = files
        await callback.answer(limit_error, show_alert=True)
        return

    await callback.answer("Начинаю обработку...")

    job_dir = settings.workdir / job_id
    try:
        job_dir.mkdir(parents=True, exist_ok=True)
        await callback.message.answer("Скачиваю и конвертирую файлы в PDF...")

        converted: list[tuple[Path, str]] = []
        for index, pending in enumerate(ordered_pending_files(files), start=1):
            converted.append(
                await convert_pending_file_to_pdf(
                    bot,
                    pending,
                    job_dir,
                    index,
                    usage_db,
                )
            )

        if action == ALBUM_ACTION_INDIVIDUAL:
            for output_path, output_name in converted:
                await send_document_result(
                    callback.message,
                    output_path,
                    output_name,
                    "Готово. PDF создан без OCR.",
                    settings,
                )
            return

        await callback.message.answer("Склеиваю PDF...")
        merged_path = job_dir / "converted_merged.pdf"
        await merge_pdfs([path for path, _ in converted], merged_path)
        await send_document_result(
            callback.message,
            merged_path,
            "converted_merged.pdf",
            "Готово. Файлы переведены в PDF и склеены.",
            settings,
        )
    except ConversionToolMissingError as exc:
        await callback.message.answer(
            f"Не хватает инструмента для конвертации: {exc}",
            reply_markup=main_menu_keyboard(),
        )
    except ConversionError as exc:
        await callback.message.answer(
            f"Не удалось обработать пачку файлов: {exc}",
            reply_markup=main_menu_keyboard(),
        )
    except Exception:
        logging.exception("Unexpected album conversion failure")
        await callback.message.answer(
            "Произошла неожиданная ошибка при обработке пачки файлов. Попробуйте заново.",
            reply_markup=main_menu_keyboard(),
        )
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)


async def handle_pdf_file_callback(callback: CallbackQuery) -> None:
    if callback.data is None or not callback.data.startswith(PDF_FILE_CALLBACK_PREFIX):
        return

    payload = callback.data.removeprefix(PDF_FILE_CALLBACK_PREFIX)
    action, _, job_id = payload.partition(":")
    pending = pending_pdf_uploads.pop(job_id, None)

    if pending is None:
        await callback.answer("PDF уже обработан или бот был перезапущен.", show_alert=True)
        return

    if callback.from_user.id != pending.user_id:
        pending_pdf_uploads[job_id] = pending
        await callback.answer(
            "Эта кнопка относится к файлу другого пользователя.",
            show_alert=True,
        )
        return

    if callback.message is None:
        await callback.answer("Не удалось продолжить действие в этом чате.", show_alert=True)
        return

    pdf_file = pending_pdf_to_tool_file(pending)

    if action == PDF_TOOL_MERGE:
        pdf_tool_sessions[callback.from_user.id] = PdfToolSession(
            action=PDF_TOOL_MERGE,
            files=[pdf_file],
        )
        await callback.answer()
        await callback.message.answer(
            "Первый PDF уже добавлен. Пришли еще один или несколько PDF, затем нажми кнопку склейки.",
            reply_markup=merge_session_keyboard(),
        )
        return

    if action == PDF_TOOL_SPLIT:
        pdf_tool_sessions[callback.from_user.id] = PdfToolSession(
            action=PDF_TOOL_SPLIT,
            files=[pdf_file],
            waiting_for_ranges=True,
        )
        await callback.answer()
        await callback.message.answer(
            "Можно нажать «Разделить все страницы в ZIP» или написать диапазон страниц для отдельного PDF, например: 1-3,5.",
            reply_markup=split_session_keyboard(),
        )
        return

    if action == PDF_TOOL_DELETE:
        pdf_tool_sessions[callback.from_user.id] = PdfToolSession(
            action=PDF_TOOL_DELETE,
            files=[pdf_file],
            waiting_for_ranges=True,
        )
        await callback.answer()
        await callback.message.answer(
            "Напиши страницы, которые нужно удалить, например: 2,5-7.",
            reply_markup=cancel_keyboard(),
        )
        return

    await callback.answer("Неизвестное действие.", show_alert=True)


async def handle_pdf_tool_callback(
    callback: CallbackQuery,
    bot: Bot,
    settings: Settings,
) -> None:
    if callback.data is None or not callback.data.startswith(PDF_TOOL_CALLBACK_PREFIX):
        return

    action = callback.data.removeprefix(PDF_TOOL_CALLBACK_PREFIX)
    user_id = callback.from_user.id

    if action == PDF_TOOL_CANCEL:
        pdf_tool_sessions.pop(user_id, None)
        await callback.answer("Отменено.")
        if callback.message is not None:
            await callback.message.answer("Действие отменено.", reply_markup=main_menu_keyboard())
        return

    if action in {PDF_TOOL_MERGE, PDF_TOOL_SPLIT, PDF_TOOL_DELETE}:
        pdf_tool_sessions[user_id] = PdfToolSession(action=action)
        await callback.answer()
        if callback.message is None:
            return
        if action == PDF_TOOL_MERGE:
            await callback.message.answer(
                "Пришли два или больше PDF-файла. Когда все файлы будут отправлены, нажми кнопку склейки.",
                reply_markup=merge_session_keyboard(),
            )
        elif action == PDF_TOOL_SPLIT:
            await callback.message.answer(
                "Пришли один PDF-файл. После этого можно нажать «Разделить все страницы в ZIP» или написать диапазон страниц для отдельного PDF, например: 1-3,5.",
                reply_markup=cancel_keyboard(),
            )
        else:
            await callback.message.answer(
                "Пришли один PDF-файл. Затем напиши страницы для удаления, например: 2,5-7.",
                reply_markup=cancel_keyboard(),
            )
        return

    if action == PDF_TOOL_MERGE_DONE:
        await merge_pdf_session(callback, bot, settings)
        return

    if action == PDF_TOOL_SPLIT_ALL:
        await split_pdf_session(callback, bot, settings)
        return


async def handle_pdf_tool_document(
    message: Message,
    bot: Bot,
    settings: Settings,
) -> None:
    document = message.document
    if document is None or message.from_user is None:
        return

    user_id = message.from_user.id
    session = pdf_tool_sessions.get(user_id)
    if session is None:
        return

    if not is_pdf(document.file_name):
        await message.answer("Для этой функции нужен PDF-файл.", reply_markup=main_menu_keyboard())
        return

    size_error = check_size(document.file_size, settings)
    if size_error:
        await message.answer(size_error, reply_markup=main_menu_keyboard())
        return

    file_name = document.file_name or "file.pdf"
    session.files.append(
        PdfToolFile(
            file_id=document.file_id,
            file_name=file_name,
            file_size=document.file_size,
            message_id=message.message_id,
            media_group_id=message.media_group_id,
            sequence=next(file_sequence),
        )
    )

    if session.action == PDF_TOOL_MERGE:
        session.files = ordered_pdf_files(session.files)
        if message.media_group_id:
            schedule_merge_album_summary(message, user_id, message.media_group_id)
            return

        await message.answer(
            f"PDF добавлен. Сейчас файлов: {len(session.files)}. Можно добавить еще файлы...",
            reply_markup=merge_session_keyboard(),
        )
        return

    if session.action == PDF_TOOL_SPLIT:
        session.files = session.files[-1:]
        session.waiting_for_ranges = True
        await message.answer(
            "PDF добавлен. Нажми «Разделить все страницы в ZIP» или напиши диапазон страниц для отдельного PDF, например: 1-3,5.",
            reply_markup=split_session_keyboard(),
        )
        return

    if session.action == PDF_TOOL_DELETE:
        session.files = session.files[-1:]
        session.waiting_for_ranges = True
        await message.answer(
            "PDF добавлен. Напиши страницы, которые нужно удалить, например: 2,5-7.",
            reply_markup=cancel_keyboard(),
        )
        return


async def download_pdf_tool_file(
    bot: Bot,
    pdf_file: PdfToolFile,
    job_dir: Path,
    index: int,
) -> Path:
    input_path = job_dir / f"{index}_{safe_stem(pdf_file.file_name)}.pdf"
    await download_telegram_file(bot, pdf_file.file_id, input_path)
    return input_path


async def merge_pdf_session(callback: CallbackQuery, bot: Bot, settings: Settings) -> None:
    session = pdf_tool_sessions.get(callback.from_user.id)
    if session is None or session.action != PDF_TOOL_MERGE:
        await callback.answer("Сначала выбери склейку PDF.", show_alert=True)
        return

    if len(session.files) < 2:
        await callback.answer("Нужно минимум два PDF-файла.", show_alert=True)
        return

    if callback.message is None:
        await callback.answer("Не удалось отправить результат в этот чат.", show_alert=True)
        return

    await callback.answer("Склеиваю PDF...")

    job_id = uuid.uuid4().hex
    job_dir = settings.workdir / job_id
    output_path = job_dir / "merged.pdf"

    try:
        job_dir.mkdir(parents=True, exist_ok=True)
        await callback.message.answer("Скачиваю PDF-файлы...")
        input_paths = [
            await download_pdf_tool_file(bot, pdf_file, job_dir, index)
            for index, pdf_file in enumerate(ordered_pdf_files(session.files), start=1)
        ]

        await callback.message.answer("Склеиваю PDF...")
        await merge_pdfs(input_paths, output_path)
        sent = await send_document_result(
            callback.message,
            output_path,
            "merged.pdf",
            "Готово. PDF-файлы склеены.",
            settings,
        )
        if sent:
            pdf_tool_sessions.pop(callback.from_user.id, None)
        else:
            pdf_tool_sessions.pop(callback.from_user.id, None)
            await callback.message.answer(
                "Режим склейки сброшен. Можно начать заново с меньшими файлами.",
                reply_markup=main_menu_keyboard(),
            )
    except ConversionError as exc:
        await callback.message.answer(
            f"Не удалось склеить PDF: {exc}\n\n"
            "Режим склейки остается активным: можно добавить другой PDF, нажать склейку еще раз или отменить.",
            reply_markup=merge_session_keyboard(),
        )
    except Exception:
        logging.exception("Unexpected PDF merge failure")
        pdf_tool_sessions.pop(callback.from_user.id, None)
        await callback.message.answer(
            "Произошла неожиданная ошибка при склейке PDF. Я сбросил режим склейки, можно начать заново.",
            reply_markup=main_menu_keyboard(),
        )
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)


async def split_pdf_session(callback: CallbackQuery, bot: Bot, settings: Settings) -> None:
    session = pdf_tool_sessions.get(callback.from_user.id)
    if session is None or session.action != PDF_TOOL_SPLIT or not session.files:
        await callback.answer("Сначала пришли PDF для разделения.", show_alert=True)
        return

    if callback.message is None:
        await callback.answer("Не удалось отправить результат в этот чат.", show_alert=True)
        return

    await callback.answer("Разделяю PDF...")

    job_id = uuid.uuid4().hex
    job_dir = settings.workdir / job_id
    pdf_file = session.files[-1]
    input_path = job_dir / f"input_{safe_stem(pdf_file.file_name)}.pdf"

    try:
        job_dir.mkdir(parents=True, exist_ok=True)
        await download_telegram_file(bot, pdf_file.file_id, input_path)
        zip_paths = await split_pdf_to_zip_parts(
            input_path,
            job_dir,
            safe_stem(pdf_file.file_name),
        )
        for index, zip_path in enumerate(zip_paths, start=1):
            caption = "Готово. Каждая страница лежит отдельным PDF внутри ZIP."
            if len(zip_paths) > 1:
                caption = f"Часть {index} из {len(zip_paths)}. Страницы лежат отдельными PDF внутри ZIP."
            await send_document_result(
                callback.message,
                zip_path,
                zip_path.name,
                caption,
                settings,
            )
        pdf_tool_sessions.pop(callback.from_user.id, None)
    except ConversionError as exc:
        await callback.message.answer(
            f"Не удалось разделить PDF: {exc}\n\n"
            "Режим разделения остается активным: можно написать диапазон страниц или нажать «Отмена».",
            reply_markup=split_session_keyboard(),
        )
    except Exception:
        logging.exception("Unexpected PDF split failure")
        pdf_tool_sessions.pop(callback.from_user.id, None)
        await callback.message.answer(
            "Произошла неожиданная ошибка при разделении PDF. Я сбросил режим разделения, можно начать заново.",
            reply_markup=main_menu_keyboard(),
        )
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)


async def handle_pdf_range_text(
    message: Message,
    bot: Bot,
    settings: Settings,
) -> bool:
    if message.from_user is None or not message.text:
        return False

    user_id = message.from_user.id
    session = pdf_tool_sessions.get(user_id)
    if session is None or not session.waiting_for_ranges or not session.files:
        return False

    if session.action not in {PDF_TOOL_SPLIT, PDF_TOOL_DELETE}:
        return False

    try:
        page_numbers = parse_page_ranges(message.text)
    except ValueError:
        await message.answer(
            "Не понял страницы. Напиши так: 1-3,5 или 2,4,8.",
            reply_markup=main_menu_keyboard(),
        )
        return True

    job_id = uuid.uuid4().hex
    job_dir = settings.workdir / job_id
    pdf_file = session.files[-1]
    input_path = job_dir / f"input_{safe_stem(pdf_file.file_name)}.pdf"

    if session.action == PDF_TOOL_SPLIT:
        output_name = f"{safe_stem(pdf_file.file_name)}_pages.pdf"
        output_path = job_dir / output_name
        action_text = "вырезать страницы"
        result_caption = "Готово. Выбранные страницы сохранены в отдельный PDF."
    else:
        output_name = f"{safe_stem(pdf_file.file_name)}_without_pages.pdf"
        output_path = job_dir / output_name
        action_text = "удалить страницы"
        result_caption = "Готово. Выбранные страницы удалены."

    try:
        job_dir.mkdir(parents=True, exist_ok=True)
        await message.answer(f"Пробую {action_text}...")
        await download_telegram_file(bot, pdf_file.file_id, input_path)

        if session.action == PDF_TOOL_SPLIT:
            await extract_pdf_pages(input_path, output_path, page_numbers)
        else:
            await delete_pdf_pages(input_path, output_path, page_numbers)

        sent = await send_document_result(
            message,
            output_path,
            output_name,
            result_caption,
            settings,
        )
        if sent:
            pdf_tool_sessions.pop(user_id, None)
        else:
            pdf_tool_sessions.pop(user_id, None)
            await message.answer(
                "Режим работы с PDF сброшен. Можно начать заново или попробовать меньший диапазон страниц.",
                reply_markup=main_menu_keyboard(),
            )
    except ConversionError as exc:
        await message.answer(
            f"Не удалось обработать PDF: {exc}\n\n"
            "Попробуйте еще раз. Можно написать другой диапазон страниц, например 1-3,5, или нажать «Отмена».",
            reply_markup=cancel_keyboard(),
        )
    except Exception:
        logging.exception("Unexpected PDF page operation failure")
        pdf_tool_sessions.pop(user_id, None)
        await message.answer(
            "Произошла неожиданная ошибка при обработке PDF. Я сбросил режим работы с этим файлом, можно начать заново.",
            reply_markup=main_menu_keyboard(),
        )
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)

    return True


async def handle_plain_text(message: Message, bot: Bot, settings: Settings) -> None:
    if message.text and message.text.startswith(("/start", "/help")):
        return

    if message.text == MAIN_BUTTON_HELP:
        await send_features(message)
        return

    if message.text == MAIN_BUTTON_PDF_TOOLS:
        await send_pdf_tools(message)
        return

    if await handle_pdf_range_text(message, bot, settings):
        return

    await message.answer(
        "Кажется, ты отправил текст без файла. Пришли документ, DJVU-файл или фото, "
        "а затем нажми кнопку конвертации.",
        reply_markup=main_menu_keyboard(),
    )


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    settings = load_settings()
    settings.workdir.mkdir(parents=True, exist_ok=True)

    for tool_name in ("djvused", "djvups", "pdftoppm", "ps2pdf", "qpdf", "soffice", "tesseract"):
        if shutil.which(tool_name) is None:
            logging.warning("%s is not available. Some conversions may fail.", tool_name)

    bot = create_bot(settings)
    dp = Dispatcher()
    dp["settings"] = settings

    dp.message.register(handle_start, Command("start", "help"))
    dp.message.register(handle_document, F.document)
    dp.message.register(handle_photo, F.photo)
    dp.callback_query.register(
        handle_conversion,
        F.data.startswith(CONVERT_CALLBACK_PREFIX),
    )
    dp.callback_query.register(
        handle_album_conversion,
        F.data.startswith(ALBUM_CONVERT_CALLBACK_PREFIX),
    )
    dp.callback_query.register(
        handle_pdf_file_callback,
        F.data.startswith(PDF_FILE_CALLBACK_PREFIX),
    )
    dp.callback_query.register(
        handle_pdf_tool_callback,
        F.data.startswith(PDF_TOOL_CALLBACK_PREFIX),
    )
    dp.message.register(handle_plain_text, F.text)

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
