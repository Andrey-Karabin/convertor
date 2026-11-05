# Telegram File-to-PDF Bot

Telegram bot for converting files and photos to PDF. This version does not run
OCR, so PDFs made from scans and photos are intended for reading and printing,
but text inside images is not recognized.

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

## Large Files With Local Telegram Bot API

The public Telegram Bot API can upload documents only up to 50 MB. For large
PDF books, run a self-hosted Telegram Bot API server next to the bot.

1. Get `api_id` and `api_hash` at https://my.telegram.org.
2. Fill these values in `.env`:

```env
TELEGRAM_API_ID=123456
TELEGRAM_API_HASH=your_api_hash
TELEGRAM_API_BASE=http://telegram-bot-api:8081
TELEGRAM_API_IS_LOCAL=true
TELEGRAM_REQUEST_TIMEOUT=3600
MAX_OUTPUT_MB=1900
DJVU_PDF_QUALITY=printer
DJVU_PDF_KEEP_RESOLUTION=true
DJVU_CONVERT_WORKERS=4
DJVU_AUTO_ORIENT_PAGES=true
DJVU_AUTO_ORIENT_MODE=landscape
DJVU_AUTO_ORIENT_WORKERS=4
DJVU_AUTO_ORIENT_DPI=110
```

3. Before switching the bot to the local API server, log it out from the public
   Bot API once:

```powershell
Invoke-RestMethod "https://api.telegram.org/bot<YOUR_BOT_TOKEN>/logOut"
```

4. Start both containers:

```powershell
docker compose --profile local-api up --build
```

The local API server listens on `127.0.0.1:8081` on the host and on
`http://telegram-bot-api:8081` inside Docker. In local mode the bot can send
PDF files up to the `MAX_OUTPUT_MB` value instead of splitting them at 50 MB.

## Usage

Send a supported file or photo to the bot. It will show a
`Конвертировать в PDF` button and return the converted PDF after processing.

If several PDF files are sent as one Telegram batch, the bot automatically
offers to merge them. If several supported non-PDF files or photos are sent as
one batch, the bot offers two actions: convert each file to PDF, or convert all
files to PDF and merge them into one result.

The bot also has persistent menu buttons near the message input:

- `Описание функционала` - shows supported conversion formats.
- `Интересные функции` - opens PDF tools.

Supported input formats:

- `.djvu`, `.djv`
- `.doc`, `.docx`, `.odt`, `.rtf`, `.txt`, `.html`, `.htm`
- `.xls`, `.xlsx`, `.ods`, `.csv`
- `.ppt`, `.pptx`, `.odp`
- `.jpg`, `.jpeg`, `.png`, `.webp`, `.bmp`, `.tif`, `.tiff`
- `.zip`, `.tar`, `.tar.gz`, `.tgz`, `.tar.bz2`, `.tar.xz`, `.7z`, `.rar`
- Telegram photos sent directly to the chat

PDF tools:

- Merge multiple PDF files into one file.
- Split one PDF into separate page files inside a ZIP archive.
- In split mode, type a page range like `1-3,5` to extract selected pages into a new PDF.
- Delete selected pages from a PDF.
- Compress a PDF file.

Other tools:

- Compress photos and image files to JPEG.
- Safely unpack ZIP/TAR/7Z/RAR archives. In Telegram, the bot can send all
  extracted files one by one or show a file list so the user can choose one
  file to extract. Archive extraction is limited by total unpacked size and
  file count.
  Encrypted archives are not supported.

You can open PDF tools from the `Интересные функции` menu or simply send a
PDF file to the bot. When a PDF is uploaded directly, the bot offers available
actions for that file.

## Streamlit Web Converter

The project also includes a Streamlit web interface with the same conversion
backend. It can convert DJVU, Office documents, spreadsheets, presentations,
and images to PDF. It also includes PDF tools for merge, compression, page
extraction, page deletion, and splitting pages into a ZIP archive. The web
interface also supports image compression and safe archive unpacking.

Start the Telegram bot and web app together:

```powershell
docker compose --profile web up -d --build
```

Open `http://localhost:8501` on your machine. On a VPS, open
`http://SERVER_IP:8501` only if the port is allowed by the firewall.

Set `WEB_PASSWORD` in `.env` before exposing the web app to the internet. If
`WEB_PASSWORD` is empty, anyone who can reach the port can use the converter.

## Office Document Fidelity

DOC/DOCX/PPT/XLS conversion is done by LibreOffice inside Docker. Layout can
shift when the original document uses fonts that are not available in the
container. The image includes open metric-compatible fonts for common Word
fonts, including Carlito, Caladea, Liberation, and Noto.

