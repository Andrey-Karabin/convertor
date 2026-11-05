"""Entry point: `python -m app`."""

from __future__ import annotations

import asyncio
import logging
import shutil

from app.bot import keyboards
from app.bot.setup import COMMANDS, create_bot, create_dispatcher, create_storage
from app.config import load_settings
from app.services.billing import Billing
from app.services.db import Database
from app.services.jobs import HANDLERS
from app.services.queue import JobQueue

REQUIRED_TOOLS = ("djvused", "djvups", "ps2pdf", "gs", "qpdf", "soffice", "ocrmypdf", "tesseract")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("app")

    settings = load_settings()
    for tool in REQUIRED_TOOLS:
        if shutil.which(tool) is None:
            log.warning("%s is not installed — some operations will fail", tool)

    db = Database(settings.db_path)
    db.open()
    billing = Billing(db, settings)
    bot = create_bot(settings)
    storage = create_storage(settings)
    dp = create_dispatcher(storage, db)
    queue = JobQueue(bot, db, billing, settings, storage, HANDLERS, keyboards.cancel_job)

    # Objects available in every handler by argument name.
    dp.workflow_data.update(settings=settings, db=db, billing=billing, queue=queue)

    try:
        await bot.set_my_commands(COMMANDS)
        await queue.start()
        log.info("Bot started")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await queue.stop()
        await storage.close()
        await bot.session.close()
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
