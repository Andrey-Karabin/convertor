# Converter Bot

Telegram-бот, который переводит файлы в PDF .

* DjVu, Word/Excel/PowerPoint/ODT/RTF/TXT/HTML, JPG/PNG/WebP/BMP/TIFF → PDF
* **OCR во всех PDF, которые выдаёт бот** (русский + английский). Страницы, где текст уже есть, пропускаются
* PDF-инструменты: склейка, вырезание страниц, удаление страниц, разделение на страницы, сжатие
* Сжатие фото, безопасная распаковка ZIP/RAR/7Z/TAR


## Структура проекта

```
app/
  __main__.py          # точка входа: python -m app
  config.py            # все настройки (из .env)
  formats.py           # поддерживаемые форматы, имена файлов, диапазоны страниц
  texts.py             # все тексты бота в одном месте
  models.py            # IncomingFile, Output, JobResult
  converters/          # чистая конвертация, ничего не знает про Telegram
    process.py         #   запуск внешних программ (при отмене процессы убиваются)
    djvu.py office.py images.py pdf.py archives.py ocr.py
  services/
    db.py              # SQLite: пользователи, статистика, баланс, платежи, задачи
    billing.py         # лимиты, резерв и возврат операций, покупки
    queue.py           # очередь, статусы, отмена, восстановление после перезапуска
    jobs.py            # что делает каждый тип задачи
    delivery.py        # OCR перед отправкой, нарезка больших PDF
  bot/
    setup.py           # Bot, Dispatcher, FSM в Redis
    callbacks.py       # данные кнопок (CallbackData)
    keyboards.py       # все клавиатуры
    flow.py            # состояния диалога, запомненные файлы, постановка в очередь
    middlewares.py     # альбомы (несколько файлов разом), учёт пользователей
    handlers/          # common, files, pdf_tools, archives, payments, admin, fallback
tests/                 # тесты конвертеров и сквозные тесты бота
```


## Развёртывание на виртуальной машине

1. Установите Docker:
   ```bash
   curl -fsSL https://get.docker.com | sudo sh
   sudo usermod -aG docker $USER   # затем выйдите из SSH и зайдите снова
   ```
2. Скопируйте проект на сервер вместе с `uv.lock` (через git или `scp -r convertor user@server:~/`) и перейдите в папку:
   ```bash
   cd ~/convertor
   cp .env.example .env
   nano .env        # BOT_TOKEN, ADMIN_IDS, цены
   ```
3. Запустите:
   ```bash
   docker compose up -d --build
   docker compose logs -f bot      # логи; выход — Ctrl+C
   ```

Благодаря `restart: unless-stopped` бот сам поднимется после перезагрузки сервера.

**Обновление:** скопируйте новый код и выполните `docker compose up -d --build`. Незавершённые задачи продолжатся после перезапуска.

**Резервная копия базы:**
```bash
docker compose cp bot:/app/data/bot.sqlite3 ./backup-$(date +%F).sqlite3
```

**Большие файлы (до 2 ГБ).** Через публичный Bot API бот может скачать только 20 МБ и отправить 50 МБ. Для файлов больше получите `api_id`/`api_hash` на https://my.telegram.org и раскомментируйте в `.env` блок «local Bot API server», включая `COMPOSE_PROFILES=local-api`. Тогда локальный сервер запустится вместе с ботом.

**Шрифты.** Чтобы документы Word выглядели как в Windows, можно положить свои `.ttf` в папку `fonts/` на сервере. Не кладите шрифты Microsoft в публичный образ: лицензия запрещает их распространять.


## Оплата через Telegram Stars

Цифровые услуги в ботах продаются только за Stars (валюта `XTR`). Платёжный провайдер в BotFather не нужен. Stars копятся на балансе бота, вывести их можно через Fragment (BotFather → ваш бот → Balance).

* `/buy` — пакеты операций (настраиваются в `PRODUCTS`)
* `/balance` — бесплатный остаток на неделю, купленные операции, безлимит
* `/terms`, `/paysupport` — обязательны по правилам Telegram
* 1 операция = 1 входящий файл. Если задача упала или отменена, операция возвращается автоматически

## Команды администратора

* `/admin` — статистика (пользователи, операции, Stars) и текущие настройки
* `/grant user_id N` — начислить (или списать при отрицательном N) операции
* `/payments user_id` — последние платежи пользователя с `charge_id`
* `/refund charge_id` — вернуть Stars и списать купленное



```

