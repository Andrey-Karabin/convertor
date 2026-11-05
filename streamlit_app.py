import asyncio
import os
import re
import tempfile
import zipfile
from pathlib import Path

import streamlit as st

from converter import (
    ConversionError,
    ConversionToolMissingError,
    compress_image,
    compress_pdf,
    convert_djvu_to_pdf,
    convert_image_to_pdf,
    convert_office_to_pdf,
    delete_pdf_pages,
    extract_archive_to_zip,
    extract_pdf_pages,
    merge_pdfs,
    split_pdf_to_zip,
)


DJVU_EXTENSIONS = {".djvu", ".djv"}
DOCUMENT_EXTENSIONS = {
    ".csv",
    ".doc",
    ".docx",
    ".html",
    ".htm",
    ".odp",
    ".ods",
    ".odt",
    ".ppt",
    ".pptx",
    ".rtf",
    ".txt",
    ".xls",
    ".xlsx",
}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
ARCHIVE_EXTENSIONS = {
    ".7z",
    ".rar",
    ".zip",
    ".tar",
    ".tar.gz",
    ".tgz",
    ".tar.bz2",
    ".tar.xz",
}
SUPPORTED_EXTENSIONS = DJVU_EXTENSIONS | DOCUMENT_EXTENSIONS | IMAGE_EXTENSIONS
PDF_EXTENSION = ".pdf"


def check_password() -> bool:
    password = os.getenv("WEB_PASSWORD", "")
    if not password:
        return True

    if st.session_state.get("web_authenticated") is True:
        return True

    st.title("Вход")
    entered_password = st.text_input("Пароль", type="password")
    if st.button("Войти", type="primary"):
        if entered_password == password:
            st.session_state["web_authenticated"] = True
            st.rerun()
        st.error("Неверный пароль.")
    return False


def safe_stem(file_name: str) -> str:
    stem = Path(file_name).stem
    stem = re.sub(r"[^\w._-]+", "_", stem).strip("._-")
    return stem or "converted"


def safe_pdf_name(file_name: str) -> str:
    return f"{safe_stem(file_name)}.pdf"


def safe_compressed_image_name(file_name: str) -> str:
    return f"{safe_stem(file_name)}_compressed.jpg"


def safe_compressed_pdf_name(file_name: str) -> str:
    return f"{safe_stem(file_name)}_compressed.pdf"


def safe_archive_output_name(file_name: str) -> str:
    stem = safe_stem(file_name)
    if stem.endswith(".tar"):
        stem = stem[: -len(".tar")] or "archive"
    return f"{stem}_extracted.zip"


def file_extension(file_name: str) -> str:
    return Path(file_name).suffix.lower()


def archive_extension(file_name: str) -> str | None:
    lower_name = file_name.lower()
    for extension in sorted(ARCHIVE_EXTENSIONS, key=len, reverse=True):
        if lower_name.endswith(extension):
            return extension
    return None


def conversion_type(file_name: str) -> str | None:
    extension = file_extension(file_name)
    if extension in DJVU_EXTENSIONS:
        return "djvu"
    if extension in DOCUMENT_EXTENSIONS:
        return "office"
    if extension in IMAGE_EXTENSIONS:
        return "image"
    return None


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def parse_page_ranges(text: str) -> list[int]:
    page_numbers: set[int] = set()
    for raw_part in text.replace(" ", "").split(","):
        if not raw_part:
            continue

        if "-" in raw_part:
            start_text, end_text = raw_part.split("-", 1)
            if not start_text.isdigit() or not end_text.isdigit():
                raise ValueError
            start = int(start_text)
            end = int(end_text)
            if start < 1 or end < start:
                raise ValueError
            page_numbers.update(range(start - 1, end))
        else:
            if not raw_part.isdigit():
                raise ValueError
            page = int(raw_part)
            if page < 1:
                raise ValueError
            page_numbers.add(page - 1)

    if not page_numbers:
        raise ValueError
    return sorted(page_numbers)


def run_async(coro):
    return asyncio.run(coro)


def save_upload(uploaded_file, output_dir: Path, prefix: str = "input") -> Path:
    extension = archive_extension(uploaded_file.name) or file_extension(uploaded_file.name)
    input_path = output_dir / f"{prefix}_{safe_stem(uploaded_file.name)}{extension}"
    input_path.write_bytes(uploaded_file.getbuffer())
    return input_path


def make_zip(paths: list[Path], zip_path: Path) -> Path:
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in paths:
            archive.write(path, arcname=path.name)
    return zip_path


def download_result(path: Path, label: str | None = None) -> None:
    st.download_button(
        label or f"Скачать {path.name}",
        data=path.read_bytes(),
        file_name=path.name,
        mime=mime_type(path),
        type="primary",
    )


def mime_type(path: Path) -> str:
    if path.suffix.lower() == ".pdf":
        return "application/pdf"
    if path.suffix.lower() == ".zip":
        return "application/zip"
    return "application/octet-stream"


