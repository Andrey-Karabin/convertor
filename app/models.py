"""Small data classes shared by the bot and the job queue."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from app.formats import FileKind, detect_kind


@dataclass(frozen=True)
class IncomingFile:
    """A file a user sent. Only Telegram ids are stored; bytes are downloaded when a job runs."""

    file_id: str
    name: str
    size: int
    kind: FileKind
    message_id: int = 0

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["kind"] = self.kind.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "IncomingFile":
        return cls(
            file_id=data["file_id"],
            name=data["name"],
            size=int(data.get("size") or 0),
            kind=FileKind(data["kind"]),
            message_id=int(data.get("message_id") or 0),
        )

    @classmethod
    def from_message(cls, message) -> "IncomingFile | None":
        """Build from an aiogram Message with a document or photo."""
        if message.document is not None:
            doc = message.document
            name = doc.file_name or "file"
            kind = detect_kind(name)
            if kind is None and (doc.mime_type or "").startswith("image/"):
                kind = FileKind.IMAGE
                name = f"{name}.jpg"
            if kind is None:
                return None
            return cls(doc.file_id, name, doc.file_size or 0, kind, message.message_id)
        if message.photo:
            photo = message.photo[-1]  # largest size
            return cls(photo.file_id, f"photo_{message.message_id}.jpg", photo.file_size or 0,
                       FileKind.IMAGE, message.message_id)
        return None


@dataclass
class Output:
    """A result file the bot will send back."""

    path: Path
    filename: str
    caption: str = ""
    ocr: bool = True  # run OCR before sending (only matters for PDFs)


@dataclass
class JobResult:
    outputs: list[Output] = field(default_factory=list)
    text: str | None = None  # an extra message instead of / in addition to files
    reply_markup: Any = None
