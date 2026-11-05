"""Safe archive extraction: ZIP, TAR(.gz/.bz2/.xz), 7Z, RAR.

Each format is a small reader with two methods: `members()` and `open(member)`.
All the safety checks (path traversal, file count, unpacked size) live in one
place: `list_files()` and `extract()`.
"""

from __future__ import annotations

import asyncio
import shutil
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import IO, Iterator
from zipfile import BadZipFile, ZipFile

from app.formats import extension
from app.converters.process import ConversionError, ToolMissingError

CHUNK = 1024 * 1024


@dataclass(frozen=True)
class Member:
    name: str  # normalised, safe relative path
    size: int
    raw: object  # format-specific handle


def safe_name(raw_name: str) -> str | None:
    """Reject absolute paths and `..` so files can't escape the output folder."""
    normalized = raw_name.replace("\\", "/").strip("/")
    if not normalized:
        return None
    path = PurePosixPath(normalized)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return None
    return path.as_posix()


class _Reader:
    def __init__(self, path: Path) -> None:
        self.path = path

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:  # pragma: no cover - overridden
        pass

    def members(self) -> Iterator[Member]:
        raise NotImplementedError

    def open(self, member: Member) -> IO[bytes]:
        raise NotImplementedError


class _ZipReader(_Reader):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        try:
            self.archive = ZipFile(path)
        except BadZipFile as exc:
            raise ConversionError("ZIP-архив повреждён") from exc

    def close(self) -> None:
        self.archive.close()

    def members(self) -> Iterator[Member]:
        for info in self.archive.infolist():
            if info.is_dir():
                continue
            if info.flag_bits & 0x1:
                raise ConversionError("Архивы с паролем не поддерживаются")
            name = safe_name(info.filename)
            if name:
                yield Member(name, info.file_size, info)

    def open(self, member: Member) -> IO[bytes]:
        return self.archive.open(member.raw)


class _TarReader(_Reader):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        try:
            self.archive = tarfile.open(path, mode="r:*")
        except tarfile.TarError as exc:
            raise ConversionError("TAR-архив повреждён") from exc

    def close(self) -> None:
        self.archive.close()

    def members(self) -> Iterator[Member]:
        for info in self.archive:
            if not info.isfile():  # skips links, devices, folders
                continue
            name = safe_name(info.name)
            if name:
                yield Member(name, info.size, info)

    def open(self, member: Member) -> IO[bytes]:
        handle = self.archive.extractfile(member.raw)
        if handle is None:
            raise ConversionError(f"Не удалось прочитать {member.name}")
        return handle


class _RarReader(_Reader):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        try:
            import rarfile
        except ImportError as exc:
            raise ToolMissingError("rarfile не установлен") from exc
        self._rarfile = rarfile
        try:
            self.archive = rarfile.RarFile(path)
            if self.archive.needs_password():
                raise ConversionError("Архивы с паролем не поддерживаются")
        except rarfile.Error as exc:
            raise ConversionError("RAR-архив повреждён") from exc

    def close(self) -> None:
        self.archive.close()

    def members(self) -> Iterator[Member]:
        for info in self.archive.infolist():
            if info.isdir():
                continue
            name = safe_name(info.filename)
            if name:
                yield Member(name, info.file_size, info)

    def open(self, member: Member) -> IO[bytes]:
        try:
            return self.archive.open(member.raw)
        except self._rarfile.RarCannotExec as exc:
            raise ToolMissingError("для RAR нужен unar или unrar") from exc
        except self._rarfile.Error as exc:
            raise ConversionError("RAR-архив повреждён") from exc


