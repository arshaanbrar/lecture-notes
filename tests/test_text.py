from backend.app.utils import split_text


def test_split_text_respects_limit_and_keeps_all_text():
    text = ("This is a sentence. " * 400) + "x" * 5000
    pieces = split_text(text, 2000)
    assert all(len(p) <= 2000 for p in pieces)
    assert "".join(pieces).replace(" ", "") == text.replace(" ", "")


def test_split_text_prefers_sentence_boundaries():
    pieces = split_text("First sentence here. Second one is a bit longer.", 30)
    assert pieces[0] == "First sentence here."
