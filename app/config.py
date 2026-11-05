"""All bot settings in one place.

Values come from environment variables (docker compose passes them from `.env`).
For a local run without Docker, `.env` in the project root is read as well.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

TRUE_VALUES = {"1", "true", "yes", "on"}


def load_env_file(path: Path = Path(".env")) -> None:
    """Load KEY=VALUE lines into os.environ without overriding real env vars."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


def _str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _int(name: str, default: int, minimum: int | None = None) -> int:
    raw = _str(name)
    if not raw:
        value = default
    else:
        try:
            value = int(raw)
        except ValueError:
            log.warning("%s=%r is not a number, using %s", name, raw, default)
            value = default
    if minimum is not None:
        value = max(minimum, value)
    return value


def _float(name: str, default: float) -> float:
    try:
        return float(_str(name) or default)
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = _str(name)
    if not raw:
        return default
    return raw.lower() in TRUE_VALUES


def _id_set(name: str) -> frozenset[int]:
    ids = set()
    for part in _str(name).replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError:
            log.warning("Ignoring invalid id in %s: %s", name, part)
    return frozenset(ids)


@dataclass(frozen=True)
class Product:
    """Something a user can buy for Telegram Stars."""

    code: str  # used as invoice payload, e.g. "c50" or "p30"
    kind: str  # "credits" or "premium"
    amount: int  # credits count or premium days
    stars: int


def parse_products(raw: str) -> tuple[Product, ...]:
    """Parse `credits:50:25,premium:30:150` (kind:amount:stars)."""
    products = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            kind, amount, stars = (piece.strip() for piece in part.split(":"))
            amount_i, stars_i = int(amount), int(stars)
        except ValueError:
            log.warning("Ignoring invalid product %r (expected kind:amount:stars)", part)
            continue
        if kind not in {"credits", "premium"} or amount_i <= 0 or stars_i <= 0:
            log.warning("Ignoring invalid product %r", part)
            continue
        code = f"{kind[0]}{amount_i}"
        products.append(Product(code=code, kind=kind, amount=amount_i, stars=stars_i))
    return tuple(products)


@dataclass(frozen=True)
class Settings:
    bot_token: str
    admin_ids: frozenset[int]

    # Storage
    data_dir: Path
    work_dir: Path
    db_path: Path
    redis_url: str

    # Telegram Bot API
    telegram_api_base: str | None
    telegram_api_is_local: bool
    telegram_request_timeout: float

    # Files
    max_input_mb: int
    max_output_mb: int

    # Queue
    queue_workers: int
    user_max_jobs: int

    # Conversion
    djvu_pdf_quality: str
    djvu_keep_resolution: bool
    djvu_workers: int
    image_compress_quality: int
    image_compress_max_side: int
    pdf_compress_quality: str
    archive_max_output_mb: int
    archive_max_files: int
    tool_timeout: int

    # OCR
    ocr_enabled: bool
    ocr_languages: str
    ocr_jobs: int
    ocr_rotate_pages: bool
    ocr_timeout: int

    # Billing
    billing_enabled: bool
    free_weekly_ops: int
    products: tuple[Product, ...] = field(default_factory=tuple)
    support_contact: str = ""

    @property
    def uses_local_api(self) -> bool:
        return bool(self.telegram_api_base) and self.telegram_api_is_local

    @property
    def effective_max_input_mb(self) -> int:
        # The public Bot API only lets bots download files up to 20 MB.
        return self.max_input_mb if self.uses_local_api else min(self.max_input_mb, 20)

    @property
    def effective_max_output_mb(self) -> int:
        # The public Bot API accepts uploads up to 50 MB; keep a safety margin.
        return self.max_output_mb if self.uses_local_api else min(self.max_output_mb, 49)

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.admin_ids


def load_settings() -> Settings:
    load_env_file()

    bot_token = _str("BOT_TOKEN")
    if not bot_token:
        raise RuntimeError("BOT_TOKEN is not set. Copy .env.example to .env and fill it in.")

    data_dir = Path(_str("DATA_DIR", "data"))
    work_dir = Path(_str("WORK_DIR") or data_dir / "work")
    db_path = Path(_str("DB_PATH") or data_dir / "bot.sqlite3")

    djvu_quality = _str("DJVU_PDF_QUALITY", "printer").lstrip("/").lower()
    if djvu_quality not in {"screen", "ebook", "printer", "prepress", "default"}:
        djvu_quality = "printer"
    pdf_quality = _str("PDF_COMPRESS_QUALITY", "ebook").lstrip("/").lower()
    if pdf_quality not in {"screen", "ebook", "printer", "prepress", "default"}:
        pdf_quality = "ebook"

    cpu_count = os.cpu_count() or 2

    return Settings(
        bot_token=bot_token,
        admin_ids=_id_set("ADMIN_IDS"),
        data_dir=data_dir,
        work_dir=work_dir,
        db_path=db_path,
        redis_url=_str("REDIS_URL", "redis://localhost:6379/0"),
        telegram_api_base=_str("TELEGRAM_API_BASE") or None,
        telegram_api_is_local=_bool("TELEGRAM_API_IS_LOCAL", False),
        telegram_request_timeout=_float("TELEGRAM_REQUEST_TIMEOUT", 3600),
        max_input_mb=_int("MAX_INPUT_MB", 200, minimum=1),
        max_output_mb=_int("MAX_OUTPUT_MB", 49, minimum=1),
        queue_workers=_int("QUEUE_WORKERS", 2, minimum=1),
        user_max_jobs=_int("USER_MAX_JOBS", 3, minimum=1),
        djvu_pdf_quality=djvu_quality,
        djvu_keep_resolution=_bool("DJVU_PDF_KEEP_RESOLUTION", True),
        djvu_workers=_int("DJVU_CONVERT_WORKERS", min(4, cpu_count), minimum=1),
        image_compress_quality=min(95, _int("IMAGE_COMPRESS_QUALITY", 75, minimum=1)),
        image_compress_max_side=_int("IMAGE_COMPRESS_MAX_SIDE", 1600, minimum=0),
        pdf_compress_quality=pdf_quality,
        archive_max_output_mb=_int("ARCHIVE_MAX_OUTPUT_MB", 500, minimum=1),
        archive_max_files=_int("ARCHIVE_MAX_FILES", 200, minimum=1),
        tool_timeout=_int("TOOL_TIMEOUT", 1800, minimum=30),
        ocr_enabled=_bool("OCR_ENABLED", True),
        ocr_languages=_str("OCR_LANGUAGES", "rus+eng"),
        ocr_jobs=_int("OCR_JOBS", min(4, cpu_count), minimum=1),
        ocr_rotate_pages=_bool("OCR_ROTATE_PAGES", True),
        ocr_timeout=_int("OCR_TIMEOUT", 3600, minimum=60),
        billing_enabled=_bool("BILLING_ENABLED", True),
        free_weekly_ops=_int("FREE_WEEKLY_OPS", 20, minimum=0),
        products=parse_products(
            _str("PRODUCTS", "credits:50:25,credits:200:90,credits:1000:400,premium:30:150")
        ),
        support_contact=_str("SUPPORT_CONTACT"),
    )
