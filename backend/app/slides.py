"""Pull the text out of lecture slides (PDF or PowerPoint) to give the notes AI more context."""

from pathlib import Path

from . import documents
from .utils import AppError

MAX_CHARS = 40_000
SUFFIXES = {".pdf", ".pptx"}


def extract_text(path: Path) -> str:
    if path.suffix.lower() not in SUFFIXES:
        raise AppError("Slides must be a PDF or PowerPoint (.pptx) file.")
    try:
        pages = documents.read(path)
    except AppError:
        raise AppError("Couldn't read the slides file, so the notes were made from the recording only.")

    text = "\n\n".join(f"[Slide {i}]\n{t.strip()}" for i, t in enumerate(pages[:200], 1) if t.strip())
    if not text:
        raise AppError("The slides had no readable text (maybe they're images), so the notes "
                       "were made from the recording only.")
    return text[:MAX_CHARS]
