import asyncio
import concurrent.futures
import gc
import os
import subprocess
import shutil
import tarfile
import tempfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import PurePosixPath
from pathlib import Path
from zipfile import BadZipFile
from zipfile import ZIP_DEFLATED, ZipFile


class ConversionError(RuntimeError):
    """Raised when a file cannot be converted."""


class ConversionToolMissingError(ConversionError):
    """Raised when an external conversion tool is not available."""


@dataclass(frozen=True)
class ArchiveEntry:
    name: str
    size: int


def _run_sync_process(args: list[str], timeout_seconds: int | None = None) -> str:
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise ConversionError("Conversion timed out.") from exc

    if result.returncode != 0:
        details = result.stderr.strip() or result.stdout.strip()
        if not details:
            details = f"{args[0]} exited with code {result.returncode}."
        raise ConversionError(details)
    return result.stdout.strip()


def _find_tool(name: str, install_hint: str) -> str:
    tool_path = shutil.which(name)
    if tool_path is None:
        raise ConversionToolMissingError(
            f"{name} was not found. {install_hint}"
        )
    return tool_path


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return max(1, value)


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


async def _run_process(
    args: list[str],
    timeout_seconds: int,
    output_path: Path | None,
    env: dict[str, str] | None = None,
) -> str:
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )

    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.communicate()
        raise ConversionError("Conversion timed out.") from exc

    stdout_text = stdout.decode(errors="replace").strip()
    stderr_text = stderr.decode(errors="replace").strip()
    details = stderr_text or stdout_text

    if process.returncode != 0:
        if not details:
            details = f"Conversion tool exited with code {process.returncode}."
        raise ConversionError(details)

    if output_path is not None and (
        not output_path.exists() or output_path.stat().st_size == 0
    ):
        details_text = f" Tool output: {details}" if details else ""
        raise ConversionError(
            f"Conversion finished, but the PDF file was not created.{details_text}"
        )

    return details


async def convert_djvu_to_pdf(
    input_path: Path,
    output_path: Path,
    timeout_seconds: int = 1800,
) -> Path:
    await asyncio.to_thread(
        _convert_djvu_to_pdf_sync,
        input_path,
        output_path,
        timeout_seconds,
    )
    return output_path


def _convert_djvu_to_pdf_sync(
    input_path: Path,
    output_path: Path,
    timeout_seconds: int,
) -> None:
    djvups_path = _find_tool(
        "djvups",
        "Install DjVuLibre or run the bot in Docker.",
    )
    ps2pdf_path = _find_tool(
        "ps2pdf",
        "Install Ghostscript or run the bot in Docker.",
    )
    djvused_path = _find_tool(
        "djvused",
        "Install DjVuLibre or run the bot in Docker.",
    )

    quality = os.getenv("DJVU_PDF_QUALITY", "printer").strip().lstrip("/")
    if quality not in {"screen", "ebook", "printer", "prepress", "default"}:
        quality = "printer"

    keep_resolution = _env_bool("DJVU_PDF_KEEP_RESOLUTION", True)
    auto_orient_pages = _env_bool("DJVU_AUTO_ORIENT_PAGES", True)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    page_count = _get_djvu_page_count(djvused_path, input_path)
    workers = min(_env_int("DJVU_CONVERT_WORKERS", 1), page_count)

    if workers <= 1 or page_count <= 1:
        _convert_djvu_page_range_to_pdf(
            djvups_path,
            ps2pdf_path,
            input_path,
            output_path,
            quality,
            keep_resolution,
            None,
            timeout_seconds,
        )
        if auto_orient_pages:
            _auto_orient_pdf_pages(output_path, timeout_seconds)
        return

    part_paths = [
        output_path.parent / f"{output_path.stem}_djvu_part_{index + 1:03d}.pdf"
        for index in range(workers)
    ]
    page_ranges = _split_page_ranges(page_count, workers)

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [
                executor.submit(
                    _convert_djvu_page_range_to_pdf,
                    djvups_path,
                    ps2pdf_path,
                    input_path,
                    part_paths[index],
                    quality,
                    keep_resolution,
                    page_range,
                    timeout_seconds,
                )
                for index, page_range in enumerate(page_ranges)
            ]
            for future in concurrent.futures.as_completed(futures):
                future.result()

        _merge_pdfs_sync(part_paths, output_path, prefer_qpdf=True)
        if auto_orient_pages:
            _auto_orient_pdf_pages(output_path, timeout_seconds)
    finally:
        for part_path in part_paths:
            part_path.unlink(missing_ok=True)
        gc.collect()

    if not output_path.exists() or output_path.stat().st_size == 0:
        raise ConversionError("DJVU conversion finished, but the PDF file was not created.")


def _get_djvu_page_count(djvused_path: str, input_path: Path) -> int:
    page_count_text = _run_sync_process([djvused_path, str(input_path), "-e", "n"])
    try:
        page_count = int(page_count_text.strip().splitlines()[-1])
    except (IndexError, ValueError) as exc:
        raise ConversionError(f"Could not read DJVU page count: {page_count_text}") from exc

    if page_count < 1:
        raise ConversionError("DJVU does not contain any pages.")
    return page_count


def _split_page_ranges(page_count: int, workers: int) -> list[str]:
    pages_per_worker = (page_count + workers - 1) // workers
    ranges = []
    start = 1
    while start <= page_count:
        end = min(page_count, start + pages_per_worker - 1)
        ranges.append(f"{start}-{end}")
        start = end + 1
    return ranges


def _ps2pdf_args(
    ps2pdf_path: str,
    quality: str,
    keep_resolution: bool,
    ps_path: Path,
    output_path: Path,
) -> list[str]:
    args = [
        ps2pdf_path,
        f"-dPDFSETTINGS=/{quality}",
        "-dAutoRotatePages=/None",
    ]
    if keep_resolution:
        args.extend(
            [
                "-dDownsampleColorImages=false",
                "-dDownsampleGrayImages=false",
                "-dDownsampleMonoImages=false",
                "-dAutoFilterColorImages=false",
                "-dColorImageFilter=/FlateEncode",
                "-dGrayImageFilter=/FlateEncode",
                "-dMonoImageFilter=/CCITTFaxEncode",
            ]
        )
    args.extend([str(ps_path), str(output_path)])
    return args


def _convert_djvu_page_range_to_pdf(
    djvups_path: str,
    ps2pdf_path: str,
    input_path: Path,
    output_path: Path,
    quality: str,
    keep_resolution: bool,
    page_range: str | None,
    timeout_seconds: int,
) -> None:
    ps_path = output_path.with_suffix(".ps")
    djvups_args = [
        djvups_path,
        "-format=ps",
        "-level=2",
        "-mode=color",
    ]
    if page_range is not None:
        djvups_args.append(f"-page={page_range}")
    djvups_args.extend([str(input_path), str(ps_path)])

    try:
        _run_sync_process(
            djvups_args,
            timeout_seconds=timeout_seconds,
        )
        _run_sync_process(
            _ps2pdf_args(
                ps2pdf_path,
                quality,
                keep_resolution,
                ps_path,
                output_path,
            ),
            timeout_seconds=timeout_seconds,
        )
    finally:
        ps_path.unlink(missing_ok=True)

    if not output_path.exists() or output_path.stat().st_size == 0:
        raise ConversionError("DJVU conversion finished, but a PDF part was not created.")


