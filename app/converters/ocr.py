"""Text recognition (OCR) for every PDF the bot sends.

OCRmyPDF adds an invisible text layer on top of scanned pages, so the PDF
becomes searchable and text can be copied. Pages that already contain text
(e.g. PDFs exported from Word) are skipped with --skip-text, so OCR is cheap
for them. With --rotate-pages it also turns sideways scanned pages upright.
"""

from __future__ import annotations

import logging
from pathlib import Path

from app.converters.process import ConversionError, find_tool, run

log = logging.getLogger(__name__)


async def add_text_layer(
    source: Path,
    output: Path,
    *,
    languages: str = "rus+eng",
    jobs: int = 2,
    rotate_pages: bool = True,
    timeout: int = 3600,
) -> Path:
    """Return a PDF with a text layer. Raises ConversionError on failure."""
    ocrmypdf = find_tool("ocrmypdf")
    args = [
        ocrmypdf,
        "--skip-text",
        "--output-type", "pdf",  # plain PDF; PDF/A conversion would rewrite the file
        "--optimize", "1",  # lossless only
        "--jobs", str(jobs),
        "-l", languages,
        "--quiet",
    ]
    if rotate_pages:
        args += ["--rotate-pages", "--rotate-pages-threshold", "5"]
    output.parent.mkdir(parents=True, exist_ok=True)
    # Exit code 4 = output written but failed a validity check; still usable.
    await run([*args, str(source), str(output)], timeout, expect_file=output, ok_codes=(0, 4))
    return output


async def try_add_text_layer(source: Path, **kwargs) -> tuple[Path, bool]:
    """OCR that never fails the job: on any error the original PDF is returned."""
    output = source.with_name(f"{source.stem}.ocr.pdf")
    try:
        await add_text_layer(source, output, **kwargs)
    except ConversionError as exc:
        log.warning("OCR skipped for %s: %s", source.name, exc)
        output.unlink(missing_ok=True)
        return source, False
    output.replace(source)
    return source, True
