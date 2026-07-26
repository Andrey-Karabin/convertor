import asyncio
import shutil
from pathlib import Path


class ConversionError(RuntimeError):
    """Raised when a file cannot be converted."""


class ConversionToolMissingError(ConversionError):
    """Raised when ddjvu is not available in PATH."""


async def convert_djvu_to_pdf(
    input_path: Path,
    output_path: Path,
    timeout_seconds: int = 300,
) -> Path:
    ddjvu_path = shutil.which("ddjvu")
    if ddjvu_path is None:
        raise ConversionToolMissingError(
            "ddjvu was not found. Install DjVuLibre or run the bot in Docker."
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)

    process = await asyncio.create_subprocess_exec(
        ddjvu_path,
        "-format=pdf",
        str(input_path),
        str(output_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
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

    if process.returncode != 0:
        details = stderr.decode(errors="replace").strip() or stdout.decode(
            errors="replace"
        ).strip()
        if not details:
            details = f"ddjvu exited with code {process.returncode}."
        raise ConversionError(details)

    if not output_path.exists() or output_path.stat().st_size == 0:
        raise ConversionError("ddjvu finished, but the PDF file was not created.")

    return output_path
