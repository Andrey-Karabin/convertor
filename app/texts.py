"""Every text the bot shows. Keeping them here makes a second language easy later."""

from __future__ import annotations

import time
from html import escape
from pathlib import Path

# --- main menu -------------------------------------------------------------

BTN_HELP = "📄 Что умеет бот"
BTN_PDF_TOOLS = "🧰 PDF-инструменты"
BTN_BALANCE = "💎 Баланс"

START = (
    "Привет! Пришлите документ, DjVu, фото или архив — я переведу в PDF.\n"
    "Во всех PDF я распознаю текст, так что по ним можно искать и копировать текст."
)

FEATURES = (
    "<b>Что умеет бот</b>\n\n"
    "• DjVu → PDF\n"
    "• Word, Excel, PowerPoint, ODT, RTF, TXT, HTML → PDF\n"
    "• JPG, PNG, WebP, BMP, TIFF и фото из чата → PDF\n"
    "• Распознавание текста (OCR) во всех PDF — русский и английский\n"
    "• Сжатие фото и PDF\n"
    "• Склейка, разделение и удаление страниц PDF\n"
    "• Безопасная распаковка ZIP, RAR, 7Z, TAR\n\n"
    "Можно прислать сразу несколько файлов одним сообщением — бот предложит "
    "перевести каждый или склеить в один PDF."
)

PDF_TOOLS = "<b>PDF-инструменты</b>\n\nВыберите действие, затем пришлите PDF."

UNSUPPORTED = "Этот формат пока не поддерживается. Нажмите «{help}», чтобы посмотреть список.".format(help=BTN_HELP)
TEXT_WITHOUT_FILE = "Пришлите файл или фото — и выберите, что с ним сделать."
CHOOSE_ACTION = "Что сделать с файлом <b>{name}</b>?"
CHOOSE_GROUP_ACTION = "Получил файлов: {count}. Что сделать?"
PDF_CHOOSE_ACTION = "PDF <b>{name}</b> получен. Что сделать?"
BUTTON_EXPIRED = "Кнопка устарела. Пришлите файл заново."
FOREIGN_BUTTON = "Эта кнопка относится к файлу другого пользователя."
SKIPPED_FILES = "Пропустил неподдерживаемые файлы: {names}"
TOO_BIG = "Файл <b>{name}</b> слишком большой: {size:.1f} МБ. Максимум: {limit} МБ."

# --- action buttons --------------------------------------------------------

BTN_TO_PDF = "📄 В PDF"
BTN_COMPRESS_PHOTO = "🗜 Сжать фото"
BTN_EACH_TO_PDF = "📄 Каждый файл в PDF"
BTN_MERGE_TO_PDF = "📚 Всё в один PDF"
BTN_PHOTOS_ZIP = "🗜 Сжать фото в ZIP"
BTN_ARCHIVE_ALL = "📦 Распаковать всё"
BTN_ARCHIVE_PICK = "🔎 Выбрать файл"
BTN_MERGE = "📚 Склеить PDF"
BTN_MERGE_WITH = "📚 Склеить с другими PDF"
BTN_SPLIT = "✂️ Разделить / вырезать страницы"
BTN_DELETE = "🗑 Удалить страницы"
BTN_COMPRESS_PDF = "🗜 Сжать PDF"
BTN_MERGE_DONE = "✅ Склеить"
BTN_SPLIT_ALL = "📦 Каждую страницу отдельно (ZIP)"
BTN_CANCEL = "Отмена"
BTN_CANCEL_JOB = "✖️ Отменить"
BTN_BACK = "← Назад"
BTN_NEXT = "Вперёд →"
BTN_BUY = "💎 Купить операции"

# --- PDF tools flow --------------------------------------------------------

ASK_MERGE_FILES = "Пришлите два или больше PDF. Когда всё отправите — нажмите «{done}».".format(done=BTN_MERGE_DONE)
ASK_MORE_FOR_MERGE = "Первый PDF добавлен. Пришлите ещё один или несколько PDF и нажмите «{done}».".format(done=BTN_MERGE_DONE)
MERGE_ADDED = "Добавлено. PDF в списке: {count}."
MERGE_NEED_TWO = "Нужно минимум два PDF."
ASK_SPLIT_FILE = "Пришлите PDF, который нужно разделить."
ASK_DELETE_FILE = "Пришлите PDF, из которого нужно удалить страницы."
ASK_COMPRESS_FILE = "Пришлите PDF, который нужно сжать."
ASK_SPLIT_RANGES = (
    "Напишите страницы, которые нужно вырезать в отдельный PDF, например: <code>1-3,5</code>\n"
    "или нажмите «{all}».".format(all=BTN_SPLIT_ALL)
)
ASK_DELETE_RANGES = "Напишите страницы, которые нужно удалить, например: <code>2,5-7</code>"
BAD_RANGES = "Не понял номера страниц. Напишите так: <code>1-3,5</code> или <code>2,4,8</code>"
NEED_PDF = "Для этого действия нужен PDF-файл."
CANCELLED = "Отменено."