async def convert_file_to_pdf(input_path: Path, output_path: Path, kind: str) -> Path:
    if kind == "djvu":
        return await convert_djvu_to_pdf(input_path, output_path)
    if kind == "office":
        return await convert_office_to_pdf(input_path, output_path)
    if kind == "image":
        return await convert_image_to_pdf(input_path, output_path)
    raise ConversionError("Формат файла не поддерживается.")


def render_converter_tab() -> None:
    st.subheader("Файлы в PDF")
    uploaded_files = st.file_uploader(
        "Загрузите DJVU, документ Office, таблицу, презентацию или изображение",
        type=sorted(extension.lstrip(".") for extension in SUPPORTED_EXTENSIONS),
        accept_multiple_files=True,
    )
    if not uploaded_files:
        return

    unsupported = [
        uploaded_file.name
        for uploaded_file in uploaded_files
        if conversion_type(uploaded_file.name) is None
    ]
    if unsupported:
        st.error("Не поддерживается: " + ", ".join(unsupported))
        return

    if len(uploaded_files) > 1:
        output_mode = st.radio(
            "Что сделать с результатами?",
            ["Скачать ZIP с отдельными PDF", "Склеить все в один PDF"],
            horizontal=True,
        )
    else:
        output_mode = "Один PDF"

    if not st.button("Конвертировать", type="primary"):
        return

    with tempfile.TemporaryDirectory(prefix="streamlit-convertor-") as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        progress = st.progress(0)
        status = st.empty()
        output_paths: list[Path] = []

        try:
            for index, uploaded_file in enumerate(uploaded_files, start=1):
                status.write(f"Конвертирую {uploaded_file.name}...")
                input_path = save_upload(uploaded_file, temp_dir, f"input_{index:03d}")
                output_path = temp_dir / f"{index:03d}_{safe_pdf_name(uploaded_file.name)}"
                run_async(
                    convert_file_to_pdf(
                        input_path,
                        output_path,
                        conversion_type(uploaded_file.name) or "",
                    )
                )
                output_paths.append(output_path)
                progress.progress(index / len(uploaded_files))

            status.write("Готово.")
            if output_mode == "Склеить все в один PDF":
                merged_path = temp_dir / "converted_merged.pdf"
                run_async(merge_pdfs(output_paths, merged_path))
                download_result(merged_path, "Скачать общий PDF")
                return

            if len(output_paths) == 1:
                download_result(output_paths[0], "Скачать PDF")
                return

            zip_path = temp_dir / "converted_pdfs.zip"
            make_zip(output_paths, zip_path)
            download_result(zip_path, "Скачать ZIP")
        except (ConversionToolMissingError, ConversionError) as exc:
            st.error(f"Не удалось обработать файл: {exc}")
        except Exception as exc:
            st.exception(exc)


def render_pdf_tools_tab() -> None:
    st.subheader("PDF-инструменты")
    tool = st.radio(
        "Действие",
        [
            "Склеить PDF",
            "Сжать PDF",
            "Вырезать диапазон страниц в новый PDF",
            "Удалить страницы",
            "Разделить все страницы в ZIP",
        ],
    )

    accept_multiple = tool == "Склеить PDF"
    uploaded_files = st.file_uploader(
        "Загрузите PDF",
        type=["pdf"],
        accept_multiple_files=accept_multiple,
        key=f"pdf_tool_{tool}",
    )
    if not uploaded_files:
        return

    files = uploaded_files if isinstance(uploaded_files, list) else [uploaded_files]
    if tool == "Склеить PDF" and len(files) < 2:
        st.info("Для склейки нужно загрузить минимум два PDF.")
        return

    page_range_text = ""
    if tool in {"Вырезать диапазон страниц в новый PDF", "Удалить страницы"}:
        page_range_text = st.text_input("Страницы", placeholder="Например: 1-3,5,8")

    if not st.button("Обработать PDF", type="primary"):
        return

    with tempfile.TemporaryDirectory(prefix="streamlit-pdf-tools-") as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        try:
            input_paths = [
                save_upload(uploaded_file, temp_dir, f"pdf_{index:03d}")
                for index, uploaded_file in enumerate(files, start=1)
            ]

            if tool == "Склеить PDF":
                output_path = temp_dir / "merged.pdf"
                run_async(merge_pdfs(input_paths, output_path))
                download_result(output_path, "Скачать склеенный PDF")
                return

            input_path = input_paths[0]
            base_name = safe_stem(files[0].name)

            if tool == "Сжать PDF":
                output_path = temp_dir / safe_compressed_pdf_name(files[0].name)
                run_async(compress_pdf(input_path, output_path, os.getenv("PDF_COMPRESS_QUALITY", "ebook")))
                download_result(output_path, "Скачать сжатый PDF")
                return

            if tool == "Разделить все страницы в ZIP":
                output_path = temp_dir / f"{base_name}_pages.zip"
                run_async(split_pdf_to_zip(input_path, output_path, base_name))
                download_result(output_path, "Скачать ZIP")
                return

            try:
                pages = parse_page_ranges(page_range_text)
            except ValueError:
                st.error("Не понял страницы. Напишите так: 1-3,5 или 2,4,8.")
                return

            if tool == "Вырезать диапазон страниц в новый PDF":
                output_path = temp_dir / f"{base_name}_pages.pdf"
                run_async(extract_pdf_pages(input_path, output_path, pages))
                download_result(output_path, "Скачать выбранные страницы")
                return

            output_path = temp_dir / f"{base_name}_without_pages.pdf"
            run_async(delete_pdf_pages(input_path, output_path, pages))
            download_result(output_path, "Скачать PDF без выбранных страниц")
        except (ConversionToolMissingError, ConversionError) as exc:
            st.error(f"Не удалось обработать PDF: {exc}")
        except Exception as exc:
            st.exception(exc)


