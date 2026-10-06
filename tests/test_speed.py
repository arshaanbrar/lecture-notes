"""The things that make it faster and use less AI: live transcripts, placement alongside the notes,
one call for short notes, skipping the AI when the answer is obvious, parallel transcription."""

import time

from backend.app import audio, config, groq, notion, summarize, transcribe
from tests.conftest import wait_for_job
from tests.fakes import H

LECTURE = "Today in discrete math we prove things by induction. " * 20


def test_live_transcript_makes_notes_and_finds_the_notion_spot(client, fake_notion, fake_ai):
    job = client.post("/api/jobs/text", data={"transcript": LECTURE, "extras": "flashcards"}).json()
    job = wait_for_job(client, job["id"])
    assert job["status"] == "done", job["error"]
    assert job["transcript"] == LECTURE and job["notes"]["flashcards"]
    # Worked out while the notes were written: Arshaan's Discrete Math, in the Topics table.
    place = job["placement"]
    assert place["person_id"] == H("arshaan") and place["class_id"] == H("dm")
    best = place["plan"]["candidates"][place["plan"]["best"]]
    assert best["kind"] == "entry" and notion.same_id(best["target_id"], H("topics"))
    assert place["plan_for"] == {"person_id": H("arshaan"), "class_id": H("dm"), "class_text": ""}
    # The spot was obvious, so the AI wasn't asked to choose it.
    assert not any("Places it could be saved" in p for p in fake_ai.prompts)


def test_no_placement_when_the_page_already_worked_it_out(client, fake_notion, fake_ai):
    job = client.post("/api/jobs/text", data={"transcript": LECTURE, "place": "false"}).json()
    job = wait_for_job(client, job["id"])
    assert job["status"] == "done" and job["placement"] is None
    assert not any("These students share one Notion" in p for p in fake_ai.prompts)


def test_uploads_also_get_placed_alongside_the_notes(client, stub_audio, fake_notion, fake_ai, monkeypatch):
    monkeypatch.setattr(transcribe, "transcribe", lambda *a: LECTURE)
    job = client.post("/api/jobs/upload", files={"file": ("lecture.webm", b"audio")}).json()
    job = wait_for_job(client, job["id"])
    assert job["placement"]["person_id"] == H("arshaan")


def test_short_lectures_get_notes_and_extras_in_one_ai_call(fake_ai):
    notes = summarize.make_notes(LECTURE, lambda _: None, extras=["quiz", "flashcards"])
    assert notes["quiz"] and notes["flashcards"] and notes["key_terms"]
    assert len(fake_ai.prompts) == 1


def test_live_pieces_tell_silence_from_broken_audio(client, monkeypatch):
    def too_short(*a, **k):
        raise audio.TooShort("too short")
    monkeypatch.setattr(audio, "normalize", too_short)
    quiet = client.post("/api/assistant/transcribe", files={"audio_file": ("s.webm", b"x")}).json()
    assert quiet == {"text": "", "ok": True}

    def broken(*a, **k):
        raise audio.AppError("Couldn't read that audio/video file")
    monkeypatch.setattr(audio, "normalize", broken)
    bad = client.post("/api/assistant/transcribe", files={"audio_file": ("s.webm", b"x")}).json()
    assert bad["ok"] is False and "Couldn't read" in bad["error"]


def test_long_uploads_are_transcribed_a_few_pieces_at_a_time_in_order(monkeypatch, tmp_path):
    pieces = [tmp_path / f"chunk_{i}.mp3" for i in range(5)]
    monkeypatch.setattr(audio, "split", lambda *a: pieces)
    running, most = 0, 0

    def fake_whisper(path, progress):
        nonlocal running, most
        running += 1
        most = max(most, running)
        time.sleep(0.05 * (5 - int(path.stem[-1])))  # later pieces finish first
        running -= 1
        return path.stem
    monkeypatch.setattr(groq, "transcribe_file", fake_whisper)
    text = transcribe.transcribe(tmp_path / "a.mp3", tmp_path, lambda _: None)
    assert text == "chunk_0 chunk_1 chunk_2 chunk_3 chunk_4"
    assert most == transcribe.PARALLEL_PIECES


def test_gpt_oss_thinks_less_and_quick_questions_use_the_fast_model(monkeypatch):
    sent = []

    def fake_post(path, progress, make_kwargs):
        body = make_kwargs()["json"]
        sent.append(body)
        if body["model"] == "unavailable-fast":
            raise groq.ModelUnavailable("no access")
        return {"choices": [{"message": {"content": "{}"}}]}
    monkeypatch.setattr(groq, "_post", fake_post)
    monkeypatch.setattr(groq, "_working", {})
    monkeypatch.setattr(config, "GROQ_MODELS", ["openai/gpt-oss-120b"])
    monkeypatch.setattr(config, "GROQ_FAST_MODELS", ["unavailable-fast", "openai/gpt-oss-20b"])

    groq.chat_json("s", "u", lambda _: None, fast=True)
    assert [b["model"] for b in sent] == ["unavailable-fast", "openai/gpt-oss-20b"]
    assert sent[-1]["reasoning_effort"] == config.GROQ_REASONING_EFFORT == "low"
    groq.chat_json("s", "u", lambda _: None)
    assert sent[-1]["model"] == "openai/gpt-oss-120b"
    groq.chat_json("s", "u", lambda _: None, fast=True)  # remembered: no retry of the unavailable one
    assert sent[-1]["model"] == "openai/gpt-oss-20b" and len(sent) == 4


def test_a_scan_read_on_the_device_is_summarised_as_a_document(client, fake_ai):
    job = client.post("/api/jobs/text", data={"transcript": "Soldiers and the state. " * 30, "label": "Messing.pdf",
                                              "source": "document", "place": "false"}).json()
    job = wait_for_job(client, job["id"])
    assert job["status"] == "done" and job["source"] == "document" and job["notes"]["source"] == "document"
    assert any(p.startswith("Note: the input below is the text of a document") for p in fake_ai.prompts)
