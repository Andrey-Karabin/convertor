"""DjVu -> PDF via djvups + Ghostscript, page ranges converted in parallel."""

from __future__ import annotations

import asyncio
from pathlib import Path

from app.converters import pdf
from app.converters.process import ConversionError, ensure_file, find_tool, run


async def to_pdf(
    source: Path,
    output: Path,
    *,
    quality: str = "printer",
    keep_resolution: bool = True,
    workers: int = 2,
    timeout: int = 1800,
) -> Path:
    djvused = find_tool("djvused")
    djvups = find_tool("djvups")
    ps2pdf = find_tool("ps2pdf")
    output.parent.mkdir(parents=True, exist_ok=True)

    out = await run([djvused, str(source), "-e", "n"], 120)
    try:
        pages = int(out.splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise ConversionError("Не удалось прочитать DjVu-файл") from exc
    if pages < 1:
        raise ConversionError("В DjVu-файле нет страниц")

    ranges = _page_ranges(pages, max(1, min(workers, pages)))
    if len(ranges) == 1:
        await _convert_range(djvups, ps2pdf, source, output, None, quality, keep_resolution, timeout)
        return ensure_file(output, "PDF")

    parts = [output.with_name(f"{output.stem}_part{i:03d}.pdf") for i in range(len(ranges))]
    try:
        await asyncio.gather(*(
            _convert_range(djvups, ps2pdf, source, part, page_range, quality, keep_resolution, timeout)
            for part, page_range in zip(parts, ranges)
        ))
        await pdf.merge(parts, output)
    finally:
        for part in parts:
            part.unlink(missing_ok=True)
    return ensure_file(output, "PDF")


def _page_ranges(pages: int, workers: int) -> list[str]:
    per_worker = -(-pages // workers)
    return [f"{start}-{min(pages, start + per_worker - 1)}" for start in range(1, pages + 1, per_worker)]


async def _convert_range(
    djvups: str,
    ps2pdf: str,
    source: Path,
    output: Path,
    page_range: str | None,
    quality: str,
    keep_resolution: bool,
    timeout: int,
) -> None:
    ps_path = output.with_suffix(".ps")
    args = [djvups, "-format=ps", "-level=2", "-mode=color"]
    if page_range:
        args.append(f"-page={page_range}")
    try:
        await run([*args, str(source), str(ps_path)], timeout, expect_file=ps_path)
        gs_args = [ps2pdf, f"-dPDFSETTINGS=/{quality}", "-dAutoRotatePages=/None"]
        if keep_resolution:
            gs_args += [
                "-dDownsampleColorImages=false",
                "-dDownsampleGrayImages=false",
                "-dDownsampleMonoImages=false",
                "-dAutoFilterColorImages=false",
                "-dColorImageFilter=/FlateEncode",
                "-dGrayImageFilter=/FlateEncode",
                "-dMonoImageFilter=/CCITTFaxEncode",
            ]
        await run([*gs_args, str(ps_path), str(output)], timeout, expect_file=output)
    finally:
        ps_path.unlink(missing_ok=True)
