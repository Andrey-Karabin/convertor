"""Images -> PDF and image compression (Pillow)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from app.converters.process import ConversionError, ensure_file


def _flatten(frame):
    """Apply EXIF rotation and put transparent images on a white background."""
    from PIL import Image, ImageOps

    page = ImageOps.exif_transpose(frame).convert("RGBA")
    background = Image.new("RGB", page.size, "white")
    background.paste(page, mask=page.getchannel("A"))
    return background


def _open(path: Path):
    from PIL import Image, UnidentifiedImageError

    try:
        return Image.open(path)
    except (UnidentifiedImageError, OSError) as exc:
        raise ConversionError("Формат изображения не поддерживается или файл повреждён") from exc


def _to_pdf(source: Path, output: Path) -> None:
    from PIL import ImageSequence

    with _open(source) as image:
        frames = [_flatten(frame) for frame in ImageSequence.Iterator(image)]
    if not frames:
        raise ConversionError("В изображении нет кадров")
    output.parent.mkdir(parents=True, exist_ok=True)
    first, *rest = frames
    first.save(output, "PDF", resolution=150.0, save_all=bool(rest), append_images=rest)


async def to_pdf(source: Path, output: Path) -> Path:
    await asyncio.to_thread(_to_pdf, source, output)
    return ensure_file(output, "PDF")


def _compress(source: Path, output: Path, quality: int, max_side: int) -> None:
    with _open(source) as image:
        page = _flatten(image)
    if max_side > 0 and max(page.size) > max_side:
        ratio = max_side / max(page.size)
        page = page.resize((max(1, int(page.width * ratio)), max(1, int(page.height * ratio))))
    output.parent.mkdir(parents=True, exist_ok=True)
    page.save(output, "JPEG", quality=quality, optimize=True, progressive=True)


async def compress(source: Path, output: Path, *, quality: int = 75, max_side: int = 1600) -> Path:
    await asyncio.to_thread(_compress, source, output, quality, max_side)
    return ensure_file(output, "Сжатое изображение")
