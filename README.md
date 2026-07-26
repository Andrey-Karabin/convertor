# Telegram DJVU-to-PDF Bot

Telegram bot for converting `.djvu` and `.djv` files to PDF. The first version
does not run OCR, so the resulting PDF is intended for reading and printing.

## Requirements

- Docker Desktop
- Telegram bot token from BotFather

## Setup

1. Copy `.env.example` to `.env`.
2. Put your Telegram token into `BOT_TOKEN`.
3. Build and start the bot:

```powershell
docker compose up --build
```

## Usage

Send a `.djvu` or `.djv` document to the bot. It will show a
`Конвертировать в PDF` button and return the converted PDF after processing.

## Configuration

- `BOT_TOKEN` - required Telegram bot token.
- `MAX_INPUT_MB` - maximum accepted input file size, default is `50`.
- `WORKDIR` - temporary work directory inside the container, default is
  `/tmp/convertor`.
