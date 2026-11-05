import asyncio
import io
import tarfile
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from app.converters import ConversionError, archives, images, pdf
from app.converters.process import has_tool
from app.formats import FileKind, compact_ranges, detect_kind, output_name, parse_page_ranges


def run(coro):
    return asyncio.run(coro)


def make_pdf(path: Path, pages: int) -> Path:
    frames = [Image.new("RGB", (200, 300), (i * 40 % 255, 100, 100)) for i in range(pages)]
    frames[0].save(path, "PDF", save_all=True, append_images=frames[1:])
    return path


def test_formats():
    assert detect_kind("a.DJVU") is FileKind.DJVU
    assert detect_kind("x.tar.gz") is FileKind.ARCHIVE
    assert detect_kind("doc.docx") is FileKind.OFFICE
    assert detect_kind("scan.pdf") is FileKind.PDF
    assert detect_kind("a.exe") is None
    assert output_name("Лекция 1.docx") == "Лекция_1.pdf"
    assert output_name("x.tar.gz", ext=".zip") == "x.zip"
    assert parse_page_ranges("1-3, 5") == [0, 1, 2, 4]
    assert compact_ranges([0, 1, 2, 4]) == "1-3,5"
    for bad in ("", "0", "3-1", "a", "1-b"):
        with pytest.raises(ValueError):
            parse_page_ranges(bad)


def test_pdf_ops(tmp_path):
    a = make_pdf(tmp_path / "a.pdf", 3)
    b = make_pdf(tmp_path / "b.pdf", 2)
    merged = run(pdf.merge([a, b], tmp_path / "m.pdf"))
    assert run(pdf.page_count(merged)) == 5

    picked = run(pdf.select_pages(merged, tmp_path / "p.pdf", [0, 4], delete=False))
    assert run(pdf.page_count(picked)) == 2
    rest = run(pdf.select_pages(merged, tmp_path / "d.pdf", [0, 4], delete=True))
    assert run(pdf.page_count(rest)) == 3
    with pytest.raises(ConversionError):
        run(pdf.select_pages(merged, tmp_path / "x.pdf", [10], delete=False))
    with pytest.raises(ConversionError):
        run(pdf.select_pages(merged, tmp_path / "x.pdf", [0, 1, 2, 3, 4], delete=True))

    parts = run(pdf.split_by_size(merged, tmp_path / "parts", "m", max_bytes=merged.stat().st_size // 2))
    assert len(parts) >= 2
    assert sum(run(pdf.page_count(p)) for p in parts) == 5

    zips = run(pdf.split_pages_to_zips(merged, tmp_path / "z", "m", max_bytes=10**9))
    assert len(zips) == 1
    assert len(zipfile.ZipFile(zips[0]).namelist()) == 5


def test_images(tmp_path):
    src = tmp_path / "photo.png"
    Image.new("RGBA", (3000, 1000), (255, 0, 0, 128)).save(src)
    out = run(images.to_pdf(src, tmp_path / "photo.pdf"))
    assert run(pdf.page_count(out)) == 1
    small = run(images.compress(src, tmp_path / "small.jpg", quality=70, max_side=1600))
    assert max(Image.open(small).size) == 1600
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"not an image")
    with pytest.raises(ConversionError):
        run(images.to_pdf(bad, tmp_path / "bad.pdf"))


def test_zip_archive(tmp_path):
    src = tmp_path / "a.zip"
    with zipfile.ZipFile(src, "w") as z:
        z.writestr("docs/one.txt", "1")
        z.writestr("two.txt", "22")
        z.writestr("../evil.txt", "x")  # must be ignored
    files = run(archives.list_files(src, max_files=10))
    assert sorted(n for n, _ in files) == ["docs/one.txt", "two.txt"]

    out = run(archives.extract(src, tmp_path / "out", max_bytes=10**6, max_files=10))
    assert sorted(p.name for p in out) == ["one.txt", "two.txt"]
    assert not (tmp_path / "evil.txt").exists()

    one = run(archives.extract(src, tmp_path / "out2", max_bytes=10**6, max_files=10, wanted={"two.txt"}))
    assert [p.read_text() for p in one] == ["22"]

    with pytest.raises(ConversionError):
        run(archives.list_files(src, max_files=1))
    with pytest.raises(ConversionError):
        run(archives.extract(src, tmp_path / "out3", max_bytes=2, max_files=10))


def test_tar_archive(tmp_path):
    src = tmp_path / "a.tar.gz"
    with tarfile.open(src, "w:gz") as t:
        data = b"hello"
        info = tarfile.TarInfo("folder/hello.txt")
        info.size = len(data)
        t.addfile(info, io.BytesIO(data))
    out = run(archives.extract(src, tmp_path / "out", max_bytes=10**6, max_files=10))
    assert out[0].read_bytes() == b"hello"


@pytest.mark.skipif(not has_tool("soffice"), reason="LibreOffice not installed")
def test_office(tmp_path):
    from app.converters import office

    src = tmp_path / "note.txt"
    src.write_text("Привет, мир", encoding="utf-8")
    out = run(office.to_pdf(src, tmp_path / "note.pdf", timeout=180))
    assert run(pdf.page_count(out)) == 1
