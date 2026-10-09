"""Pull the text out of lecture slides (PDFs or PowerPoints, one or more) to give the notes AI more context."""

from pathlib import Path

from . import documents
from .utils import AppError

MAX_CHARS = 40_000
OCR_PAGES = 20  # slides only add context, so read fewer scanned pages than for a document
SUFFIXES = {".pdf", ".pptx"}
DECK_SEPARATOR = "\f"  # between decks, so each can get its share of the room in a prompt


def extract_all(paths: list[Path], progress=lambda _: None, warn=lambda _: None) -> str:
    """The text of every deck, each headed with its file name. A deck that can't be read is skipped
    with a warning; the others (and the recording) still make the notes."""
    decks = []
    for path in paths:
        name = deck_name(path)
        if len(paths) > 1:
            progress(f"Reading the slides… {name}")
        try:
            text = extract_text(path, progress, ocr_pages=max(1, OCR_PAGES // len(paths)))
        except AppError as e:
            warn(f"Couldn't read the slides “{name}”, so they were left out." if len(paths) > 1 else str(e))
            continue
        decks.append(f"[Slides: {name}]\n" + text[:MAX_CHARS // len(paths)])
    return DECK_SEPARATOR.join(decks)


def deck_name(path: Path) -> str:
    """The original file name ("slides_2_Week 3 - Induction.pdf" was saved as the 2nd file)."""
    stem = path.stem.split("_", 2)[-1] if path.stem.startswith("slides_") else path.stem
    return stem + path.suffix


def extract_text(path: Path, progress=lambda _: None, ocr_pages: int = OCR_PAGES) -> str:
    if path.suffix.lower() not in SUFFIXES:
        raise AppError("Slides must be a PDF or PowerPoint (.pptx) file.")
    try:
        pages = documents.read(path, progress, ocr_pages=ocr_pages)
    except AppError:
        raise AppError("Couldn't read the slides file, so the notes were made from the recording only.")

    text = "\n\n".join(f"[Slide {i}]\n{t.strip()}" for i, t in enumerate(pages[:200], 1) if t.strip())
    if not text:
        raise AppError("Couldn't find any text in the slides, so the notes were made from the recording only.")
    return text[:MAX_CHARS]