# --- queue -----------------------------------------------------------------

STAGE_STARTING = "Начинаю"
STAGE_DOWNLOADING = "Скачиваю"
STAGE_CONVERTING = "Перевожу в PDF"
STAGE_MERGING = "Склеиваю"
STAGE_COMPRESSING = "Сжимаю"
STAGE_PROCESSING = "Обрабатываю"
STAGE_READING_ARCHIVE = "Читаю список файлов"
STAGE_EXTRACTING = "Распаковываю"
STAGE_OCR = "Распознаю текст (OCR) — для больших сканов это может занять несколько минут"
STAGE_SENDING = "Отправляю"

TOO_MANY_JOBS = "У вас уже {count} задачи в работе. Дождитесь их завершения или отмените одну."
ERROR_TOOL_MISSING = "На сервере не хватает программы для этой операции. Администратор уже видит ошибку в логах."
ERROR_UNEXPECTED = "Произошла неожиданная ошибка. Попробуйте ещё раз."
ERROR_SEND_FAILED = "Не получилось отправить результат."
OCR_SKIPPED = "⚠️ Текст распознать не удалось — PDF без текстового слоя."
JOB_NOT_FOUND = "Задача уже завершена."


def job_queued(title: str, position: int, running: int) -> str:
    if position <= 1 and running == 0:
        return f"🕐 {title}\nСейчас начну…"
    return f"🕐 {title}\nВаше место в очереди: {position}"


def job_running(title: str, stage: str) -> str:
    return f"⚙️ {title}\n{stage}…"


def job_resumed(title: str) -> str:
    return f"🔄 {title}\nБот перезапускался — задача снова в очереди."


def job_cancelled(title: str) -> str:
    return f"🚫 {title}\nОтменено. Операции возвращены на баланс."


def job_failed(title: str, reason: str, refunded: bool) -> str:
    tail = "\nОперация возвращена на баланс." if refunded else ""
    return f"❌ {title}\n{escape(reason)}{tail}"


# --- results ---------------------------------------------------------------

DONE_CONVERTED = "Готово ✅"
DONE_MERGED = "Готово ✅ Файлы склеены в один PDF."
DONE_IMAGES_ZIP = "Готово ✅ Фото сжаты и упакованы в ZIP."
DONE_PAGES_EXTRACTED = "Готово ✅ Выбранные страницы в отдельном PDF."
DONE_PAGES_DELETED = "Готово ✅ Страницы удалены."


def done_compressed(before_bytes: int, after: Path) -> str:
    mb = 1024 * 1024
    return f"Готово ✅ Было {before_bytes / mb:.1f} МБ, стало {after.stat().st_size / mb:.1f} МБ."


def split_caption(number: int, total: int) -> str:
    base = "Каждая страница — отдельный PDF внутри ZIP."
    return base if total == 1 else f"Часть {number} из {total}. {base}"


def file_of(number: int, total: int) -> str:
    return f"Файл {number} из {total}"


def part_caption(number: int, total: int) -> str:
    return f"Часть {number} из {total}"


def result_too_big(size_mb: float, limit_mb: int) -> str:
    return f"Результат весит {size_mb:.1f} МБ — больше лимита Telegram ({limit_mb} МБ)."


def sending_in_parts(size_mb: float, parts: int) -> str:
    return f"PDF весит {size_mb:.1f} МБ — отправляю частями ({parts} шт.)."


# --- archives --------------------------------------------------------------

ARCHIVE_EXPIRED = "Список файлов устарел. Пришлите архив заново."


def archive_pick(count: int) -> str:
    return f"В архиве файлов: {count}. Выберите, какой прислать."


# --- titles for the queue --------------------------------------------------

def title_convert(names: list[str], merge: bool) -> str:
    if len(names) == 1:
        return f"{escape(names[0])} → PDF"
    return f"{len(names)} файлов → {'один PDF' if merge else 'PDF'}"


def title_for(action: str, name: str) -> str:
    name = escape(name)
    return {
        "compress_images": f"Сжатие фото: {name}",
        "pdf_merge": f"Склейка PDF ({name})",
        "pdf_extract": f"Вырезаю страницы: {name}",
        "pdf_delete": f"Удаляю страницы: {name}",
        "pdf_split_all": f"Разделение: {name}",
        "pdf_compress": f"Сжатие PDF: {name}",
        "archive_list": f"Архив: {name}",
        "archive_extract": f"Распаковка: {name}",
    }.get(action, name)


