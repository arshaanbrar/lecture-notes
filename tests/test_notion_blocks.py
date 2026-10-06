from backend.app import notion

NOTES = {
    "summary": "s", "key_points": ["k"], "key_terms": [{"term": "Base case", "definition": "P(1)."}],
    "action_items": ["Read 5.1"],
    "explanations": [{"topic": "Induction", "explanation": "Dominoes."}], "cheat_sheet": ["P(1)"],
    "practice_questions": [{"q": "Two steps?", "a": "Base + step."}],
    "quiz": [{"question": "First?", "options": ["a", "b", "c", "d"], "answer": 1, "explanation": "b"}],
    "flashcards": [{"front": "Base case", "back": "P(1)"}],
}


def headings(blocks):
    return [b["heading_2"]["rich_text"][0]["text"]["content"] for b in blocks if b["type"] == "heading_2"]


def test_sections_are_in_the_right_order():
    assert headings(notion.build_blocks(NOTES)) == [
        "Summary", "Key points", "Key terms", "Action items", "Explained simply", "Cheat sheet",
        "Practice questions", "Quiz", "Flashcards"]


def test_answers_are_hidden_in_toggles_and_flashcards_are_a_table():
    blocks = notion.build_blocks(NOTES)
    toggles = [b for b in blocks if b["type"] == "toggle"]
    assert toggles[0]["toggle"]["children"][0]["paragraph"]["rich_text"][0]["text"]["content"] == "Base + step."
    assert "B) b" in toggles[1]["toggle"]["children"][0]["paragraph"]["rich_text"][0]["text"]["content"]
    table = next(b for b in blocks if b["type"] == "table")
    assert len(table["table"]["children"]) == 2  # header + one card


def test_long_transcript_goes_inside_a_collapsed_toggle(fake_notion):
    transcript = "This sentence is part of a long lecture. " * 6000
    notion.create_page("T", {"summary": "s"}, transcript, "aaaa031d4dec83b7afd701c73565cd05")
    page_blocks = fake_notion.appended[0][1]
    toggle = page_blocks[-1]
    assert toggle["type"] == "heading_2" and toggle["heading_2"]["is_toggleable"]
    assert "Full transcript" in toggle["heading_2"]["rich_text"][0]["text"]["content"]
    # The transcript paragraphs are appended under the toggle, at most 100 per request.
    inside = [children for block_id, children in fake_notion.appended[1:]]
    assert all(block_id == "block" + str(len(page_blocks) - 1) for block_id, _ in fake_notion.appended[1:])
    assert all(len(c) <= 100 for c in inside) and sum(len(c) for c in inside) > 100


def test_a_documents_text_is_labelled_full_text(fake_notion):
    notion.create_page("T", {"summary": "s", "source": "document"}, "Text of the reading.",
                       "aaaa031d4dec83b7afd701c73565cd05")
    toggle = fake_notion.appended[0][1][-1]
    assert toggle["heading_2"]["rich_text"][0]["text"]["content"] == "Full text (4 words)"