class _SevenZipReader(_Reader):
    """py7zr can't stream single files, so wanted files are unpacked to a temp dir first."""

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        try:
            import py7zr
            from py7zr.exceptions import Bad7zFile, PasswordRequired
        except ImportError as exc:
            raise ToolMissingError("py7zr не установлен") from exc
        self._py7zr = py7zr
        self._tmp: tempfile.TemporaryDirectory | None = None
        try:
            with py7zr.SevenZipFile(path, mode="r") as archive:
                if archive.needs_password():
                    raise ConversionError("Архивы с паролем не поддерживаются")
                self._infos = [i for i in archive.list() if not i.is_directory]
        except (Bad7zFile, PasswordRequired) as exc:
            raise ConversionError("7Z-архив повреждён или защищён паролем") from exc

    def close(self) -> None:
        if self._tmp is not None:
            self._tmp.cleanup()

    def members(self) -> Iterator[Member]:
        for info in self._infos:
            name = safe_name(info.filename)
            if name:
                yield Member(name, int(info.uncompressed or 0), info.filename)

    def prepare(self, members: list[Member]) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="7z-", dir=self.path.parent)
        with self._py7zr.SevenZipFile(self.path, mode="r") as archive:
            archive.extract(path=self._tmp.name, targets=[m.raw for m in members])

    def open(self, member: Member) -> IO[bytes]:
        assert self._tmp is not None
        root = Path(self._tmp.name).resolve()
        path = (root / member.raw).resolve()
        if root not in path.parents or not path.is_file():
            raise ConversionError(f"Не удалось распаковать {member.name}")
        return path.open("rb")


def _reader(path: Path) -> _Reader:
    ext = extension(path.name)
    if ext == ".zip":
        return _ZipReader(path)
    if ext in {".tar", ".tgz", ".tar.gz", ".tar.bz2", ".tar.xz"}:
        return _TarReader(path)
    if ext == ".7z":
        return _SevenZipReader(path)
    if ext == ".rar":
        return _RarReader(path)
    raise ConversionError("Этот формат архива не поддерживается")


def _list(path: Path, max_files: int) -> list[Member]:
    with _reader(path) as reader:
        members = []
        for member in reader.members():
            members.append(member)
            if len(members) > max_files:
                raise ConversionError(f"В архиве больше {max_files} файлов")
    if not members:
        raise ConversionError("В архиве нет файлов")
    return members


async def list_files(path: Path, max_files: int) -> list[tuple[str, int]]:
    members = await asyncio.to_thread(_list, path, max_files)
    return [(m.name, m.size) for m in members]


def _extract(path: Path, out_dir: Path, wanted: set[str] | None, max_bytes: int, max_files: int) -> list[Path]:
    out_root = out_dir.resolve()
    with _reader(path) as reader:
        members = [m for m in reader.members() if wanted is None or m.name in wanted]
        if not members:
            raise ConversionError("Нужные файлы в архиве не найдены")
        if len(members) > max_files:
            raise ConversionError(f"В архиве больше {max_files} файлов")
        if sum(m.size for m in members) > max_bytes:
            raise ConversionError(f"После распаковки файлы займут больше {max_bytes // CHUNK} МБ")
        if isinstance(reader, _SevenZipReader):
            reader.prepare(members)

        written = 0
        result: list[Path] = []
        for member in members:
            target = (out_dir / member.name).resolve()
            if out_root not in target.parents:
                raise ConversionError("В архиве небезопасные пути к файлам")
            target.parent.mkdir(parents=True, exist_ok=True)
            # Count real bytes too: sizes in archive headers can lie (zip bombs).
            with reader.open(member) as src, target.open("wb") as dst:
                while chunk := src.read(CHUNK):
                    written += len(chunk)
                    if written > max_bytes:
                        raise ConversionError(f"После распаковки файлы займут больше {max_bytes // CHUNK} МБ")
                    dst.write(chunk)
            result.append(target)
    return result


async def extract(
    path: Path,
    out_dir: Path,
    *,
    max_bytes: int,
    max_files: int,
    wanted: set[str] | None = None,
) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        return await asyncio.to_thread(_extract, path, out_dir, wanted, max_bytes, max_files)
    except BaseException:
        shutil.rmtree(out_dir, ignore_errors=True)
        raise