def _auto_orient_pdf_pages(pdf_path: Path, timeout_seconds: int) -> None:
    pdftoppm_path = _find_tool(
        "pdftoppm",
        "Install Poppler or run the bot in Docker.",
    )
    tesseract_path = _find_tool(
        "tesseract",
        "Install Tesseract OCR or run the bot in Docker.",
    )

    page_count = _get_pdf_page_count(pdf_path)
    if page_count == 0:
        return

    candidate_pages = _orientation_candidate_pages(pdf_path, page_count)
    if not candidate_pages:
        return

    workers = min(_env_int("DJVU_AUTO_ORIENT_WORKERS", 2), len(candidate_pages))
    dpi = _env_int("DJVU_AUTO_ORIENT_DPI", 110)
    min_confidence = float(os.getenv("DJVU_AUTO_ORIENT_MIN_CONFIDENCE", "1.0"))
    work_dir = pdf_path.parent / f"{pdf_path.stem}_orientation"
    work_dir.mkdir(parents=True, exist_ok=True)

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [
                executor.submit(
                    _detect_pdf_page_rotation,
                    pdftoppm_path,
                    tesseract_path,
                    pdf_path,
                    work_dir,
                    page_index,
                    dpi,
                    min_confidence,
                    timeout_seconds,
                )
                for page_index in candidate_pages
            ]
            detected_rotations = [future.result() for future in futures]

        rotations = [0] * page_count
        for page_index, rotation in zip(candidate_pages, detected_rotations):
            rotations[page_index] = rotation
        if not any(rotations):
            return

        _rotate_pdf_pages(pdf_path, rotations)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
        gc.collect()


def _orientation_candidate_pages(pdf_path: Path, page_count: int) -> list[int]:
    mode = os.getenv("DJVU_AUTO_ORIENT_MODE", "landscape").strip().lower()
    if mode in {"off", "false", "none", "0"}:
        return []
    if mode == "all":
        return list(range(page_count))
    if mode not in {"landscape", "wide"}:
        mode = "landscape"

    from pypdf import PdfReader

    reader = PdfReader(str(pdf_path))
    candidate_pages = []
    for page_index, page in enumerate(reader.pages):
        width = float(page.mediabox.width)
        height = float(page.mediabox.height)
        if width > height:
            candidate_pages.append(page_index)
    return candidate_pages


def _detect_pdf_page_rotation(
    pdftoppm_path: str,
    tesseract_path: str,
    pdf_path: Path,
    work_dir: Path,
    page_index: int,
    dpi: int,
    min_confidence: float,
    timeout_seconds: int,
) -> int:
    page_number = page_index + 1
    image_prefix = work_dir / f"page_{page_number:05d}"
    image_path = image_prefix.with_suffix(".png")

    try:
        _run_sync_process(
            [
                pdftoppm_path,
                "-f",
                str(page_number),
                "-l",
                str(page_number),
                "-singlefile",
                "-png",
                "-r",
                str(dpi),
                str(pdf_path),
                str(image_prefix),
            ],
            timeout_seconds=timeout_seconds,
        )

        result = subprocess.run(
            [
                tesseract_path,
                str(image_path),
                "stdout",
                "--psm",
                "0",
                "-l",
                "osd",
            ],
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout_seconds,
        )
    except (ConversionError, subprocess.TimeoutExpired):
        return 0
    finally:
        image_path.unlink(missing_ok=True)

    osd_output = "\n".join(
        part for part in (result.stdout, result.stderr) if part
    )
    rotation = _parse_tesseract_rotation(osd_output, min_confidence)
    return rotation


def _parse_tesseract_rotation(osd_output: str, min_confidence: float) -> int:
    rotation: int | None = None
    confidence = 0.0

    for raw_line in osd_output.splitlines():
        line = raw_line.strip()
        if line.startswith("Rotate:"):
            try:
                rotation = int(line.split(":", 1)[1].strip())
            except ValueError:
                rotation = None
        elif line.startswith("Orientation confidence:"):
            try:
                confidence = float(line.split(":", 1)[1].strip())
            except ValueError:
                confidence = 0.0

    if rotation not in {90, 180, 270}:
        return 0
    if confidence < min_confidence:
        return 0
    return rotation


def _get_pdf_page_count(input_path: Path) -> int:
    qpdf_path = shutil.which("qpdf")
    if qpdf_path is not None:
        page_count_text = _run_sync_process([qpdf_path, "--show-npages", str(input_path)])
        try:
            return int(page_count_text)
        except ValueError as exc:
            raise ConversionError(f"Could not read PDF page count: {page_count_text}") from exc

    from pypdf import PdfReader

    reader = PdfReader(str(input_path))
    return len(reader.pages)


def _one_based_page_ranges(page_numbers: list[int]) -> str:
    if not page_numbers:
        raise ConversionError("No pages selected.")

    sorted_pages = sorted(set(page_numbers))
    ranges = []
    start = sorted_pages[0]
    end = sorted_pages[0]

    for page in sorted_pages[1:]:
        if page == end + 1:
            end = page
            continue

        ranges.append((start, end))
        start = page
        end = page

    ranges.append((start, end))

    parts = []
    for start, end in ranges:
        if start == end:
            parts.append(str(start))
        else:
            parts.append(f"{start}-{end}")
    return ",".join(parts)


def _rotate_pdf_pages(pdf_path: Path, rotations: list[int]) -> None:
    qpdf_path = shutil.which("qpdf")
    if qpdf_path is not None:
        _rotate_pdf_pages_with_qpdf(qpdf_path, pdf_path, rotations)
        return

    _rotate_pdf_pages_with_pypdf(pdf_path, rotations)


def _rotate_pdf_pages_with_qpdf(
    qpdf_path: str,
    pdf_path: Path,
    rotations: list[int],
) -> None:
    rotate_args = []
    for angle in (90, 180, 270):
        pages = [
            page_index + 1
            for page_index, rotation in enumerate(rotations)
            if rotation == angle
        ]
        if pages:
            rotate_args.append(f"--rotate=+{angle}:{_one_based_page_ranges(pages)}")

    if not rotate_args:
        return

    rotated_path = pdf_path.with_suffix(".oriented.pdf")
    _run_sync_process(
        [qpdf_path, str(pdf_path), *rotate_args, str(rotated_path)]
    )
    rotated_path.replace(pdf_path)


def _rotate_pdf_pages_with_pypdf(pdf_path: Path, rotations: list[int]) -> None:
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(str(pdf_path))
    writer = PdfWriter()
    for page_index, page in enumerate(reader.pages):
        rotation = rotations[page_index]
        if rotation:
            page.rotate(rotation)
        writer.add_page(page)

    rotated_path = pdf_path.with_suffix(".oriented.pdf")
    with rotated_path.open("wb") as output_file:
        writer.write(output_file)
    rotated_path.replace(pdf_path)


