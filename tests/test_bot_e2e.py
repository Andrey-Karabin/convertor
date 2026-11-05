"""End-to-end: real dispatcher, handlers, queue and converters against a fake Telegram API."""

from __future__ import annotations

import asyncio
import io
import itertools
import time
import zipfile
from dataclasses import replace
from pathlib import Path

from aiohttp.test_utils import TestServer
from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Update
from PIL import Image

from app import texts
from app.bot import keyboards
from app.bot.setup import create_dispatcher
from app.config import Settings, parse_products
from app.services.billing import Billing
from app.services.db import Database
from app.services.jobs import HANDLERS
from app.services.queue import JobQueue
from tests.fake_telegram import FakeTelegram

USER = 777
ADMIN = 1


def make_settings(tmp: Path, **overrides) -> Settings:
    base = Settings(
        bot_token="42:TEST", admin_ids=frozenset({ADMIN}), data_dir=tmp, work_dir=tmp / "work",
        db_path=tmp / "bot.sqlite3", redis_url="memory", telegram_api_base=None,
        telegram_api_is_local=False, telegram_request_timeout=60, max_input_mb=20, max_output_mb=49,
        queue_workers=1, user_max_jobs=3, djvu_pdf_quality="printer", djvu_keep_resolution=True,
        djvu_workers=2, image_compress_quality=70, image_compress_max_side=800,
        pdf_compress_quality="ebook", archive_max_output_mb=50, archive_max_files=50, tool_timeout=120,
        ocr_enabled=True, ocr_languages="rus+eng", ocr_jobs=1, ocr_rotate_pages=True, ocr_timeout=120,
        billing_enabled=True, free_weekly_ops=20, products=parse_products("credits:10:5,premium:30:150"),
        support_contact="@support",
    )
    return replace(base, **overrides)


def png_bytes(color=(200, 50, 50)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (400, 300), color).save(buffer, "PNG")
    return buffer.getvalue()


def pdf_bytes(pages: int) -> bytes:
    buffer = io.BytesIO()
    frames = [Image.new("RGB", (200, 300), (i * 30 % 255, 90, 90)) for i in range(pages)]
    frames[0].save(buffer, "PDF", save_all=True, append_images=frames[1:])
    return buffer.getvalue()


class Harness:
    def __init__(self, tmp: Path, **settings) -> None:
        self.tmp = tmp
        self.settings = make_settings(tmp, **settings)
        self.tg = FakeTelegram()
        self.ids = itertools.count(1)

    async def __aenter__(self) -> "Harness":
        self.server = TestServer(self.tg.app())
        await self.server.start_server()
        base = str(self.server.make_url("")).rstrip("/")
        session = AiohttpSession(api=TelegramAPIServer.from_base(base))
        self.bot = Bot(self.settings.bot_token, session=session)
        self.db = Database(self.settings.db_path)
        self.db.open()
        self.billing = Billing(self.db, self.settings)
        self.storage = MemoryStorage()
        from app.bot.handlers import admin, archives, common, fallback, files, payments, pdf_tools
        for module in (admin, archives, common, fallback, files, payments, pdf_tools):
            module.router._parent_router = None  # routers are module-level; reattach per test
        self.dp = create_dispatcher(self.storage, self.db)
        self.queue = JobQueue(self.bot, self.db, self.billing, self.settings, self.storage, HANDLERS,
                              keyboards.cancel_job)
        self.dp.workflow_data.update(settings=self.settings, db=self.db, billing=self.billing, queue=self.queue)
        await self.queue.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.queue.stop()
        await self.bot.session.close()
        await self.server.close()
        self.db.close()

    def _user(self, user_id: int) -> dict:
        return {"id": user_id, "is_bot": False, "first_name": "Test", "username": f"u{user_id}"}

    def _msg(self, user_id: int, **extra) -> dict:
        return {"message_id": next(self.ids), "date": int(time.time()),
                "chat": {"id": user_id, "type": "private"}, "from": self._user(user_id), **extra}

    async def feed(self, user_id: int = USER, **message) -> None:
        update = Update.model_validate({"update_id": next(self.ids), "message": self._msg(user_id, **message)},
                                       context={"bot": self.bot})
        await self.dp.feed_update(self.bot, update)

    async def send_text(self, text: str, user_id: int = USER) -> None:
        await self.feed(user_id, text=text)

    async def send_file(self, name: str, data: bytes, user_id: int = USER, group: str | None = None) -> None:
        file_id = f"f{next(self.ids)}"
        self.tg.files[file_id] = data
        extra = {"media_group_id": group} if group else {}
        await self.feed(user_id, document={"file_id": file_id, "file_unique_id": "u" + file_id,
                                           "file_name": name, "file_size": len(data)}, **extra)

    async def press(self, callback_data: str, user_id: int = USER) -> None:
        update = Update.model_validate({
            "update_id": next(self.ids),
            "callback_query": {"id": str(next(self.ids)), "from": self._user(user_id), "chat_instance": "c",
                               "data": callback_data, "message": self._msg(user_id, text="kb")},
        }, context={"bot": self.bot})
        await self.dp.feed_update(self.bot, update)

    async def wait_documents(self, count: int, timeout: float = 60) -> list:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            docs = self.tg.by("sendDocument")
            if len(docs) >= count and not self.queue._running and not self.queue._waiting:
                return docs
            await asyncio.sleep(0.1)
        raise AssertionError(f"expected {count} documents, got {self.tg.by('sendDocument')}; texts: {self.tg.texts()}")

    async def idle(self, timeout: float = 30) -> None:
        deadline = time.monotonic() + timeout
        while (self.queue._running or self.queue._waiting) and time.monotonic() < deadline:
            await asyncio.sleep(0.1)


