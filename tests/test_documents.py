import zipfile

import pytest

from backend.app import documents
from backend.app.utils import AppError
from tests.test_slides import make_pdf


def make_docx(path, *paragraphs):
    """A minimal Word file with the given paragraphs."""
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", f'<w:document xmlns:w="{ns}"><w:body>{body}</w:body></w:document>')


def test_reads_word_pdf_and_text(tmp_path):
    make_docx(tmp_path / "reading.docx", "Durkheim on social facts", "Anomie explained")
    assert documents.full_text(tmp_path / "reading.docx") == "Durkheim on social facts\nAnomie explained"
    make_pdf(tmp_path / "handout.pdf", "Strong induction")
    assert "Strong induction" in documents.full_text(tmp_path / "handout.pdf")
    (tmp_path / "notes.txt").write_text("Plain notes", encoding="utf-8")
    assert documents.full_text(tmp_path / "notes.txt") == "Plain notes"


def test_documents_are_told_apart_from_recordings():
    assert documents.is_document("Week 3.PDF") and documents.is_document("essay.docx")
    assert not documents.is_document("lecture.m4a") and not documents.is_document("recording.webm")


def test_bad_or_empty_documents_give_friendly_errors(tmp_path):
    (tmp_path / "broken.docx").write_bytes(b"not a zip")
    with pytest.raises(AppError, match="Couldn't open"):
        documents.full_text(tmp_path / "broken.docx")
    (tmp_path / "blank.txt").write_text("   ")
    with pytest.raises(AppError, match="Couldn't find any text"):
        documents.full_text(tmp_path / "blank.txt")


def test_very_long_documents_are_cut_with_a_warning(tmp_path):
    (tmp_path / "book.txt").write_text("word " * 100_000)
    warnings = []
    text = documents.full_text(tmp_path / "book.txt", warnings.append)
    assert len(text) == documents.MAX_CHARS and "very long" in warnings[0]


needs_ocr = pytest.mark.skipif(not documents.ocr_available(), reason="tesseract/pdftoppm not installed")


def make_scan(path, *lines):
    """A 'scanned' PDF: each page is only a picture of text, with no text layer."""
    from PIL import Image, ImageDraw, ImageFont
    font = ImageFont.load_default(size=40)
    pages = []
    for line in lines:
        img = Image.new("RGB", (1240, 1754), "white")
        ImageDraw.Draw(img).text((100, 200), line, fill="black", font=font)
        pages.append(img)
    pages[0].save(path, save_all=True, append_images=pages[1:], resolution=150)
    return pages


@needs_ocr
def test_scanned_pdfs_and_photos_are_read_with_ocr(tmp_path):
    pages = make_scan(tmp_path / "scan.pdf", "Week 4: Durkheim and anomie", "Social facts shape how people act")
    progress = []
    text = documents.full_text(tmp_path / "scan.pdf", progress=progress.append)
    assert "Durkheim and anomie" in text and "Social facts shape" in text
    assert progress == ["Reading scanned page 1 of 2…", "Reading scanned page 2 of 2…"]
    pages[1].save(tmp_path / "photo.jpg")
    assert "Social facts shape" in documents.full_text(tmp_path / "photo.jpg")


@needs_ocr
def test_only_so_many_scanned_pages_are_read(tmp_path, monkeypatch):
    monkeypatch.setattr(documents, "OCR_MAX_PAGES", 1)
    make_scan(tmp_path / "scan.pdf", "First page here", "Second page here")
    warnings = []
    text = documents.full_text(tmp_path / "scan.pdf", warnings.append)
    assert "First page" in text and "Second page" not in text
    assert "2 scanned pages; only the first 1" in warnings[0]


def test_scans_without_ocr_installed_give_a_friendly_error(tmp_path, monkeypatch):
    monkeypatch.setattr(documents, "ocr_available", lambda: False)
    make_scan(tmp_path / "scan.pdf", "Hidden text")
    with pytest.raises(AppError, match="Couldn't find any text"):
        documents.full_text(tmp_path / "scan.pdf")
