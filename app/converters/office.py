"""Word / Excel / PowerPoint / text -> PDF via LibreOffice."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from app.converters.process import ConversionError, find_tool, run

EXPORT_FILTERS = {
    "writer_pdf_Export": {".doc", ".docx", ".odt", ".rtf", ".txt", ".html", ".htm"},
    "calc_pdf_Export": {".xls", ".xlsx", ".ods", ".csv"},
    "impress_pdf_Export": {".ppt", ".pptx", ".odp"},
}


def _export_filter(source: Path) -> str:
    ext = source.suffix.lower()
    for name, extensions in EXPORT_FILTERS.items():
        if ext in extensions:
            return f"pdf:{name}"
    return "pdf"


async def to_pdf(source: Path, output: Path, *, timeout: int = 600) -> Path:
    soffice = find_tool("soffice", "libreoffice")
    out_dir = output.parent / "lo_out"
    profile = output.parent / "lo_profile"  # own profile => parallel runs don't clash
    out_dir.mkdir(parents=True, exist_ok=True)
    profile.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env.update({"HOME": str(profile), "SAL_USE_VCLPLUGIN": "svp", "LANG": env.get("LANG", "C.UTF-8")})
    env.pop("DISPLAY", None)

    try:
        await run(
            [soffice, "--headless", "--nologo", "--nodefault", "--nofirststartwizard",
             "--nolockcheck", "--norestore", f"-env:UserInstallation={profile.resolve().as_uri()}",
             "--convert-to", _export_filter(source), "--outdir", str(out_dir), str(source)],
            timeout,
            env=env,
        )
        produced = [p for p in out_dir.glob("*.pdf") if p.stat().st_size > 0]
        if not produced:
            raise ConversionError("LibreOffice не смог открыть документ")
        produced[0].replace(output)
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)
        shutil.rmtree(profile, ignore_errors=True)
    return output