For the closest match to Microsoft Word, put original `.ttf` or `.otf` fonts
into the local `fonts/` folder and restart the container. For example, from a
Windows machine you can copy Calibri, Cambria, Arial, Times New Roman, and
Courier New font files from `C:\Windows\Fonts`. The bot mounts this folder into
the container and refreshes the font cache before Office conversion.

## Configuration

- `BOT_TOKEN` - required Telegram bot token.
- `MAX_INPUT_MB` - maximum accepted input file size, default is `200`.
- `MAX_OUTPUT_MB` - maximum result file size the bot tries to upload back to
  Telegram, default is `49`.
  Large PDF results are automatically split into several PDF parts before
  sending.
- `DJVU_PDF_QUALITY` - Ghostscript compression profile for DJVU scans, default
  is `printer`. Use `prepress` for maximum quality, `ebook` for moderate
  compression, or `screen` for smaller files.
- `DJVU_PDF_KEEP_RESOLUTION` - keep source image resolution during DJVU to PDF
  conversion. Default is `true`; set to `false` only when smaller files matter
  more than text sharpness.
- `DJVU_CONVERT_WORKERS` - number of parallel DJVU page-range conversion
  workers. Use `4` as a safe default; increase on a VPS with enough CPU, RAM,
  and disk speed.
- `DJVU_AUTO_ORIENT_PAGES` - detect sideways scanned pages after DJVU to PDF
  conversion and rotate them automatically. Default is `true`.
- `DJVU_AUTO_ORIENT_MODE` - which pages to inspect: `landscape` checks only
  wide pages and is faster; `all` checks every page and is slower but catches
  more edge cases.
- `DJVU_AUTO_ORIENT_WORKERS` - number of parallel page orientation checks.
- `DJVU_AUTO_ORIENT_DPI` - preview render DPI for orientation detection. Higher
  values can improve detection but make the step slower.
- `DJVU_AUTO_ORIENT_MIN_CONFIDENCE` - minimum Tesseract OSD confidence for
  rotating a page. Increase it if pages are rotated incorrectly.
- `WORKDIR` - temporary work directory inside the container, default is
  `/tmp/convertor`.
- `DB_PATH` - SQLite database path for users, metrics, and limit accounting,
  default is `/tmp/convertor/bot.sqlite3`.
- `ADMIN_IDS` - comma-separated numeric Telegram user ids that can open
  `/admin`. Admins are not restricted by weekly limits.
- `WEEKLY_LIMIT_FILES` - successful operations allowed per user for the last
  7 days. `0` disables this limit.
- `WEEKLY_LIMIT_MB` - total input megabytes allowed per user for the last
  7 days. `0` disables this limit.
- `IMAGE_COMPRESS_QUALITY` - JPEG quality for compressed photos, default is
  `75`.
- `IMAGE_COMPRESS_MAX_SIDE` - maximum width or height for compressed photos,
  default is `1600`. Use `0` to keep original dimensions.
- `PDF_COMPRESS_QUALITY` - Ghostscript compression profile for PDF files,
  default is `ebook`. Use `screen` for smaller files or `printer` for better
  quality.
- `ARCHIVE_MAX_OUTPUT_MB` - maximum total unpacked archive size, default is
  `500`.
- `ARCHIVE_MAX_FILES` - maximum number of files allowed inside an archive,
  default is `200`.
- `STREAMLIT_PORT` - host port for the Streamlit web interface, default is
  `8501`.
- `STREAMLIT_MAX_UPLOAD_MB` - Streamlit upload limit in megabytes, default is
  `500`.
- `WEB_PASSWORD` - optional password for the Streamlit web interface. Leave it
  empty only for local or otherwise protected deployments.
- `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` - required only for the local
  Telegram Bot API server.
- `TELEGRAM_API_BASE` - set to `http://telegram-bot-api:8081` to make the bot
  use the local API container.
- `TELEGRAM_API_IS_LOCAL` - set to `true` when the local API server runs with
  `TELEGRAM_LOCAL=1`.
- `TELEGRAM_REQUEST_TIMEOUT` - request timeout for the local API server. Keep it
  high for large PDF uploads.

## Admin panel and limits

Set your numeric Telegram id in `ADMIN_IDS`, restart the bot, then send
`/admin` to the bot. The panel shows weekly/total usage metrics, successful and
failed operations, top users, and current limit settings.

Weekly limits are counted by successful operations over the last 7 days. They
apply to conversions and PDF tools such as merge, split, extract pages, and
delete pages. Admin ids from `ADMIN_IDS` bypass the limits for testing and
maintenance.