async def convert_djvu_to_pdf_with_ddjvu(
    input_path: Path,
    output_path: Path,
    timeout_seconds: int = 300,
) -> Path:
    ddjvu_path = _find_tool(
        "ddjvu",
        "Install DjVuLibre or run the bot in Docker.",
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)

    await _run_process(
        [
            ddjvu_path,
            "-format=pdf",
            str(input_path),
            str(output_path),
        ],
        timeout_seconds,
        output_path,
    )
    return output_path


async def convert_office_to_pdf(
    input_path: Path,
    output_path: Path,
    timeout_seconds: int = 300,
) -> Path:
    soffice_path = _find_tool(
        "soffice",
        "Install LibreOffice or run the bot in Docker.",
    )
    _refresh_custom_font_cache()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    generated_path = output_path.parent / f"{input_path.stem}.pdf"
    existing_pdfs = {path.resolve() for path in output_path.parent.glob("*.pdf")}
    profile_dir = output_path.parent / "libreoffice-profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(output_path.parent),
            "LANG": os.getenv("LANG", "C.UTF-8"),
            "LC_ALL": os.getenv("LC_ALL", "C.UTF-8"),
            "SAL_USE_VCLPLUGIN": "svp",
        }
    )
    env.pop("DISPLAY", None)

    try:
        process_output = await _run_process(
            [
                soffice_path,
                "--headless",
                "--nologo",
                "--nodefault",
                "--nofirststartwizard",
                "--nolockcheck",
                "--norestore",
                f"-env:UserInstallation={profile_dir.resolve().as_uri()}",
                "--convert-to",
                _office_pdf_export_filter(input_path),
                "--outdir",
                str(output_path.parent),
                str(input_path),
            ],
            timeout_seconds,
            None,
            env=env,
        )
    finally:
        shutil.rmtree(profile_dir, ignore_errors=True)

    actual_pdf_path = _find_office_generated_pdf(
        output_path.parent,
        generated_path,
        existing_pdfs,
    )
    if actual_pdf_path is None:
        details = f" LibreOffice output: {process_output}" if process_output else ""
        raise ConversionError(
            f"LibreOffice finished, but the PDF file was not created.{details}"
        )

    if actual_pdf_path != output_path:
        actual_pdf_path.replace(output_path)
    return output_path


def _find_office_generated_pdf(
    output_dir: Path,
    expected_path: Path,
    existing_pdfs: set[Path],
) -> Path | None:
    if expected_path.exists() and expected_path.stat().st_size > 0:
        return expected_path

    new_pdfs = [
        path
        for path in output_dir.glob("*.pdf")
        if path.resolve() not in existing_pdfs and path.stat().st_size > 0
    ]
    if not new_pdfs:
        return None

    return max(new_pdfs, key=lambda path: path.stat().st_mtime)


def _office_pdf_export_filter(input_path: Path) -> str:
    extension = input_path.suffix.lower()
    if extension in {".doc", ".docx", ".odt", ".rtf", ".txt", ".html", ".htm"}:
        return "pdf:writer_pdf_Export"
    if extension in {".xls", ".xlsx", ".ods", ".csv"}:
        return "pdf:calc_pdf_Export"
    if extension in {".ppt", ".pptx", ".odp"}:
        return "pdf:impress_pdf_Export"
    return "pdf"


def _refresh_custom_font_cache() -> None:
    custom_font_dir = Path("/usr/local/share/fonts/custom")
    if not custom_font_dir.exists():
        return
    if not any(custom_font_dir.glob("*.*")):
        return

    fc_cache_path = shutil.which("fc-cache")
    if fc_cache_path is None:
        return

    try:
        _run_sync_process([fc_cache_path, "-f", str(custom_font_dir)], timeout_seconds=60)
    except ConversionError:
        pass


def _convert_image_to_pdf_sync(input_path: Path, output_path: Path) -> None:
    try:
        from PIL import Image, ImageOps, ImageSequence, UnidentifiedImageError
    except ImportError as exc:
        raise ConversionToolMissingError(
            "Pillow is not installed. Install requirements or run the bot in Docker."
        ) from exc

    try:
        with Image.open(input_path) as image:
            frames = []
            for frame in ImageSequence.Iterator(image):
                page = ImageOps.exif_transpose(frame).convert("RGBA")
                background = Image.new("RGB", page.size, "white")
                background.paste(page, mask=page.getchannel("A"))
                frames.append(background)

            if not frames:
                raise ConversionError("Image does not contain any frames.")

            first_page, *extra_pages = frames
            output_path.parent.mkdir(parents=True, exist_ok=True)
            first_page.save(
                output_path,
                "PDF",
                resolution=100.0,
                save_all=bool(extra_pages),
                append_images=extra_pages,
            )
    except UnidentifiedImageError as exc:
        raise ConversionError("The image format is not supported or the file is damaged.") from exc
    except OSError as exc:
        raise ConversionError(str(exc)) from exc

    if not output_path.exists() or output_path.stat().st_size == 0:
        raise ConversionError("Image conversion finished, but the PDF file was not created.")


async def convert_image_to_pdf(input_path: Path, output_path: Path) -> Path:
    await asyncio.to_thread(_convert_image_to_pdf_sync, input_path, output_path)
    return output_path


def _resize_image_to_max_side(image, max_side: int):
    if max_side <= 0:
        return image

    width, height = image.size
    longest_side = max(width, height)
    if longest_side <= max_side:
        return image

    ratio = max_side / longest_side
    new_size = (max(1, int(width * ratio)), max(1, int(height * ratio)))
    return image.resize(new_size)


def _compress_image_sync(
    input_path: Path,
    output_path: Path,
    quality: int,
    max_side: int,
) -> None:
    try:
        from PIL import Image, ImageOps, UnidentifiedImageError
    except ImportError as exc:
        raise ConversionToolMissingError(
            "Pillow is not installed. Install requirements or run the bot in Docker."
        ) from exc

    quality = min(95, max(1, quality))

    try:
        with Image.open(input_path) as image:
            page = ImageOps.exif_transpose(image).convert("RGBA")
            page = _resize_image_to_max_side(page, max_side)
            background = Image.new("RGB", page.size, "white")
            background.paste(page, mask=page.getchannel("A"))

            output_path.parent.mkdir(parents=True, exist_ok=True)
            background.save(
                output_path,
                "JPEG",
                quality=quality,
                optimize=True,
                progressive=True,
            )
    except UnidentifiedImageError as exc:
        raise ConversionError("The image format is not supported or the file is damaged.") from exc
    except OSError as exc:
        raise ConversionError(str(exc)) from exc

    if not output_path.exists() or output_path.stat().st_size == 0:
        raise ConversionError("Image compression finished, but the output file was not created.")


async def compress_image(
    input_path: Path,
    output_path: Path,
    quality: int = 75,
    max_side: int = 1600,
) -> Path:
    await asyncio.to_thread(
        _compress_image_sync,
        input_path,
        output_path,
        quality,
        max_side,
    )
    return output_path


