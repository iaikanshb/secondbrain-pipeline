"""Per-page branch: direct text-layer extraction (lossless, free, instant)
vs. vision OCR (only for pages with no real text layer -- scans/handwriting).

Doing this per-page, not per-document, matters: a lecture PDF can mix a
typed cover page with photographed handwritten pages.
"""
from dataclasses import dataclass, field

import pymupdf

import config
import ocr


@dataclass
class ExtractResult:
    text: str
    page_count: int
    pages: list = field(default_factory=list)  # per-page text, 0-indexed by page-1
    pages_extracted_directly: int = 0
    pages_ocred: int = 0
    pages_skipped: int = 0
    review_flags: list = field(default_factory=list)  # 1-indexed page numbers


def get_page_count(path: str) -> int:
    return len(pymupdf.open(path))


def extract_pdf(path: str, skip_ocr: bool = False) -> ExtractResult:
    """skip_ocr=True: pages with no text layer are skipped rather than sent
    to vision OCR -- used for textbook-sized PDFs, where a handful of
    diagram-only pages aren't worth a vision call each and losslessness
    matters far less than for lecture material actually being studied from."""
    doc = pymupdf.open(path)
    parts = []
    result = ExtractResult(text="", page_count=len(doc))

    for i, page in enumerate(doc, start=1):
        layer_text = page.get_text().strip()

        if len(layer_text) >= config.TEXT_LAYER_MIN_CHARS:
            parts.append(f"--- page {i} (text layer) ---\n{layer_text}")
            result.pages.append(layer_text)
            result.pages_extracted_directly += 1
            continue

        if skip_ocr:
            result.pages.append("")
            result.pages_skipped += 1
            continue

        pix = page.get_pixmap(dpi=250)
        img_bytes = pix.tobytes("png")
        page_result = ocr.transcribe_and_verify(img_bytes)
        parts.append(f"--- page {i} (vision OCR) ---\n{page_result.text}")
        result.pages.append(page_result.text)
        result.pages_ocred += 1
        if page_result.needed_correction or page_result.has_illegible:
            result.review_flags.append(i)

    result.text = "\n\n".join(parts)
    return result
