"""OCR for scanned PDF pages with the engine built into the system: Windows.Media.Ocr on
Windows, Apple Vision on macOS.

A machine that uses another PC's models (e.g. a Mac) sends scanned pages to that PC instead:
Windows OCR there takes ~0.1 s per page against ~2.4 s for Apple Vision on an Intel Mac.
If the PC can't do it (off, older version), pages fall back to the local engine.
"""

from __future__ import annotations

import asyncio
import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import cache

import numpy as np
import pymupdf

DPI = 200
VISION_LANGUAGES = ["en-US", "es-ES"]
REMOTE_BATCH = 4  # pages per request to the PC
REMOTE_RETRY_S = 300  # after the PC fails, use local OCR for this long before trying again

# Set by the server on a remote machine: PNG images -> text lines per image (see use_remote).
_remote: Callable[[list[bytes]], list[list[str]]] | None = None
_remote_off_until = 0.0


def use_remote(fn: Callable[[list[bytes]], list[list[str]]] | None) -> None:
    global _remote
    _remote = fn


@cache
def _engine():
    try:
        from winrt.windows.globalization import Language
        from winrt.windows.media.ocr import OcrEngine

        return OcrEngine.try_create_from_language(Language("en-US")) or \
            OcrEngine.try_create_from_user_profile_languages()
    except Exception:
        return None


@cache
def _vision():
    try:
        import Vision

        return Vision
    except Exception:
        return None


def available() -> bool:
    if sys.platform == "darwin":
        return _vision() is not None
    return _engine() is not None


def _gray(pix: pymupdf.Pixmap) -> np.ndarray:
    if pix.n != 1 or pix.alpha:
        pix = pymupdf.Pixmap(pymupdf.csGRAY, pix)
    return np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.stride)[:, : pix.width]


async def _ocr_gray(arr: np.ndarray) -> list[str]:
    from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap

    h, w = arr.shape
    bmp = SoftwareBitmap.create_copy_from_buffer(
        np.ascontiguousarray(arr).tobytes(), BitmapPixelFormat.GRAY8, w, h
    )
    result = await _engine().recognize_async(bmp)
    return [line.text for line in result.lines]


async def _ocr_page(page: pymupdf.Page) -> list[str]:
    return await _ocr_gray(_gray(page.get_pixmap(dpi=DPI, colorspace=pymupdf.csGRAY)))


def ocr_images(pngs: list[bytes]) -> list[list[str]]:
    """Windows OCR of page images sent by another machine (the gateway's /v1/ocr)."""
    async def run():
        return [await _ocr_gray(_gray(pymupdf.Pixmap(png))) for png in pngs]

    return asyncio.run(run())


def _png(page: pymupdf.Page) -> bytes:
    return page.get_pixmap(dpi=DPI, colorspace=pymupdf.csGRAY).tobytes("png")


def _vision_page(page: pymupdf.Page) -> list[str]:
    import objc
    from Foundation import NSData

    vision = _vision()
    png = page.get_pixmap(dpi=DPI, colorspace=pymupdf.csGRAY).tobytes("png")
    # Indexing runs on a worker thread, which has no autorelease pool of its own.
    with objc.autorelease_pool():
        req = vision.VNRecognizeTextRequest.alloc().init()
        req.setRecognitionLevel_(vision.VNRequestTextRecognitionLevelAccurate)
        req.setRecognitionLanguages_(VISION_LANGUAGES)
        req.setUsesLanguageCorrection_(True)
        handler = vision.VNImageRequestHandler.alloc().initWithData_options_(
            NSData.dataWithBytes_length_(png, len(png)), None)
        ok, _ = handler.performRequests_error_([req], None)
        if not ok:
            return []
        return [str(obs.topCandidates_(1)[0].string()) for obs in req.results() or []]


def ocr_pages(doc: pymupdf.Document, pages: list[int],
              progress=lambda done, total: None) -> dict[int, list[str]]:
    """OCR the given 0-based pages; returns page -> list of text lines.

    progress(done, total) is called after each page (OCR can take minutes per file)."""
    if not pages:
        return {}
    out: dict[int, list[str]] = {}
    if _remote and time.time() >= _remote_off_until:
        _ocr_remote(doc, pages, progress, out)
    rest = [p for p in pages if p not in out]
    if rest and available():
        _ocr_local(doc, rest, progress, out, len(pages))
    return out


def _ocr_remote(doc: pymupdf.Document, pages: list[int], progress, out: dict) -> None:
    """Send pages to the PC in small batches, rendering the next batch while it reads one."""
    global _remote_off_until
    batches = [pages[i:i + REMOTE_BATCH] for i in range(0, len(pages), REMOTE_BATCH)]
    try:
        with ThreadPoolExecutor(1) as pool:
            pending = None  # (pages, future)
            for batch in batches + [None]:
                pngs = [_png(doc[p]) for p in batch] if batch else None
                if pending:
                    done_pages, fut = pending
                    out.update(zip(done_pages, fut.result()))
                    progress(len(out), len(pages))
                pending = (batch, pool.submit(_remote, pngs)) if batch else None
    except Exception as e:  # PC off / older version: finish the file with the local engine
        _remote_off_until = time.time() + REMOTE_RETRY_S
        print(f"OCR on the PC unavailable ({type(e).__name__}: {e}); using local OCR",
              file=sys.stderr)


def _ocr_local(doc: pymupdf.Document, pages: list[int], progress, out: dict, total: int) -> None:
    if sys.platform == "darwin":
        for p in pages:
            out[p] = _vision_page(doc[p])
            progress(len(out), total)
        return

    async def run():
        for p in pages:
            out[p] = await _ocr_page(doc[p])
            progress(len(out), total)

    asyncio.run(run())
