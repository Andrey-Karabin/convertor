"""PDF operations: merge, pick/delete pages, split, compress.

qpdf is used when installed (fast, low memory); pypdf is the fallback.
"""

from __future__ import annotations

import asyncio
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from app.formats import compact_ranges
from app.converters.process import ConversionError, ensure_file, find_tool, has_tool, run

QPDF_TIMEOUT = 600


async def page_count(path: Path) -> int:
    if has_tool("qpdf"):
        out = await run([find_tool("qpdf"), "--show-npages", str(path)], QPDF_TIMEOUT)
        try:
            return int(out.splitlines()[-1])
        except (ValueError, IndexError) as exc:
            raise ConversionError("Не удалось прочитать PDF") from exc
    return await asyncio.to_thread(_pypdf_page_count, path)


def _pypdf_page_count(path: Path) -> int:
    from pypdf import PdfReader

    try:
        return len(PdfReader(str(path)).pages)
    except Exception as exc:  # pypdf raises many different errors
        raise ConversionError("PDF повреждён или защищён паролем") from exc


async def merge(inputs: list[Path], output: Path) -> Path:
    if len(inputs) < 2:
        raise ConversionError("Для склейки нужно минимум два PDF")
    output.parent.mkdir(parents=True, exist_ok=True)
    if has_tool("qpdf"):
        await run(
            [find_tool("qpdf"), "--empty", "--pages", *map(str, inputs), "--", str(output)],
            QPDF_TIMEOUT,
            expect_file=output,
            ok_codes=(0, 3),  # 3 = finished with warnings
        )
    else:
        await asyncio.to_thread(_pypdf_write_pages, [(p, None) for p in inputs], output)
    return ensure_file(output, "PDF")


async def select_pages(source: Path, output: Path, pages: list[int], *, delete: bool) -> Path:
    """Keep only `pages` (zero-based) or, with delete=True, everything except them."""
    total = await page_count(source)
    chosen = set(pages)
    if any(p < 0 or p >= total for p in chosen):
        raise ConversionError(f"В PDF всего {total} стр. Проверьте номера страниц")
    if delete:
        chosen = set(range(total)) - chosen
    if not chosen:
        raise ConversionError("В результате не останется ни одной страницы")

    output.parent.mkdir(parents=True, exist_ok=True)
    if has_tool("qpdf"):
        await run(
            [find_tool("qpdf"), str(source), "--pages", str(source), compact_ranges(list(chosen)),
             "--", str(output)],
            QPDF_TIMEOUT,
            expect_file=output,
            ok_codes=(0, 3),
        )
    else:
        await asyncio.to_thread(_pypdf_write_pages, [(source, sorted(chosen))], output)
    return ensure_file(output, "PDF")


def _pypdf_write_pages(sources: list[tuple[Path, list[int] | None]], output: Path) -> None:
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    for path, indexes in sources:
        reader = PdfReader(str(path))
        for index in indexes if indexes is not None else range(len(reader.pages)):
            writer.add_page(reader.pages[index])
    with output.open("wb") as fh:
        writer.write(fh)


def _page_bytes(page) -> bytes:
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_page(page)
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


async def split_by_size(source: Path, out_dir: Path, base_name: str, max_bytes: int) -> list[Path]:
    """Cut a big PDF into parts that each fit into `max_bytes`."""
    return await asyncio.to_thread(_split_by_size, source, out_dir, base_name, max_bytes)


def _split_by_size(source: Path, out_dir: Path, base_name: str, max_bytes: int) -> list[Path]:
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(str(source))
    if not reader.pages:
        raise ConversionError("В PDF нет страниц")
    out_dir.mkdir(parents=True, exist_ok=True)

    target = max(1, int(max_bytes * 0.9))
    groups: list[list[int]] = [[]]
    size = 0
    for index, page in enumerate(reader.pages):
        page_size = len(_page_bytes(page))
        if page_size > max_bytes:
            raise ConversionError(f"Страница {index + 1} сама по себе больше лимита Telegram")
        if groups[-1] and size + page_size > target:
            groups.append([])
            size = 0
        groups[-1].append(index)
        size += page_size

    parts: list[Path] = []
    queue = list(groups)
    while queue:
        group = queue.pop(0)
        part = out_dir / f"{base_name}_part{len(parts) + 1}_p{group[0] + 1}-{group[-1] + 1}.pdf"
        writer = PdfWriter()
        for index in group:
            writer.add_page(reader.pages[index])
        with part.open("wb") as fh:
            writer.write(fh)
        if part.stat().st_size > max_bytes:
            part.unlink()
            if len(group) == 1:
                raise ConversionError(f"Страница {group[0] + 1} слишком большая для отправки")
            middle = len(group) // 2
            queue[:0] = [group[:middle], group[middle:]]
            continue
        parts.append(part)
    return parts


async def split_pages_to_zips(source: Path, out_dir: Path, base_name: str, max_bytes: int) -> list[Path]:
    """Every page becomes its own PDF inside a ZIP (several ZIPs if too big)."""
    return await asyncio.to_thread(_split_pages_to_zips, source, out_dir, base_name, max_bytes)


def _split_pages_to_zips(source: Path, out_dir: Path, base_name: str, max_bytes: int) -> list[Path]:
    from pypdf import PdfReader

    reader = PdfReader(str(source))
    if not reader.pages:
        raise ConversionError("В PDF нет страниц")
    out_dir.mkdir(parents=True, exist_ok=True)

    zips: list[Path] = []
    archive: ZipFile | None = None
    size = 0
    try:
        for number, page in enumerate(reader.pages, start=1):
            data = _page_bytes(page)
            if archive is None or (size and size + len(data) > max_bytes):
                if archive is not None:
                    archive.close()
                zips.append(out_dir / f"{base_name}_pages_{len(zips) + 1}.zip")
                archive = ZipFile(zips[-1], "w", compression=ZIP_DEFLATED)
                size = 0
            archive.writestr(f"{base_name}_page_{number}.pdf", data)
            size += len(data)
    finally:
        if archive is not None:
            archive.close()
    return zips


async def compress(source: Path, output: Path, quality: str, timeout: int) -> Path:
    gs = find_tool("gs")
    output.parent.mkdir(parents=True, exist_ok=True)
    await run(
        [gs, "-sDEVICE=pdfwrite", "-dCompatibilityLevel=1.5", f"-dPDFSETTINGS=/{quality}",
         "-dNOPAUSE", "-dQUIET", "-dBATCH", f"-sOutputFile={output}", str(source)],
        timeout,
        expect_file=output,
    )
    return output