# --- balance & payments ----------------------------------------------------

LIMIT_REACHED = (
    "Бесплатные операции на этой неделе закончились, а на балансе не хватает операций "
    "(нужно {needed}, доступно {available}).\n\nМожно купить пакет операций за Telegram Stars ⭐."
)
BUY_TITLE = "Пакеты операций"
BUY_INTRO = (
    "1 операция = 1 файл (конвертация, сжатие, склейка считается по числу файлов).\n"
    "Оплата — Telegram Stars ⭐. Покупая, вы соглашаетесь с /terms."
)
NO_PRODUCTS = "Продажа сейчас не настроена."
PAYMENT_THANKS = "Спасибо за покупку! ⭐\n\n{balance}"
PAYMENT_UNKNOWN = "Этот товар больше недоступен."


def product_title(kind: str, amount: int) -> str:
    return f"{amount} операций" if kind == "credits" else f"Безлимит на {amount} дн."


def product_description(kind: str, amount: int) -> str:
    if kind == "credits":
        return f"{amount} операций с файлами. Не сгорают."
    return f"Неограниченное число операций в течение {amount} дней."


def balance_text(balance, billing_enabled: bool) -> str:
    if not billing_enabled or balance.unlimited:
        return "💎 <b>Баланс</b>\n\nДля вас ограничений нет."
    lines = ["💎 <b>Баланс</b>", ""]
    if balance.has_premium:
        until = time.strftime("%d.%m.%Y", time.localtime(balance.premium_until))
        lines.append(f"Безлимит до {until}")
    lines.append(f"Бесплатно на этой неделе: {balance.free_left} из {balance.free_total}")
    lines.append(f"Купленные операции: {balance.credits}")
    return "\n".join(lines)


TERMS = (
    "<b>Условия</b>\n\n"
    "• Операции покупаются за Telegram Stars и зачисляются сразу после оплаты.\n"
    "• Купленные операции не сгорают. Если обработка не удалась, операция возвращается автоматически.\n"
    "• Файлы удаляются с сервера сразу после обработки.\n"
    "• Вопросы по оплате и возвратам: /paysupport"
)


def paysupport(contact: str) -> str:
    who = contact or "администратору бота"
    return (
        "<b>Поддержка по оплате</b>\n\n"
        f"Напишите {who}: укажите дату покупки и что пошло не так. "
        "Если операции не были использованы, Stars вернём."
    )


# --- admin -----------------------------------------------------------------

ADMIN_ONLY = "Эта команда только для администратора."
ADMIN_TITLE = "Админ-панель"
GRANT_USAGE = "Использование: <code>/grant user_id количество</code> (можно отрицательное)"
REFUND_USAGE = "Использование: <code>/refund charge_id</code> — id платежа есть в /admin → Платежи пользователя"


def admin_stats(s: dict) -> str:
    mb = 1024 * 1024
    by_type = "\n".join(f"• {kind}: {n}" for kind, n in s["by_type"]) or "• нет данных"
    top = "\n".join(
        f"• {('@' + u['username']) if u.get('username') else (u.get('full_name') or u['user_id'])}: {u['n']}"
        for u in s["top_users"]
    ) or "• нет данных"
    return (
        "<b>Статистика</b>\n\n"
        f"Пользователей: {s['users_total']} (за 7 дней: {s['users_week']})\n"
        f"Успешных операций за 7 дней: {s['success_week']}, ошибок: {s['failed_week']}\n"
        f"Входящий объём за 7 дней: {s['input_bytes_week'] / mb:.1f} МБ\n"
        f"Сейчас в очереди: {s['queued']}\n"
        f"Stars: всего {s['stars_total']} ⭐, за 7 дней {s['stars_week']} ⭐\n\n"
        f"<b>По типам</b>\n{by_type}\n\n<b>Топ пользователей</b>\n{top}"
    )


def admin_settings(settings) -> str:
    products = ", ".join(f"{product_title(p.kind, p.amount)} = {p.stars}⭐" for p in settings.products) or "нет"
    return (
        "<b>Настройки</b>\n\n"
        f"Оплата и лимиты: {'включены' if settings.billing_enabled else 'выключены'}\n"
        f"Бесплатно в неделю: {settings.free_weekly_ops} операций\n"
        f"Пакеты: {products}\n"
        f"Файл на вход: до {settings.effective_max_input_mb} МБ, на выход: до {settings.effective_max_output_mb} МБ\n"
        f"Очередь: {settings.queue_workers} параллельных задач, до {settings.user_max_jobs} на пользователя\n"
        f"OCR: {'включён' if settings.ocr_enabled else 'выключен'} ({settings.ocr_languages})"
    )
