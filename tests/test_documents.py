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
    with pytest.raises(AppError, match="no readable text"):
        documents.full_text(tmp_path / "blank.txt")


def test_very_long_documents_are_cut_with_a_warning(tmp_path):
    (tmp_path / "book.txt").write_text("word " * 100_000)
    warnings = []
    text = documents.full_text(tmp_path / "book.txt", warnings.append)
    assert len(text) == documents.MAX_CHARS and "very long" in warnings[0]