def run(coro):
    return asyncio.run(coro)


def test_image_to_pdf_and_compress(tmp_path):
    async def scenario():
        async with Harness(tmp_path) as h:
            await h.send_text("/start")
            await h.send_file("photo.png", png_bytes())
            await h.press(h.tg.button("В PDF"))
            docs = await h.wait_documents(1)
            assert docs[0].file_name == "photo.pdf"
            assert docs[0].file_bytes.startswith(b"%PDF")
            # ocrmypdf isn't installed here, so the bot must still send the PDF and say so
            assert texts.OCR_SKIPPED in docs[0].data["caption"] or "Готово" in docs[0].data["caption"]

            await h.send_file("<b>&photo2.png", png_bytes())  # HTML in names must not break messages
            await h.press(h.tg.button("Сжать фото"))
            docs = await h.wait_documents(2)
            assert docs[1].file_name == "b_photo2_small.jpg"
            # the status message with the cancel button is removed after success
            assert h.tg.by("deleteMessage")
            bal = await h.billing.balance(USER)
            assert bal.free_left == 18
    run(scenario())


def test_album_merge(tmp_path):
    async def scenario():
        async with Harness(tmp_path) as h:
            await asyncio.gather(
                h.send_file("a.png", png_bytes((10, 10, 10)), group="G1"),
                h.send_file("b.png", png_bytes((90, 90, 90)), group="G1"),
                h.send_file("c.pdf", pdf_bytes(2), group="G1"),
            )
            assert any("Получил файлов: 3" in t for t in h.tg.texts())
            await h.press(h.tg.button("в один PDF"))
            docs = await h.wait_documents(1)
            assert docs[0].file_name == "merged.pdf"
            from pypdf import PdfReader
            assert len(PdfReader(io.BytesIO(docs[0].file_bytes)).pages) == 4
            assert (await h.billing.balance(USER)).free_left == 17  # 3 files = 3 operations
    run(scenario())


def test_pdf_tools(tmp_path):
    async def scenario():
        from pypdf import PdfReader

        async with Harness(tmp_path) as h:
            # split via the menu: choose tool, send PDF, type pages
            await h.send_text(texts.BTN_PDF_TOOLS)
            await h.press(h.tg.button("Разделить"))
            await h.send_file("book.pdf", pdf_bytes(5))
            await h.send_text("abc")
            assert any("Не понял" in t for t in h.tg.texts())
            await h.send_text("2-3")
            docs = await h.wait_documents(1)
            assert docs[0].file_name == "book_pages.pdf"
            assert len(PdfReader(io.BytesIO(docs[0].file_bytes)).pages) == 2

            # merge: PDF -> "merge with others" -> another PDF -> done
            await h.send_file("one.pdf", pdf_bytes(1))
            await h.press(h.tg.button("Склеить с другими"))
            await h.press(h.tg.button("Склеить"))  # only one file yet
            await h.send_file("two.pdf", pdf_bytes(2))
            await h.press(h.tg.button("Склеить"))
            docs = await h.wait_documents(2)
            assert len(PdfReader(io.BytesIO(docs[1].file_bytes)).pages) == 3

            # delete pages
            await h.send_file("del.pdf", pdf_bytes(4))
            await h.press(h.tg.button("Удалить"))
            await h.send_text("1,4")
            docs = await h.wait_documents(3)
            assert len(PdfReader(io.BytesIO(docs[2].file_bytes)).pages) == 2

            # split every page into a zip (no OCR for zips)
            await h.send_file("all.pdf", pdf_bytes(3))
            await h.press(h.tg.button("Разделить"))
            await h.press(h.tg.button("Каждую страницу"))
            docs = await h.wait_documents(4)
            assert len(zipfile.ZipFile(io.BytesIO(docs[3].file_bytes)).namelist()) == 3
    run(scenario())


