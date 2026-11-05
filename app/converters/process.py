"""Running external tools (LibreOffice, Ghostscript, qpdf, ...)."""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
from pathlib import Path


class ConversionError(RuntimeError):
    """The file could not be processed. The message is shown to the user."""


class ToolMissingError(ConversionError):
    """An external program is not installed."""


def find_tool(*names: str) -> str:
    for name in names:
        path = shutil.which(name)
        if path:
            return path
    raise ToolMissingError(f"{names[0]} не установлен на сервере")


def has_tool(name: str) -> bool:
    return shutil.which(name) is not None


async def run(
    args: list[str],
    timeout: int,
    *,
    env: dict[str, str] | None = None,
    expect_file: Path | None = None,
    ok_codes: tuple[int, ...] = (0,),
) -> str:
    """Run a program and return its output.

    The program runs in its own process group, so on timeout or when the job is
    cancelled the whole tree (e.g. LibreOffice helpers) is killed.
    """
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        start_new_session=True,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        _kill(process)
        await process.wait()
        raise ConversionError("Обработка заняла слишком много времени") from exc
    except asyncio.CancelledError:
        _kill(process)
        await process.wait()
        raise

    out = stdout.decode(errors="replace").strip()
    err = stderr.decode(errors="replace").strip()
    if process.returncode not in ok_codes:
        details = (err or out or f"код {process.returncode}").splitlines()[-1][:300]
        raise ConversionError(f"{Path(args[0]).name}: {details}")
    if expect_file is not None and (not expect_file.exists() or expect_file.stat().st_size == 0):
        raise ConversionError("Инструмент завершился, но файл не создан")
    return out or err


def _kill(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    killpg = getattr(os, "killpg", None)  # not available on Windows
    try:
        if killpg is not None:
            killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except (ProcessLookupError, PermissionError):
        try:
            process.kill()
        except ProcessLookupError:
            pass


def ensure_file(path: Path, what: str = "Файл") -> Path:
    if not path.exists() or path.stat().st_size == 0:
        raise ConversionError(f"{what} не создан")
    return path
