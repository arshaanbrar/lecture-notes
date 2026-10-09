from backend.app import summarize


def test_short_lecture_makes_notes_with_key_terms(fake_ai):
    notes = summarize.make_notes("Induction lecture.", lambda _: None)
    assert notes["title"] == "Proofs by Induction"
    assert notes["key_terms"] == [{"term": "Base case", "definition": "P(1)."}]
    assert len(fake_ai.prompts) == 1  # no extras requested -> one AI call


def test_long_lecture_is_summarised_in_parts_then_merged(fake_ai, monkeypatch):
    monkeypatch.setattr(summarize.config, "SUMMARY_CHUNK_CHARS", 3000)
    summarize.make_notes("Induction is a proof technique. " * 400, lambda _: None)
    assert sum("This is part" in p for p in fake_ai.prompts) >= 3
    assert any(p.startswith("Below are notes written for consecutive parts") for p in fake_ai.prompts)


def test_only_ticked_extras_are_requested_and_cleaned(fake_ai):
    notes = summarize.make_notes("Induction.", lambda _: None, extras=["quiz", "flashcards", "bogus"])
    extras_prompt = fake_ai.prompts[-1]
    assert '"quiz":' in extras_prompt and '"flashcards":' in extras_prompt
    assert '"practice_questions":' not in extras_prompt
    assert "practice_questions" not in notes  # returned by the AI but not requested
    assert notes["quiz"][0]["answer"] == 1 and notes["flashcards"][0]["front"] == "Base case"


def test_failed_extras_keep_the_notes_and_warn(fake_ai):
    fake_ai.fail_extras = True
    warnings = []
    notes = summarize.make_notes("Induction.", lambda _: None, extras=["quiz"], warn=warnings.append)
    assert notes["title"] == "Proofs by Induction" and "quiz" not in notes
    assert warnings and "study extras" in warnings[0]


def test_slides_are_added_to_the_prompt(fake_ai):
    summarize.make_notes("Induction.", lambda _: None, slides="[Slide 1]\nStrong induction")
    assert "LECTURE SLIDES" in fake_ai.prompts[0] and "Strong induction" in fake_ai.prompts[0]


def test_bad_quiz_items_are_dropped():
    cleaned = summarize._clean_extras({"quiz": [
        {"question": "ok", "options": ["a", "b", "c", "d"], "answer": 3},
        {"question": "too few options", "options": ["a"], "answer": 0},
        {"question": "bad answer", "options": ["a", "b", "c", "d"], "answer": 9},
    ]}, ["quiz"])
    assert [q["question"] for q in cleaned["quiz"]] == ["ok"]


def test_each_deck_gets_a_share_of_the_room(fake_ai):
    from backend.app.slides import DECK_SEPARATOR
    decks = DECK_SEPARATOR.join(["[Slides: a.pdf]\n" + "A" * 20000, "[Slides: b.pdf]\n" + "B" * 20000])
    summarize.make_notes("Induction.", lambda _: None, slides=decks)
    assert "[Slides: a.pdf]" in fake_ai.prompts[0] and "[Slides: b.pdf]" in fake_ai.prompts[0]
