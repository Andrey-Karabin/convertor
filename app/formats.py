"""Which files the bot understands and how output files are named."""

from __future__ import annotations

import re
from enum import Enum
from pathlib import Path


class FileKind(str, Enum):
    PDF = "pdf"
    DJVU = "djvu"
    OFFICE = "office"
    IMAGE = "image"
    ARCHIVE = "archive"


DJVU_EXTENSIONS = (".djvu", ".djv")
OFFICE_EXTENSIONS = (
    ".doc", ".docx", ".odt", ".rtf", ".txt", ".html", ".htm",
    ".xls", ".xlsx", ".ods", ".csv",
    ".ppt", ".pptx", ".odp",
)
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff")
# Longest first so ".tar.gz" wins over ".gz"-like endings.
ARCHIVE_EXTENSIONS = (".tar.bz2", ".tar.gz", ".tar.xz", ".tgz", ".tar", ".zip", ".7z", ".rar")


def extension(file_name: str | None) -> str:
    """Lower-case extension, aware of double extensions like .tar.gz."""
    if not file_name:
        return ""
    lower = file_name.lower()
    for ext in ARCHIVE_EXTENSIONS:
        if lower.endswith(ext):
            return ext
    return Path(lower).suffix


def detect_kind(file_name: str | None) -> FileKind | None:
    ext = extension(file_name)
    if ext == ".pdf":
        return FileKind.PDF
    if ext in DJVU_EXTENSIONS:
        return FileKind.DJVU
    if ext in OFFICE_EXTENSIONS:
        return FileKind.OFFICE
    if ext in IMAGE_EXTENSIONS:
        return FileKind.IMAGE
    if ext in ARCHIVE_EXTENSIONS:
        return FileKind.ARCHIVE
    return None


def safe_stem(file_name: str) -> str:
    name = file_name
    ext = extension(file_name)
    if ext and name.lower().endswith(ext):
        name = name[: -len(ext)]
    stem = re.sub(r"[^\w.-]+", "_", name).strip("._-")
    return stem[:100] or "file"


def output_name(file_name: str, suffix: str = "", ext: str = ".pdf") -> str:
    return f"{safe_stem(file_name)}{suffix}{ext}"


def safe_send_name(relative_name: str) -> str:
    """File name for a file taken out of an archive (keeps sub-folders visible)."""
    name = re.sub(r"[^\w.,()+=@ -]+", "__", relative_name).strip("._- ")
    if len(name) > 120:
        suffix = Path(name).suffix
        name = f"{name[: max(1, 120 - len(suffix) - 3)]}...{suffix}"
    return name or "file"


def parse_page_ranges(text: str) -> list[int]:
    """`1-3, 5` -> [0, 1, 2, 4] (zero-based). Raises ValueError on bad input."""
    pages: set[int] = set()
    for part in text.replace(" ", "").replace(";", ",").split(","):
        if not part:
            continue
        if "-" in part:
            start_text, end_text = part.split("-", 1)
            if not (start_text.isdigit() and end_text.isdigit()):
                raise ValueError(part)
            start, end = int(start_text), int(end_text)
            if start < 1 or end < start:
                raise ValueError(part)
            pages.update(range(start - 1, end))
        else:
            if not part.isdigit() or int(part) < 1:
                raise ValueError(part)
            pages.add(int(part) - 1)
    if not pages:
        raise ValueError(text)
    return sorted(pages)


def compact_ranges(pages: list[int], one_based_input: bool = False) -> str:
    """[0, 1, 2, 4] -> "1-3,5" (qpdf syntax, one-based)."""
    if not pages:
        raise ValueError("no pages")
    shift = 0 if one_based_input else 1
    ordered = sorted(set(pages))
    parts: list[str] = []
    start = end = ordered[0]
    for page in ordered[1:]:
        if page == end + 1:
            end = page
            continue
        parts.append(f"{start + shift}" if start == end else f"{start + shift}-{end + shift}")
        start = end = page
    parts.append(f"{start + shift}" if start == end else f"{start + shift}-{end + shift}")
    return ",".join(parts)