def test_archive_pick(tmp_path):
    async def scenario():
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as z:
            z.writestr("a.txt", "AAA")
            z.writestr("dir/b.txt", "BBB")
        async with Harness(tmp_path) as h:
            await h.send_file("files.zip", buffer.getvalue())
            await h.press(h.tg.button("Выбрать файл"))
            await h.idle()
            assert any("В архиве файлов: 2" in t for t in h.tg.texts())
            await h.press(h.tg.button("dir/b.txt"))
            docs = await h.wait_documents(1)
            assert docs[0].file_bytes == b"BBB"

            await h.send_file("files.zip", buffer.getvalue())
            await h.press(h.tg.button("Распаковать всё"))
            docs = await h.wait_documents(3)
            assert sorted(d.file_bytes for d in docs[1:]) == [b"AAA", b"BBB"]
            # listing is free, each extraction costs one operation
            assert (await h.billing.balance(USER)).free_left == 18
    run(scenario())


def test_limits_payment_and_refund_on_error(tmp_path):
    async def scenario():
        async with Harness(tmp_path, free_weekly_ops=1) as h:
            # a broken image fails -> the operation comes back
            await h.send_file("broken.png", b"not a png")
            await h.press(h.tg.button("В PDF"))
            await h.idle()
            assert any("❌" in t and "возвращена" in t for t in h.tg.texts())
            assert (await h.billing.balance(USER)).free_left == 1

            await h.send_file("ok.png", png_bytes())
            await h.press(h.tg.button("В PDF"))
            await h.wait_documents(1)
            assert (await h.billing.balance(USER)).free_left == 0

            # out of operations -> offer to buy
            await h.send_file("more.png", png_bytes())
            await h.press(h.tg.button("В PDF"))
            assert any("закончились" in t for t in h.tg.texts())
            await h.press(h.tg.button("Купить"))
            await h.press(h.tg.button("10 операций"))
            invoice = h.tg.by("sendInvoice")[-1]
            assert invoice.data["currency"] == "XTR" and invoice.data["prices"][0]["amount"] == 5

            # Telegram confirms the payment
            pre = Update.model_validate({"update_id": 99001, "pre_checkout_query": {
                "id": "pc1", "from": h._user(USER), "currency": "XTR", "total_amount": 5,
                "invoice_payload": invoice.data["payload"]}}, context={"bot": h.bot})
            await h.dp.feed_update(h.bot, pre)
            assert h.tg.by("answerPreCheckoutQuery")[-1].data["ok"] == "true"
            payment = {"currency": "XTR", "total_amount": 5, "invoice_payload": invoice.data["payload"],
                       "telegram_payment_charge_id": "charge-1", "provider_payment_charge_id": ""}
            await h.feed(USER, successful_payment=payment)
            await h.feed(USER, successful_payment=payment)  # duplicate delivery must not double credit
            assert (await h.billing.balance(USER)).credits == 10

            await h.send_file("more.png", png_bytes())
            await h.press(h.tg.button("В PDF"))
            await h.wait_documents(2)
            assert (await h.billing.balance(USER)).credits == 9

            # admin refund takes the credits back
            await h.send_text("/refund charge-1", user_id=ADMIN)
            assert h.tg.by("refundStarPayment")
            assert (await h.billing.balance(USER)).credits == 0
            # non-admin can't use admin commands
            await h.send_text("/admin")
            assert texts.ADMIN_ONLY in h.tg.texts()[-1]
    run(scenario())


def test_queue_position_cancel_and_per_user_limit(tmp_path):
    async def scenario():
        async with Harness(tmp_path, user_max_jobs=2) as h:
            gate = asyncio.Event()
            original = h.queue.handlers["convert"]

            async def slow(ctx):
                await gate.wait()
                return await original(ctx)

            h.queue.handlers["convert"] = slow
            for name in ("1.png", "2.png", "3.png"):
                await h.send_file(name, png_bytes())
                await h.press(h.tg.button("В PDF"))
            await asyncio.sleep(0.3)
            assert any("место в очереди: 1" in t for t in h.tg.texts())
            assert any("уже 2 задачи" in t for t in h.tg.texts())  # third job rejected

            waiting = h.queue._waiting[0]
            from app.bot.callbacks import JobCb
            await h.press(JobCb(job_id=waiting.id).pack())
            assert any("Отменено" in t for t in h.tg.texts())
            gate.set()
            await h.wait_documents(1)
            await h.idle()
            assert len(h.tg.by("sendDocument")) == 1
            assert (await h.billing.balance(USER)).free_left == 19  # cancelled one refunded
    run(scenario())


def test_jobs_survive_restart(tmp_path):
    async def scenario():
        async with Harness(tmp_path) as h:
            gate = asyncio.Event()

            async def never(ctx):
                await gate.wait()

            h.queue.handlers["convert"] = never
            await h.send_file("x.png", png_bytes())
            await h.press(h.tg.button("В PDF"))
            await asyncio.sleep(0.3)
            assert h.queue._running
        # "restart": a new harness on the same data folder picks the job up again
        h2 = Harness(tmp_path)
        h2.tg.files = h.tg.files  # same Telegram file ids stay valid after a restart
        async with h2:
            docs = await h2.wait_documents(1)
            assert docs[0].file_name == "x.pdf"
            assert any("перезапускался" in t for t in h2.tg.texts())
            assert (await h2.billing.balance(USER)).free_left == 19  # charged once
    run(scenario())
