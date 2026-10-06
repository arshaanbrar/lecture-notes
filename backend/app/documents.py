"""Read the text out of documents (PDF, Word, PowerPoint, plain text, photos) so they can be
summarised like a lecture, or used as slides to help the notes for a recording.

Scanned PDFs and photos have no text in them, only pictures of text. Those pages are read with
Tesseract OCR (free and open source, runs on this server): pdftoppm turns each page into an image
and tesseract reads it.
"""

import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from .utils import AppError

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg"}
SUFFIXES = {".pdf", ".docx", ".pptx", ".txt", ".md"} | IMAGE_SUFFIXES
MAX_CHARS = 300_000  # ~100 pages; longer documents are cut off with a warning
OCR_MAX_PAGES = 40   # OCR takes several seconds a page on a small free server
OCR_DPI = 200
OCR_TIMEOUT = 180    # seconds per page
MIN_PAGE_CHARS = 25  # a page with less text than this is probably a scan
WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def is_document(path: Path | str) -> bool:
    return Path(path).suffix.lower() in SUFFIXES


def ocr_available() -> bool:
    return bool(shutil.which("tesseract") and shutil.which("pdftoppm"))


def _ocr_image(image: Path) -> str:
    try:
        done = subprocess.run(["tesseract", str(image), "stdout"], capture_output=True, text=True,
                              timeout=OCR_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return done.stdout if done.returncode == 0 else ""


def _ocr_pdf_page(path: Path, number: int, workdir: Path) -> str:
    """Render one page (1-based) to an image and read it."""
    prefix = workdir / f"page{number}"
    try:
        subprocess.run(["pdftoppm", "-r", str(OCR_DPI), "-gray", "-png", "-singlefile",
                        "-f", str(number), "-l", str(number), str(path), str(prefix)],
                       capture_output=True, timeout=OCR_TIMEOUT, check=True)
    except (OSError, subprocess.SubprocessError):
        return ""
    image = prefix.with_suffix(".png")
    try:
        return _ocr_image(image)
    finally:
        image.unlink(missing_ok=True)


def _ocr_missing_pages(path: Path, pages: list[str], progress, warn, max_pages: int) -> list[str]:
    """Fill in the pages that have (almost) no text by reading them with OCR."""
    missing = [i for i, text in enumerate(pages) if len(text.strip()) < MIN_PAGE_CHARS]
    if not missing or not ocr_available():
        return pages
    if len(missing) > max_pages:
        warn(f"The document has {len(missing)} scanned pages; only the first {max_pages} were read.")
        missing = missing[:max_pages]
    pages = list(pages)
    with tempfile.TemporaryDirectory(prefix="ocr-") as tmp:
        for n, i in enumerate(missing, 1):
            progress(f"Reading scanned page {n} of {len(missing)}…")
            text = _ocr_pdf_page(path, i + 1, Path(tmp))
            if len(text.strip()) > len(pages[i].strip()):
                pages[i] = text
    return pages


def read(path: Path, progress=lambda _: None, warn=lambda _: None, ocr_pages: int | None = None) -> list[str]:
    """The text of each page/slide (one item for Word and text files). Raises AppError if unreadable.
    Scanned PDF pages and photos are read with OCR (up to `ocr_pages` pages, default OCR_MAX_PAGES;
    0 turns it off)."""
    ocr_pages = OCR_MAX_PAGES if ocr_pages is None else ocr_pages
    suffix = path.suffix.lower()
    if suffix not in SUFFIXES:
        raise AppError("That file type can't be read. Use a PDF, Word (.docx), PowerPoint (.pptx), "
                       "text file or a photo (.jpg/.png).")
    if suffix in IMAGE_SUFFIXES:
        if not shutil.which("tesseract"):
            raise AppError("Reading text from photos isn't set up on this server.")
        progress("Reading the text in the photo…")
        return [_ocr_image(path)]
    try:
        if suffix == ".pdf":
            from pypdf import PdfReader
            pages = [page.extract_text() or "" for page in PdfReader(str(path)).pages[:500]]
        elif suffix == ".pptx":
            from pptx import Presentation
            pages = []
            for slide in Presentation(str(path)).slides:
                texts = [shape.text_frame.text for shape in slide.shapes if shape.has_text_frame]
                pages.append("\n".join(t for t in texts if t.strip()))
            return pages
        elif suffix == ".docx":
            with zipfile.ZipFile(path) as z:
                root = ElementTree.fromstring(z.read("word/document.xml"))
            paragraphs = ["".join(t.text or "" for t in p.iter(f"{WORD_NS}t")) for p in root.iter(f"{WORD_NS}p")]
            return ["\n".join(p for p in paragraphs if p.strip())]
        else:  # .txt / .md
            return [path.read_bytes().decode("utf-8", errors="replace")]
    except Exception:
        raise AppError("Couldn't open that document. It may be damaged or password-protected.")
    # A PDF: scanned pages have no text layer, so read those with OCR.
    return _ocr_missing_pages(path, pages, progress, warn, ocr_pages) if ocr_pages else pages


def full_text(path: Path, warn=lambda _: None, progress=lambda _: None) -> str:
    """The whole document as one text, for making notes from it."""
    pages = [p.strip() for p in read(path, progress, warn) if p.strip()]
    text = re.sub(r"\n{3,}", "\n\n", "\n\n".join(pages))
    if not text.strip():
        raise AppError("Couldn't find any text in the document, even by reading it as a scan. "
                       "Check it isn't blank, blurry or upside down.")
    if len(text) > MAX_CHARS:
        warn(f"The document is very long, so only the first ~{MAX_CHARS // 3000} pages were used.")
        text = text[:MAX_CHARS]
    return text