async def compress_pdf(
    input_path: Path,
    output_path: Path,
    quality: str = "ebook",
    timeout_seconds: int = 600,
) -> Path:
    ghostscript_path = shutil.which("gs") or shutil.which("gswin64c") or shutil.which("gswin32c")
    if ghostscript_path is None:
        raise ConversionToolMissingError(
            "Ghostscript was not found. Install Ghostscript or run the bot in Docker."
        )

    quality = quality.strip().lstrip("/").lower()
    if quality not in {"screen", "ebook", "printer", "prepress", "default"}:
        quality = "ebook"

    output_path.parent.mkdir(parents=True, exist_ok=True)
    await _run_process(
        [
            ghostscript_path,
            "-sDEVICE=pdfwrite",
            "-dCompatibilityLevel=1.4",
            f"-dPDFSETTINGS=/{quality}",
            "-dNOPAUSE",
            "-dQUIET",
            "-dBATCH",
            f"-sOutputFile={output_path}",
            str(input_path),
        ],
        timeout_seconds,
        output_path,
    )
    return output_path


def _safe_archive_name(raw_name: str) -> str | None:
    normalized_name = raw_name.replace("\\", "/").strip("/")
    if not normalized_name:
        return None

    path = PurePosixPath(normalized_name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return None
    return path.as_posix()


def _archive_is_tar(input_path: Path) -> bool:
    name = input_path.name.lower()
    return name.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz"))


def _archive_is_zip(input_path: Path) -> bool:
    return input_path.suffix.lower() == ".zip"


def _archive_is_7z(input_path: Path) -> bool:
    return input_path.suffix.lower() == ".7z"


def _archive_is_rar(input_path: Path) -> bool:
    return input_path.suffix.lower() == ".rar"


def _add_archive_file_to_limits(
    safe_name: str | None,
    file_size: int,
    file_count: int,
    total_bytes: int,
    max_total_bytes: int,
    max_files: int,
) -> tuple[int, int]:
    if safe_name is None:
        return file_count, total_bytes

    file_count += 1
    if file_count > max_files:
        raise ConversionError(
            f"Archive contains too many files. Limit: {max_files}."
        )

    total_bytes += int(file_size)
    if total_bytes > max_total_bytes:
        raise ConversionError(
            f"Unpacked files are too large. Limit: {max_total_bytes // (1024 * 1024)} MB."
        )

    return file_count, total_bytes


def _ensure_archive_file_count(file_count: int, max_files: int) -> None:
    if file_count > max_files:
        raise ConversionError(
            f"Archive contains too many files. Limit: {max_files}."
        )


def _archive_output_path(output_dir: Path, safe_name: str) -> Path:
    output_root = output_dir.resolve()
    output_path = (output_dir / safe_name).resolve()
    if output_root not in output_path.parents:
        raise ConversionError("Archive contains unsafe file paths.")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    return output_path


def _copy_archive_stream_to_file(source_file, output_dir: Path, safe_name: str) -> Path:
    output_path = _archive_output_path(output_dir, safe_name)
    with output_path.open("wb") as output_file:
        shutil.copyfileobj(source_file, output_file)
    return output_path


def _list_zip_archive_entries(input_path: Path, max_files: int) -> list[ArchiveEntry]:
    entries: list[ArchiveEntry] = []
    try:
        with ZipFile(input_path) as source_archive:
            for item in source_archive.infolist():
                if item.is_dir():
                    continue
                safe_name = _safe_archive_name(item.filename)
                if safe_name is None:
                    continue
                entries.append(ArchiveEntry(safe_name, int(item.file_size)))
                _ensure_archive_file_count(len(entries), max_files)
    except BadZipFile as exc:
        raise ConversionError("ZIP archive is damaged or not supported.") from exc
    return entries


def _list_tar_archive_entries(input_path: Path, max_files: int) -> list[ArchiveEntry]:
    entries: list[ArchiveEntry] = []
    try:
        with tarfile.open(input_path, mode="r:*") as source_archive:
            for item in source_archive:
                if not item.isfile():
                    continue
                safe_name = _safe_archive_name(item.name)
                if safe_name is None:
                    continue
                entries.append(ArchiveEntry(safe_name, int(item.size)))
                _ensure_archive_file_count(len(entries), max_files)
    except tarfile.TarError as exc:
        raise ConversionError("TAR archive is damaged or not supported.") from exc
    return entries


def _list_7z_archive_entries(input_path: Path, max_files: int) -> list[ArchiveEntry]:
    try:
        from py7zr import SevenZipFile
        from py7zr.exceptions import Bad7zFile, PasswordRequired
    except ImportError as exc:
        raise ConversionToolMissingError(
            "py7zr is not installed. Install requirements or run the bot in Docker."
        ) from exc

    entries: list[ArchiveEntry] = []
    try:
        with SevenZipFile(input_path, mode="r") as source_archive:
            for item in source_archive.list():
                if getattr(item, "is_directory", False):
                    continue
                safe_name = _safe_archive_name(item.filename)
                if safe_name is None:
                    continue
                file_size = int(getattr(item, "uncompressed", 0) or 0)
                entries.append(ArchiveEntry(safe_name, file_size))
                _ensure_archive_file_count(len(entries), max_files)
    except Bad7zFile as exc:
        raise ConversionError("7Z archive is damaged or not supported.") from exc
    except PasswordRequired as exc:
        raise ConversionError("Encrypted 7Z archives are not supported.") from exc
    return entries


def _list_rar_archive_entries(input_path: Path, max_files: int) -> list[ArchiveEntry]:
    try:
        import rarfile
    except ImportError as exc:
        raise ConversionToolMissingError(
            "rarfile is not installed. Install requirements or run the bot in Docker."
        ) from exc

    entries: list[ArchiveEntry] = []
    try:
        with rarfile.RarFile(input_path) as source_archive:
            for item in source_archive.infolist():
                if item.isdir():
                    continue
                safe_name = _safe_archive_name(item.filename)
                if safe_name is None:
                    continue
                entries.append(ArchiveEntry(safe_name, int(item.file_size)))
                _ensure_archive_file_count(len(entries), max_files)
    except rarfile.PasswordRequired as exc:
        raise ConversionError("Encrypted RAR archives are not supported.") from exc
    except rarfile.RarCannotExec as exc:
        raise ConversionToolMissingError(
            "RAR extraction tool was not found. Install unar or run the bot in Docker."
        ) from exc
    except rarfile.Error as exc:
        raise ConversionError("RAR archive is damaged or not supported.") from exc
    return entries


def _list_archive_files_sync(input_path: Path, max_files: int) -> list[ArchiveEntry]:
    if max_files <= 0:
        raise ConversionError("Archive file count limit must be positive.")
    if _archive_is_zip(input_path):
        entries = _list_zip_archive_entries(input_path, max_files)
    elif _archive_is_tar(input_path):
        entries = _list_tar_archive_entries(input_path, max_files)
    elif _archive_is_7z(input_path):
        entries = _list_7z_archive_entries(input_path, max_files)
    elif _archive_is_rar(input_path):
        entries = _list_rar_archive_entries(input_path, max_files)
    else:
        raise ConversionError("Archive format is not supported.")
    if not entries:
        raise ConversionError("Archive does not contain files that can be unpacked.")
    return entries


def _write_zip_archive_contents(
    input_path: Path,
    output_zip_path: Path,
    max_total_bytes: int,
    max_files: int,
) -> tuple[int, int]:
    total_bytes = 0
    file_count = 0
    output_zip_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        with ZipFile(input_path) as source_archive:
            with ZipFile(output_zip_path, "w", compression=ZIP_DEFLATED) as result_archive:
                for item in source_archive.infolist():
                    if item.is_dir():
                        continue

                    safe_name = _safe_archive_name(item.filename)
                    if safe_name is None:
                        continue

                    file_count, total_bytes = _add_archive_file_to_limits(
                        safe_name,
                        item.file_size,
                        file_count,
                        total_bytes,
                        max_total_bytes,
                        max_files,
                    )

                    with source_archive.open(item) as source_file:
                        with result_archive.open(safe_name, "w") as output_file:
                            shutil.copyfileobj(source_file, output_file)
    except BadZipFile as exc:
        raise ConversionError("ZIP archive is damaged or not supported.") from exc

    return file_count, total_bytes


def _write_tar_archive_contents(
    input_path: Path,
    output_zip_path: Path,
    max_total_bytes: int,
    max_files: int,
) -> tuple[int, int]:
    total_bytes = 0
    file_count = 0
    output_zip_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        with tarfile.open(input_path, mode="r:*") as source_archive:
            with ZipFile(output_zip_path, "w", compression=ZIP_DEFLATED) as result_archive:
                for item in source_archive:
                    if not item.isfile():
                        continue

                    safe_name = _safe_archive_name(item.name)
                    if safe_name is None:
                        continue

                    file_count, total_bytes = _add_archive_file_to_limits(
                        safe_name,
                        item.size,
                        file_count,
                        total_bytes,
                        max_total_bytes,
                        max_files,
                    )

                    source_file = source_archive.extractfile(item)
                    if source_file is None:
                        continue
                    with source_file:
                        with result_archive.open(safe_name, "w") as output_file:
                            shutil.copyfileobj(source_file, output_file)
    except tarfile.TarError as exc:
        raise ConversionError("TAR archive is damaged or not supported.") from exc

    return file_count, total_bytes


def _write_7z_archive_contents(
    input_path: Path,
    output_zip_path: Path,
    max_total_bytes: int,
    max_files: int,
) -> tuple[int, int]:
    try:
        from py7zr import SevenZipFile
        from py7zr.exceptions import Bad7zFile, PasswordRequired
    except ImportError as exc:
        raise ConversionToolMissingError(
            "py7zr is not installed. Install requirements or run the bot in Docker."
        ) from exc

    file_count = 0
    total_bytes = 0
    targets: list[tuple[str, str]] = []
    output_zip_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        with SevenZipFile(input_path, mode="r") as source_archive:
            for item in source_archive.list():
                if getattr(item, "is_directory", False):
                    continue

                safe_name = _safe_archive_name(item.filename)
                if safe_name is None:
                    continue

                raw_file_size = getattr(item, "uncompressed", None)
                if raw_file_size is None:
                    raise ConversionError("7Z archive contains files with unknown unpacked size.")
                file_size = int(raw_file_size)
                file_count, total_bytes = _add_archive_file_to_limits(
                    safe_name,
                    file_size,
                    file_count,
                    total_bytes,
                    max_total_bytes,
                    max_files,
                )
                targets.append((item.filename, safe_name))

            if not targets:
                return 0, 0

            with tempfile.TemporaryDirectory(prefix="extract-7z-") as temp_dir_name:
                temp_dir = Path(temp_dir_name)
                source_archive.extract(path=temp_dir, targets=[name for name, _ in targets])
                actual_total_bytes = 0
                actual_file_count = 0
                with ZipFile(output_zip_path, "w", compression=ZIP_DEFLATED) as result_archive:
                    for original_name, safe_name in targets:
                        extracted_path = (temp_dir / original_name).resolve()
                        if not extracted_path.is_file():
                            raise ConversionError(f"7Z archive did not extract {original_name}.")
                        if temp_dir.resolve() not in extracted_path.parents:
                            raise ConversionError("7Z archive contains unsafe file paths.")
                        actual_file_count += 1
                        actual_total_bytes += extracted_path.stat().st_size
                        if actual_file_count > max_files:
                            raise ConversionError(
                                f"Archive contains too many files. Limit: {max_files}."
                            )
                        if actual_total_bytes > max_total_bytes:
                            raise ConversionError(
                                f"Unpacked files are too large. Limit: {max_total_bytes // (1024 * 1024)} MB."
                            )
                        with extracted_path.open("rb") as source_file:
                            with result_archive.open(safe_name, "w") as output_file:
                                shutil.copyfileobj(source_file, output_file)
                file_count = actual_file_count
                total_bytes = actual_total_bytes
    except Bad7zFile as exc:
        raise ConversionError("7Z archive is damaged or not supported.") from exc
    except PasswordRequired as exc:
        raise ConversionError("Encrypted 7Z archives are not supported.") from exc

    return file_count, total_bytes


def _write_rar_archive_contents(
    input_path: Path,
    output_zip_path: Path,
    max_total_bytes: int,
    max_files: int,
) -> tuple[int, int]:
    try:
        import rarfile
    except ImportError as exc:
        raise ConversionToolMissingError(
            "rarfile is not installed. Install requirements or run the bot in Docker."
        ) from exc

    total_bytes = 0
    file_count = 0
    output_zip_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        with rarfile.RarFile(input_path) as source_archive:
            with ZipFile(output_zip_path, "w", compression=ZIP_DEFLATED) as result_archive:
                for item in source_archive.infolist():
                    if item.isdir():
                        continue

                    safe_name = _safe_archive_name(item.filename)
                    if safe_name is None:
                        continue

                    file_count, total_bytes = _add_archive_file_to_limits(
                        safe_name,
                        item.file_size,
                        file_count,
                        total_bytes,
                        max_total_bytes,
                        max_files,
                    )

                    with source_archive.open(item) as source_file:
                        with result_archive.open(safe_name, "w") as output_file:
                            shutil.copyfileobj(source_file, output_file)
    except rarfile.PasswordRequired as exc:
        raise ConversionError("Encrypted RAR archives are not supported.") from exc
    except rarfile.RarCannotExec as exc:
        raise ConversionToolMissingError(
            "RAR extraction tool was not found. Install unar or run the bot in Docker."
        ) from exc
    except rarfile.Error as exc:
        raise ConversionError("RAR archive is damaged or not supported.") from exc

    return file_count, total_bytes


def _extract_archive_to_zip_sync(
    input_path: Path,
    output_zip_path: Path,
    max_total_bytes: int,
    max_files: int,
) -> tuple[int, int]:
    if max_total_bytes <= 0:
        raise ConversionError("Archive output size limit must be positive.")
    if max_files <= 0:
        raise ConversionError("Archive file count limit must be positive.")

    if _archive_is_zip(input_path):
        file_count, total_bytes = _write_zip_archive_contents(
            input_path,
            output_zip_path,
            max_total_bytes,
            max_files,
        )
    elif _archive_is_tar(input_path):
        file_count, total_bytes = _write_tar_archive_contents(
            input_path,
            output_zip_path,
            max_total_bytes,
            max_files,
        )
    elif _archive_is_7z(input_path):
        file_count, total_bytes = _write_7z_archive_contents(
            input_path,
            output_zip_path,
            max_total_bytes,
            max_files,
        )
    elif _archive_is_rar(input_path):
        file_count, total_bytes = _write_rar_archive_contents(
            input_path,
            output_zip_path,
            max_total_bytes,
            max_files,
        )
    else:
        raise ConversionError("Archive format is not supported.")

    if file_count == 0:
        output_zip_path.unlink(missing_ok=True)
        raise ConversionError("Archive does not contain files that can be unpacked.")
    if not output_zip_path.exists() or output_zip_path.stat().st_size == 0:
        raise ConversionError("Archive was processed, but the output ZIP was not created.")

    return file_count, total_bytes


async def extract_archive_to_zip(
    input_path: Path,
    output_zip_path: Path,
    max_total_bytes: int,
    max_files: int,
) -> tuple[int, int]:
    return await asyncio.to_thread(
        _extract_archive_to_zip_sync,
        input_path,
        output_zip_path,
        max_total_bytes,
        max_files,
    )


async def list_archive_files(
    input_path: Path,
    max_total_bytes: int,
    max_files: int,
) -> list[ArchiveEntry]:
    return await asyncio.to_thread(_list_archive_files_sync, input_path, max_files)


def _extract_normalized_zip_to_files(
    normalized_zip_path: Path,
    output_dir: Path,
    selected_names: set[str] | None,
) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    output_root = output_dir.resolve()
    output_paths: list[Path] = []

    try:
        with ZipFile(normalized_zip_path) as archive:
            for item in archive.infolist():
                if item.is_dir():
                    continue

                safe_name = _safe_archive_name(item.filename)
                if safe_name is None:
                    continue
                if selected_names is not None and safe_name not in selected_names:
                    continue

                output_path = (output_dir / safe_name).resolve()
                if output_root not in output_path.parents:
                    raise ConversionError("Archive contains unsafe file paths.")

                output_path.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(item) as source_file:
                    with output_path.open("wb") as output_file:
                        shutil.copyfileobj(source_file, output_file)
                output_paths.append(output_path)
    except BadZipFile as exc:
        raise ConversionError("ZIP archive is damaged or not supported.") from exc

    if not output_paths:
        raise ConversionError("No selected files were extracted.")
    return output_paths


def _extract_selected_zip_files(
    input_path: Path,
    output_dir: Path,
    selected_names: set[str],
    max_total_bytes: int,
    max_files: int,
) -> tuple[list[Path], int]:
    output_paths: list[Path] = []
    total_bytes = 0
    file_count = 0
    try:
        with ZipFile(input_path) as source_archive:
            for item in source_archive.infolist():
                if item.is_dir():
                    continue
                safe_name = _safe_archive_name(item.filename)
                if safe_name is None or safe_name not in selected_names:
                    continue
                file_count, total_bytes = _add_archive_file_to_limits(
                    safe_name,
                    item.file_size,
                    file_count,
                    total_bytes,
                    max_total_bytes,
                    max_files,
                )
                with source_archive.open(item) as source_file:
                    output_paths.append(
                        _copy_archive_stream_to_file(source_file, output_dir, safe_name)
                    )
    except BadZipFile as exc:
        raise ConversionError("ZIP archive is damaged or not supported.") from exc
    return output_paths, total_bytes


def _extract_selected_tar_files(
    input_path: Path,
    output_dir: Path,
    selected_names: set[str],
    max_total_bytes: int,
    max_files: int,
) -> tuple[list[Path], int]:
    output_paths: list[Path] = []
    total_bytes = 0
    file_count = 0
    try:
        with tarfile.open(input_path, mode="r:*") as source_archive:
            for item in source_archive:
                if not item.isfile():
                    continue
                safe_name = _safe_archive_name(item.name)
                if safe_name is None or safe_name not in selected_names:
                    continue
                file_count, total_bytes = _add_archive_file_to_limits(
                    safe_name,
                    item.size,
                    file_count,
                    total_bytes,
                    max_total_bytes,
                    max_files,
                )
                source_file = source_archive.extractfile(item)
                if source_file is None:
                    continue
                with source_file:
                    output_paths.append(
                        _copy_archive_stream_to_file(source_file, output_dir, safe_name)
                    )
    except tarfile.TarError as exc:
        raise ConversionError("TAR archive is damaged or not supported.") from exc
    return output_paths, total_bytes


def _extract_selected_rar_files(
    input_path: Path,
    output_dir: Path,
    selected_names: set[str],
    max_total_bytes: int,
    max_files: int,
) -> tuple[list[Path], int]:
    try:
        import rarfile
    except ImportError as exc:
        raise ConversionToolMissingError(
            "rarfile is not installed. Install requirements or run the bot in Docker."
        ) from exc

    output_paths: list[Path] = []
    total_bytes = 0
    file_count = 0
    try:
        with rarfile.RarFile(input_path) as source_archive:
            for item in source_archive.infolist():
                if item.isdir():
                    continue
                safe_name = _safe_archive_name(item.filename)
                if safe_name is None or safe_name not in selected_names:
                    continue
                file_count, total_bytes = _add_archive_file_to_limits(
                    safe_name,
                    item.file_size,
                    file_count,
                    total_bytes,
                    max_total_bytes,
                    max_files,
                )
                with source_archive.open(item) as source_file:
                    output_paths.append(
                        _copy_archive_stream_to_file(source_file, output_dir, safe_name)
                    )
    except rarfile.PasswordRequired as exc:
        raise ConversionError("Encrypted RAR archives are not supported.") from exc
    except rarfile.RarCannotExec as exc:
        raise ConversionToolMissingError(
            "RAR extraction tool was not found. Install unar or run the bot in Docker."
        ) from exc
    except rarfile.Error as exc:
        raise ConversionError("RAR archive is damaged or not supported.") from exc
    return output_paths, total_bytes


def _extract_selected_7z_files(
    input_path: Path,
    output_dir: Path,
    selected_names: set[str],
    max_total_bytes: int,
    max_files: int,
) -> tuple[list[Path], int]:
    try:
        from py7zr import SevenZipFile
        from py7zr.exceptions import Bad7zFile, PasswordRequired
    except ImportError as exc:
        raise ConversionToolMissingError(
            "py7zr is not installed. Install requirements or run the bot in Docker."
        ) from exc

    targets: list[tuple[str, str, int]] = []
    total_bytes = 0
    file_count = 0
    try:
        with SevenZipFile(input_path, mode="r") as source_archive:
            for item in source_archive.list():
                if getattr(item, "is_directory", False):
                    continue
                safe_name = _safe_archive_name(item.filename)
                if safe_name is None or safe_name not in selected_names:
                    continue
                file_size = int(getattr(item, "uncompressed", 0) or 0)
                file_count, total_bytes = _add_archive_file_to_limits(
                    safe_name,
                    file_size,
                    file_count,
                    total_bytes,
                    max_total_bytes,
                    max_files,
                )
                targets.append((item.filename, safe_name, file_size))

            if not targets:
                return [], 0

            output_paths: list[Path] = []
            with tempfile.TemporaryDirectory(prefix="extract-selected-7z-") as temp_dir_name:
                temp_dir = Path(temp_dir_name)
                source_archive.extract(path=temp_dir, targets=[name for name, _, _ in targets])
                actual_total_bytes = 0
                for original_name, safe_name, _ in targets:
                    extracted_path = (temp_dir / original_name).resolve()
                    if not extracted_path.is_file():
                        raise ConversionError(f"7Z archive did not extract {original_name}.")
                    if temp_dir.resolve() not in extracted_path.parents:
                        raise ConversionError("7Z archive contains unsafe file paths.")
                    actual_total_bytes += extracted_path.stat().st_size
                    if actual_total_bytes > max_total_bytes:
                        raise ConversionError(
                            f"Unpacked files are too large. Limit: {max_total_bytes // (1024 * 1024)} MB."
                        )
                    output_path = _archive_output_path(output_dir, safe_name)
                    shutil.copy2(extracted_path, output_path)
                    output_paths.append(output_path)
                total_bytes = actual_total_bytes
    except Bad7zFile as exc:
        raise ConversionError("7Z archive is damaged or not supported.") from exc
    except PasswordRequired as exc:
        raise ConversionError("Encrypted 7Z archives are not supported.") from exc
    return output_paths, total_bytes


def _extract_selected_archive_files_sync(
    input_path: Path,
    output_dir: Path,
    selected_names: set[str],
    max_total_bytes: int,
    max_files: int,
) -> tuple[list[Path], int]:
    if not selected_names:
        raise ConversionError("No files selected.")
    if len(selected_names) > max_files:
        raise ConversionError(f"Too many selected files. Limit: {max_files}.")

    if _archive_is_zip(input_path):
        output_paths, total_bytes = _extract_selected_zip_files(
            input_path, output_dir, selected_names, max_total_bytes, max_files
        )
    elif _archive_is_tar(input_path):
        output_paths, total_bytes = _extract_selected_tar_files(
            input_path, output_dir, selected_names, max_total_bytes, max_files
        )
    elif _archive_is_7z(input_path):
        output_paths, total_bytes = _extract_selected_7z_files(
            input_path, output_dir, selected_names, max_total_bytes, max_files
        )
    elif _archive_is_rar(input_path):
        output_paths, total_bytes = _extract_selected_rar_files(
            input_path, output_dir, selected_names, max_total_bytes, max_files
        )
    else:
        raise ConversionError("Archive format is not supported.")

    if not output_paths:
        raise ConversionError("No selected files were extracted.")
    return output_paths, total_bytes


async def extract_archive_to_files(
    input_path: Path,
    output_dir: Path,
    max_total_bytes: int,
    max_files: int,
    selected_names: set[str] | None = None,
) -> tuple[list[Path], int]:
    if selected_names is not None:
        return await asyncio.to_thread(
            _extract_selected_archive_files_sync,
            input_path,
            output_dir,
            selected_names,
            max_total_bytes,
            max_files,
        )

    normalized_zip_path = output_dir / "_normalized_archive.zip"
    try:
        _, total_bytes = await extract_archive_to_zip(
            input_path,
            normalized_zip_path,
            max_total_bytes,
            max_files,
        )
        output_paths = await asyncio.to_thread(
            _extract_normalized_zip_to_files,
            normalized_zip_path,
            output_dir,
            selected_names,
        )
    finally:
        normalized_zip_path.unlink(missing_ok=True)

    return output_paths, total_bytes


def _merge_pdfs_sync(
    input_paths: list[Path],
    output_path: Path,
    prefer_qpdf: bool = False,
) -> None:
    if prefer_qpdf:
        qpdf_path = shutil.which("qpdf")
        if qpdf_path is not None:
            _merge_pdfs_with_qpdf(qpdf_path, input_paths, output_path)
            return

    _merge_pdfs_with_pypdf(input_paths, output_path)


def _merge_pdfs_with_qpdf(
    qpdf_path: str,
    input_paths: list[Path],
    output_path: Path,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _run_sync_process(
        [
            qpdf_path,
            "--empty",
            "--pages",
            *[str(input_path) for input_path in input_paths],
            "--",
            str(output_path),
        ]
    )

    if not output_path.exists() or output_path.stat().st_size == 0:
        raise ConversionError("Merged PDF file was not created.")


def _merge_pdfs_with_pypdf(input_paths: list[Path], output_path: Path) -> None:
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    for input_path in input_paths:
        reader = PdfReader(str(input_path))
        for page in reader.pages:
            writer.add_page(page)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("wb") as output_file:
        writer.write(output_file)

    if not output_path.exists() or output_path.stat().st_size == 0:
        raise ConversionError("Merged PDF file was not created.")


async def merge_pdfs(input_paths: list[Path], output_path: Path) -> Path:
    if len(input_paths) < 2:
        raise ConversionError("Send at least two PDF files for merging.")
    await asyncio.to_thread(_merge_pdfs_sync, input_paths, output_path)
    return output_path


def _write_selected_pages_sync(
    input_path: Path,
    output_path: Path,
    page_numbers: list[int],
    invert: bool = False,
) -> None:
    qpdf_path = shutil.which("qpdf")
    if qpdf_path is not None:
        _write_selected_pages_with_qpdf(
            qpdf_path,
            input_path,
            output_path,
            page_numbers,
            invert,
        )
        return

    _write_selected_pages_with_pypdf(input_path, output_path, page_numbers, invert)


def _compact_page_ranges(page_numbers: list[int]) -> str:
    if not page_numbers:
        raise ConversionError("No pages selected.")

    sorted_pages = sorted(set(page_numbers))
    ranges = []
    start = sorted_pages[0]
    end = sorted_pages[0]

    for page in sorted_pages[1:]:
        if page == end + 1:
            end = page
            continue

        ranges.append((start, end))
        start = page
        end = page

    ranges.append((start, end))

    parts = []
    for start, end in ranges:
        if start == end:
            parts.append(str(start + 1))
        else:
            parts.append(f"{start + 1}-{end + 1}")
    return ",".join(parts)


def _write_selected_pages_with_qpdf(
    qpdf_path: str,
    input_path: Path,
    output_path: Path,
    page_numbers: list[int],
    invert: bool,
) -> None:
    page_count_text = _run_sync_process([qpdf_path, "--show-npages", str(input_path)])
    try:
        page_count = int(page_count_text)
    except ValueError as exc:
        raise ConversionError(f"Could not read PDF page count: {page_count_text}") from exc

    selected_pages = set(page_numbers)
    if any(page < 0 or page >= page_count for page in selected_pages):
        raise ConversionError(f"PDF has {page_count} pages. Check the page numbers.")

    if invert:
        selected_pages = set(range(page_count)) - selected_pages

    if not selected_pages:
        raise ConversionError("The result would contain no pages.")

    page_range = _compact_page_ranges(list(selected_pages))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _run_sync_process(
        [
            qpdf_path,
            str(input_path),
            "--pages",
            str(input_path),
            page_range,
            "--",
            str(output_path),
        ]
    )

    if not output_path.exists() or output_path.stat().st_size == 0:
        raise ConversionError("PDF file was not created.")


def _write_selected_pages_with_pypdf(
    input_path: Path,
    output_path: Path,
    page_numbers: list[int],
    invert: bool,
) -> None:
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(str(input_path))
    page_count = len(reader.pages)
    selected_pages = set(page_numbers)

    if any(page < 0 or page >= page_count for page in selected_pages):
        raise ConversionError(f"PDF has {page_count} pages. Check the page numbers.")

    writer = PdfWriter()
    for index, page in enumerate(reader.pages):
        should_use_page = index in selected_pages
        if invert:
            should_use_page = not should_use_page
        if should_use_page:
            writer.add_page(page)

    if len(writer.pages) == 0:
        raise ConversionError("The result would contain no pages.")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("wb") as output_file:
        writer.write(output_file)

    if not output_path.exists() or output_path.stat().st_size == 0:
        raise ConversionError("PDF file was not created.")


async def extract_pdf_pages(
    input_path: Path,
    output_path: Path,
    page_numbers: list[int],
) -> Path:
    await asyncio.to_thread(
        _write_selected_pages_sync,
        input_path,
        output_path,
        page_numbers,
        False,
    )
    return output_path


async def delete_pdf_pages(
    input_path: Path,
    output_path: Path,
    page_numbers: list[int],
) -> Path:
    await asyncio.to_thread(
        _write_selected_pages_sync,
        input_path,
        output_path,
        page_numbers,
        True,
    )
    return output_path


def _page_to_pdf_bytes(page) -> bytes:
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_page(page)
    page_buffer = BytesIO()
    writer.write(page_buffer)
    return page_buffer.getvalue()


def _split_pdf_to_zip_parts_sync(
    input_path: Path,
    output_dir: Path,
    base_name: str,
    max_part_bytes: int,
) -> list[Path]:
    from pypdf import PdfReader

    reader = PdfReader(str(input_path))
    if len(reader.pages) == 0:
        raise ConversionError("PDF does not contain any pages.")

    output_dir.mkdir(parents=True, exist_ok=True)
    zip_paths: list[Path] = []
    archive: ZipFile | None = None
    current_zip_path: Path | None = None
    current_size = 0
    current_pages = 0

    def open_next_archive() -> ZipFile:
        nonlocal current_zip_path, current_size, current_pages
        current_zip_path = output_dir / f"{base_name}_pages_part_{len(zip_paths) + 1}.zip"
        zip_paths.append(current_zip_path)
        current_size = 0
        current_pages = 0
        return ZipFile(current_zip_path, "w", compression=ZIP_DEFLATED)

    archive = open_next_archive()
    try:
        for index, page in enumerate(reader.pages, start=1):
            page_bytes = _page_to_pdf_bytes(page)
            if current_pages > 0 and current_size + len(page_bytes) > max_part_bytes:
                archive.close()
                archive = open_next_archive()

            archive.writestr(f"{base_name}_page_{index}.pdf", page_bytes)
            current_size += len(page_bytes)
            current_pages += 1
    finally:
        if archive is not None:
            archive.close()

    zip_paths = [zip_path for zip_path in zip_paths if zip_path.exists()]
    if not zip_paths or any(zip_path.stat().st_size == 0 for zip_path in zip_paths):
        raise ConversionError("ZIP archive with split pages was not created.")

    return zip_paths


async def split_pdf_to_zip_parts(
    input_path: Path,
    output_dir: Path,
    base_name: str,
    max_part_bytes: int = 45 * 1024 * 1024,
) -> list[Path]:
    return await asyncio.to_thread(
        _split_pdf_to_zip_parts_sync,
        input_path,
        output_dir,
        base_name,
        max_part_bytes,
    )


def _write_pdf_part(reader, page_indexes: list[int], output_path: Path) -> None:
    from pypdf import PdfWriter

    writer = PdfWriter()
    for page_index in page_indexes:
        writer.add_page(reader.pages[page_index])

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("wb") as output_file:
        writer.write(output_file)

    if not output_path.exists() or output_path.stat().st_size == 0:
        raise ConversionError("PDF part was not created.")


def _split_pdf_to_pdf_parts_sync(
    input_path: Path,
    output_dir: Path,
    base_name: str,
    max_part_bytes: int,
) -> list[Path]:
    from pypdf import PdfReader

    reader = PdfReader(str(input_path))
    page_count = len(reader.pages)
    if page_count == 0:
        raise ConversionError("PDF does not contain any pages.")

    output_dir.mkdir(parents=True, exist_ok=True)
    target_part_bytes = max(1, int(max_part_bytes * 0.9))
    groups: list[list[int]] = []
    current_group: list[int] = []
    current_size = 0

    for page_index, page in enumerate(reader.pages):
        page_size = len(_page_to_pdf_bytes(page))
        if page_size > max_part_bytes:
            raise ConversionError(
                f"Page {page_index + 1} is larger than the Telegram upload limit by itself."
            )

        if current_group and current_size + page_size > target_part_bytes:
            groups.append(current_group)
            current_group = []
            current_size = 0

        current_group.append(page_index)
        current_size += page_size

    if current_group:
        groups.append(current_group)

    part_paths: list[Path] = []
    group_index = 0
    while group_index < len(groups):
        group = groups[group_index]
        first_page = group[0] + 1
        last_page = group[-1] + 1
        part_path = output_dir / (
            f"{base_name}_part_{len(part_paths) + 1}_pages_{first_page}-{last_page}.pdf"
        )
        _write_pdf_part(reader, group, part_path)

        if part_path.stat().st_size > max_part_bytes and len(group) > 1:
            part_path.unlink(missing_ok=True)
            middle = len(group) // 2
            groups[group_index:group_index + 1] = [group[:middle], group[middle:]]
            continue

        if part_path.stat().st_size > max_part_bytes:
            raise ConversionError(
                f"PDF part for page {first_page} is still too large to send."
            )

        part_paths.append(part_path)
        group_index += 1

    if not part_paths:
        raise ConversionError("PDF parts were not created.")

    return part_paths


async def split_pdf_to_pdf_parts(
    input_path: Path,
    output_dir: Path,
    base_name: str,
    max_part_bytes: int = 45 * 1024 * 1024,
) -> list[Path]:
    return await asyncio.to_thread(
        _split_pdf_to_pdf_parts_sync,
        input_path,
        output_dir,
        base_name,
        max_part_bytes,
    )


def _split_pdf_to_zip_sync(input_path: Path, zip_path: Path, base_name: str) -> None:
    zip_paths = _split_pdf_to_zip_parts_sync(
        input_path,
        zip_path.parent,
        base_name,
        max_part_bytes=1024 * 1024 * 1024,
    )
    zip_paths[0].replace(zip_path)

async def split_pdf_to_zip(input_path: Path, zip_path: Path, base_name: str) -> Path:
    await asyncio.to_thread(_split_pdf_to_zip_sync, input_path, zip_path, base_name)
    return zip_path
