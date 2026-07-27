FROM python:3.10-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV WORKDIR=/tmp/convertor
ENV MAX_INPUT_MB=200
ENV MAX_OUTPUT_MB=49
ENV DJVU_PDF_QUALITY=printer
ENV DJVU_PDF_KEEP_RESOLUTION=true
ENV DJVU_CONVERT_WORKERS=4
ENV DJVU_AUTO_ORIENT_PAGES=true
ENV DJVU_AUTO_ORIENT_MODE=landscape
ENV DJVU_AUTO_ORIENT_WORKERS=4
ENV DJVU_AUTO_ORIENT_DPI=110

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        djvulibre-bin \
        fontconfig \
        fonts-crosextra-caladea \
        fonts-crosextra-carlito \
        fonts-dejavu \
        fonts-liberation \
        fonts-liberation2 \
        fonts-noto-core \
        fonts-noto-cjk \
        ghostscript \
        libreoffice-calc \
        libreoffice-impress \
        libreoffice-writer \
        poppler-utils \
        qpdf \
        tesseract-ocr \
        tesseract-ocr-osd \
    && fc-cache -f \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "run.py"]
