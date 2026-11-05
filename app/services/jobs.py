"""What each job kind does. A handler downloads, converts and returns outputs;
the queue takes care of OCR, sending, errors, refunds and cleanup."""

from __future__ import annotations

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from app import texts
from app.converters import ConversionError, archives, djvu, images, office, pdf
from app.formats import FileKind, output_name, safe_send_name, safe_stem
from app.models import IncomingFile, JobResult, Output
from app.services.queue import Handler, JobContext


async def _to_pdf(ctx: JobContext, file: IncomingFile, source: Path, target: Path) -> Path:
    s = ctx.settings
    if file.kind is FileKind.PDF:
        source.replace(target)
        return target
    if file.kind is FileKind.DJVU:
        return await djvu.to_pdf(source, target, quality=s.djvu_pdf_quality,
                                 keep_resolution=s.djvu_keep_resolution, workers=s.djvu_workers,
                                 timeout=s.tool_timeout)
    if file.kind is FileKind.OFFICE:
        return await office.to_pdf(source, target, timeout=s.tool_timeout)
    if file.kind is FileKind.IMAGE:
        return await images.to_pdf(source, target)
    raise ConversionError(f"Не умею переводить в PDF: {file.name}")


async def convert(ctx: JobContext) -> JobResult:
    """payload: files, mode = each | merge"""
    files = ctx.job.files
    mode = ctx.job.payload.get("mode", "each")
    pdfs: list[tuple[Path, str]] = []
    for number, file in enumerate(files, start=1):
        counter = f" ({number}/{len(files)})" if len(files) > 1 else ""
        await ctx.progress(texts.STAGE_DOWNLOADING + counter)
        source = await ctx.download(file, prefix=f"{number:03d}_")
        await ctx.progress(texts.STAGE_CONVERTING + counter)
        target = ctx.workdir / f"{number:03d}_{output_name(file.name)}"
        pdfs.append((await _to_pdf(ctx, file, source, target), output_name(file.name)))

    if mode == "merge" and len(pdfs) > 1:
        await ctx.progress(texts.STAGE_MERGING)
        merged = await pdf.merge([p for p, _ in pdfs], ctx.workdir / "merged.pdf")
        return JobResult([Output(merged, "merged.pdf", texts.DONE_MERGED)])
    return JobResult([Output(path, name, texts.DONE_CONVERTED) for path, name in pdfs])


async def compress_images(ctx: JobContext) -> JobResult:
    s = ctx.settings
    results: list[Path] = []
    files = ctx.job.files
    for number, file in enumerate(files, start=1):
        await ctx.progress(f"{texts.STAGE_COMPRESSING} ({number}/{len(files)})")
        source = await ctx.download(file, prefix=f"{number:03d}_")
        target = ctx.workdir / f"{number:03d}_{output_name(file.name, '_small', '.jpg')}"
        results.append(await images.compress(source, target, quality=s.image_compress_quality,
                                             max_side=s.image_compress_max_side))
    if len(results) == 1:
        name = output_name(files[0].name, "_small", ".jpg")
        return JobResult([Output(results[0], name, texts.done_compressed(files[0].size, results[0]))])

    zip_path = ctx.workdir / "compressed_images.zip"
    with ZipFile(zip_path, "w", compression=ZIP_DEFLATED) as archive:
        for path in results:
            archive.write(path, path.name.split("_", 1)[1] if "_" in path.name else path.name)
    return JobResult([Output(zip_path, zip_path.name, texts.DONE_IMAGES_ZIP)])


async def pdf_merge(ctx: JobContext) -> JobResult:
    ctx.job.payload["mode"] = "merge"
    return await convert(ctx)


async def pdf_pages(ctx: JobContext) -> JobResult:
    """payload: files=[pdf], pages=[zero-based], delete=bool"""
    file = ctx.job.files[0]
    delete = bool(ctx.job.payload.get("delete"))
    await ctx.progress(texts.STAGE_DOWNLOADING)
    source = await ctx.download(file)
    await ctx.progress(texts.STAGE_PROCESSING)
    suffix = "_without_pages" if delete else "_pages"
    target = ctx.workdir / output_name(file.name, suffix)
    await pdf.select_pages(source, target, ctx.job.payload["pages"], delete=delete)
    caption = texts.DONE_PAGES_DELETED if delete else texts.DONE_PAGES_EXTRACTED
    return JobResult([Output(target, target.name, caption)])


async def pdf_split_all(ctx: JobContext) -> JobResult:
    file = ctx.job.files[0]
    await ctx.progress(texts.STAGE_DOWNLOADING)
    source = await ctx.download(file)
    await ctx.progress(texts.STAGE_PROCESSING)
    limit = ctx.settings.effective_max_output_mb * 1024 * 1024
    zips = await pdf.split_pages_to_zips(source, ctx.workdir / "zips", safe_stem(file.name), limit)
    return JobResult([
        Output(z, z.name, texts.split_caption(n, len(zips)), ocr=False) for n, z in enumerate(zips, start=1)
    ])


async def pdf_compress(ctx: JobContext) -> JobResult:
    file = ctx.job.files[0]
    await ctx.progress(texts.STAGE_DOWNLOADING)
    source = await ctx.download(file)
    await ctx.progress(texts.STAGE_COMPRESSING)
    target = ctx.workdir / output_name(file.name, "_compressed")
    await pdf.compress(source, target, ctx.settings.pdf_compress_quality, ctx.settings.tool_timeout)
    return JobResult([Output(target, target.name, texts.done_compressed(source.stat().st_size, target))])


async def archive_list(ctx: JobContext) -> JobResult:
    """Read the file list and show it as buttons (the choice is kept in FSM data)."""
    from app.bot import keyboards

    file = ctx.job.files[0]
    await ctx.progress(texts.STAGE_DOWNLOADING)
    source = await ctx.download(file)
    await ctx.progress(texts.STAGE_READING_ARCHIVE)
    entries = await archives.list_files(source, ctx.settings.archive_max_files)
    names = [name for name, _ in entries]
    await ctx.state().update_data(archive={"file": file.to_dict(), "entries": names})
    return JobResult(text=texts.archive_pick(len(names)), reply_markup=keyboards.archive_entries(names, 0))


async def archive_extract(ctx: JobContext) -> JobResult:
    """payload: files=[archive], wanted=[names] or None. Files are sent as they are (no OCR)."""
    s = ctx.settings
    file = ctx.job.files[0]
    wanted = ctx.job.payload.get("wanted")
    await ctx.progress(texts.STAGE_DOWNLOADING)
    source = await ctx.download(file)
    await ctx.progress(texts.STAGE_EXTRACTING)
    out_dir = ctx.workdir / "extracted"
    paths = await archives.extract(source, out_dir, max_bytes=s.archive_max_output_mb * 1024 * 1024,
                                   max_files=s.archive_max_files, wanted=set(wanted) if wanted else None)
    total = len(paths)
    return JobResult([
        Output(p, safe_send_name(p.relative_to(out_dir).as_posix()),
               texts.file_of(n, total) if total > 1 else "", ocr=False)
        for n, p in enumerate(paths, start=1)
    ])


HANDLERS: dict[str, Handler] = {
    "convert": convert,
    "compress_images": compress_images,
    "pdf_merge": pdf_merge,
    "pdf_pages": pdf_pages,
    "pdf_split_all": pdf_split_all,
    "pdf_compress": pdf_compress,
    "archive_list": archive_list,
    "archive_extract": archive_extract,
}
