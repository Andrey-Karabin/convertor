FROM python:3.12-slim-bookworm

# uv: installs the exact versions from uv.lock
COPY --from=ghcr.io/astral-sh/uv:0.13.4 /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8 \
    DATA_DIR=/app/data \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH

# Conversion tools:
#   djvulibre-bin + ghostscript  DjVu -> PDF, PDF compression
#   libreoffice-*                Word/Excel/PowerPoint -> PDF
#   tesseract-ocr-rus/eng        OCR languages (used by ocrmypdf)
#   qpdf                         fast PDF merge / page selection
#   unar                         RAR extraction
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        djvulibre-bin \
        ghostscript \
        qpdf \
        unar \
        tesseract-ocr \
        tesseract-ocr-osd \
        tesseract-ocr-rus \
        tesseract-ocr-eng \
        libreoffice-writer-nogui \
        libreoffice-calc-nogui \
        libreoffice-impress-nogui \
        fontconfig \
        fonts-dejavu \
        fonts-liberation2 \
        fonts-crosextra-carlito \
        fonts-crosextra-caladea \
        fonts-noto-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first: this layer is cached until pyproject.toml / uv.lock change.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --locked --no-dev

COPY app ./app

# Extra fonts (optional) are mounted into /usr/local/share/fonts/custom.
CMD ["sh", "-c", "fc-cache -f >/dev/null 2>&1; exec python -m app"]
