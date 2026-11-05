"""A tiny fake Telegram Bot API server for end-to-end tests."""

from __future__ import annotations

import itertools
import json
import time
from dataclasses import dataclass, field
from typing import Any

from aiohttp import web


@dataclass
class Call:
    method: str
    data: dict[str, Any]
    file_bytes: bytes | None = None
    file_name: str | None = None


@dataclass
class FakeTelegram:
    token: str = "42:TEST"
    files: dict[str, bytes] = field(default_factory=dict)
    calls: list[Call] = field(default_factory=list)
    _ids: itertools.count = field(default_factory=lambda: itertools.count(1000))

    def app(self) -> web.Application:
        app = web.Application(client_max_size=200 * 1024 * 1024)
        app.router.add_post("/bot{token}/{method}", self._method)
        app.router.add_get("/file/bot{token}/{path:.*}", self._file)
        return app

    def _message(self, chat_id: Any, **extra: Any) -> dict[str, Any]:
        return {"message_id": next(self._ids), "date": int(time.time()),
                "chat": {"id": int(chat_id), "type": "private"}, **extra}

    async def _method(self, request: web.Request) -> web.Response:
        method = request.match_info["method"]
        form = await request.post()
        data: dict[str, Any] = {}
        file_bytes = file_name = None
        for key, value in form.items():
            if hasattr(value, "file"):
                file_bytes = value.file.read()
                file_name = value.filename
            else:
                data[key] = value
        for key in ("reply_markup", "prices"):
            if key in data:
                data[key] = json.loads(data[key])
        self.calls.append(Call(method, data, file_bytes, file_name))

        result: Any = True
        if method == "getMe":
            result = {"id": 42, "is_bot": True, "first_name": "Bot", "username": "test_bot"}
        elif method in {"sendMessage", "sendDocument", "sendInvoice"}:
            result = self._message(data["chat_id"], text=data.get("text", ""))
        elif method == "editMessageText":
            result = self._message(data["chat_id"], text=data.get("text", ""))
        elif method == "getFile":
            file_id = data["file_id"]
            result = {"file_id": file_id, "file_unique_id": "u" + file_id,
                      "file_size": len(self.files[file_id]), "file_path": f"documents/{file_id}"}
        return web.json_response({"ok": True, "result": result})

    async def _file(self, request: web.Request) -> web.Response:
        file_id = request.match_info["path"].split("/")[-1]
        return web.Response(body=self.files[file_id])

    # helpers for assertions
    def by(self, method: str) -> list[Call]:
        return [c for c in self.calls if c.method == method]

    def texts(self) -> list[str]:
        return [c.data.get("text", "") for c in self.calls if c.method in {"sendMessage", "editMessageText"}]

    def last_markup(self) -> dict[str, Any]:
        for call in reversed(self.calls):
            if call.method == "sendMessage" and isinstance(call.data.get("reply_markup"), dict) \
                    and "inline_keyboard" in call.data["reply_markup"]:
                return call.data["reply_markup"]
        raise AssertionError("no inline keyboard sent")

    def button(self, text_part: str) -> str:
        for row in self.last_markup()["inline_keyboard"]:
            for button in row:
                if text_part in button["text"]:
                    return button["callback_data"]
        raise AssertionError(f"no button {text_part!r} in {self.last_markup()}")