def render_compression_archive_tab() -> None:
    st.subheader("Сжатие и архивы")
    tool = st.radio(
        "Действие",
        ["Сжать изображение", "Разархивировать ZIP/TAR/7Z/RAR"],
    )

    if tool == "Сжать изображение":
        uploaded_file = st.file_uploader(
            "Загрузите изображение",
            type=sorted(extension.lstrip(".") for extension in IMAGE_EXTENSIONS),
            key="compress_image_upload",
        )
        quality = st.slider(
            "Качество JPEG",
            min_value=1,
            max_value=95,
            value=env_int("IMAGE_COMPRESS_QUALITY", 75),
        )
        max_side = st.number_input(
            "Максимальная сторона, px",
            min_value=0,
            max_value=10000,
            value=env_int("IMAGE_COMPRESS_MAX_SIDE", 1600),
            step=100,
        )
        if uploaded_file is None or not st.button("Сжать изображение", type="primary"):
            return

        with tempfile.TemporaryDirectory(prefix="streamlit-image-compress-") as temp_dir_name:
            temp_dir = Path(temp_dir_name)
            input_path = save_upload(uploaded_file, temp_dir)
            output_path = temp_dir / safe_compressed_image_name(uploaded_file.name)
            try:
                run_async(compress_image(input_path, output_path, quality, int(max_side)))
                download_result(output_path, "Скачать сжатое изображение")
            except (ConversionToolMissingError, ConversionError) as exc:
                st.error(f"Не удалось сжать изображение: {exc}")
            except Exception as exc:
                st.exception(exc)
        return

    uploaded_file = st.file_uploader(
        "Загрузите ZIP/TAR/7Z/RAR-архив",
        type=["zip", "tar", "gz", "tgz", "bz2", "xz", "7z", "rar"],
        key="extract_archive_upload",
    )
    max_output_mb = st.number_input(
        "Максимальный распакованный объем, МБ",
        min_value=1,
        max_value=5000,
        value=env_int("ARCHIVE_MAX_OUTPUT_MB", 500),
        step=50,
    )
    max_files = st.number_input(
        "Максимальное количество файлов",
        min_value=1,
        max_value=10000,
        value=env_int("ARCHIVE_MAX_FILES", 200),
        step=10,
    )
    if uploaded_file is None or not st.button("Разархивировать", type="primary"):
        return

    if archive_extension(uploaded_file.name) is None:
        st.error(
            "Поддерживаются только архивы: .zip, .tar, .tar.gz, .tgz, "
            ".tar.bz2, .tar.xz, .7z, .rar."
        )
        return

    with tempfile.TemporaryDirectory(prefix="streamlit-archive-extract-") as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        input_path = save_upload(uploaded_file, temp_dir)
        output_path = temp_dir / safe_archive_output_name(uploaded_file.name)
        try:
            file_count, total_bytes = run_async(
                extract_archive_to_zip(
                    input_path,
                    output_path,
                    int(max_output_mb) * 1024 * 1024,
                    int(max_files),
                )
            )
            st.success(
                f"Готово. Файлов: {file_count}, объем внутри: {total_bytes / (1024 * 1024):.1f} МБ."
            )
            download_result(output_path, "Скачать распакованные файлы ZIP")
        except (ConversionToolMissingError, ConversionError) as exc:
            st.error(f"Не удалось разархивировать: {exc}")
        except Exception as exc:
            st.exception(exc)


def main() -> None:
    st.set_page_config(
        page_title="Convertor",
        page_icon=":page_facing_up:",
        layout="wide",
    )
    if not check_password():
        return

    st.title("Конвертер файлов")
    st.caption("DJVU, документы, изображения, сжатие, архивы и PDF-инструменты без Telegram.")

    converter_tab, pdf_tools_tab, compression_tab = st.tabs(
        ["Конвертация в PDF", "PDF-инструменты", "Сжатие и архивы"]
    )
    with converter_tab:
        render_converter_tab()
    with pdf_tools_tab:
        render_pdf_tools_tab()
    with compression_tab:
        render_compression_archive_tab()


if __name__ == "__main__":
    main()
