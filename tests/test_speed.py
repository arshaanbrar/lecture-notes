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

    def fake_whisper(path, progress, max_wait=None):
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


def test_a_scan_read_on_the_device_is_summarised_as_a_document(client, fake_ai):
    job = client.post("/api/jobs/text", data={"transcript": "Soldiers and the state. " * 30, "label": "Messing.pdf",
                                              "source": "document", "place": "false"}).json()
    job = wait_for_job(client, job["id"])
    assert job["status"] == "done" and job["source"] == "document" and job["notes"]["source"] == "document"
    assert any(p.startswith("Note: the input below is the text of a document") for p in fake_ai.prompts)


def test_quick_jobs_dont_wait_behind_recordings_and_waiting_says_why(client, stub_audio, fake_ai, monkeypatch):
    import threading
    release = threading.Event()

    def slow_transcribe(path, workdir, progress):
        progress("Transcribing… 1 of 5 parts done")
        release.wait(10)
        return "Today: proof by induction."
    monkeypatch.setattr(transcribe, "transcribe", slow_transcribe)
    try:
        first = client.post("/api/jobs/upload", files={"file": ("long.webm", b"audio")}).json()
        second = client.post("/api/jobs/upload", files={"file": ("next.webm", b"audio")}).json()
        deadline = time.time() + 5
        while client.get(f"/api/jobs/{first['id']}").json()["status"] != "transcribing" and time.time() < deadline:
            time.sleep(0.02)
        waiting = client.get(f"/api/jobs/{second['id']}").json()
        assert waiting["status"] == "queued"
        assert waiting["message"] == "Waiting for 1 other file to finish first (Transcribing… 1 of 5 parts done)"
        # Notes from text (a live recording, or a scan read on the device) go straight through.
        quick = client.post("/api/jobs/text", data={"transcript": LECTURE, "place": "false"}).json()
        assert wait_for_job(client, quick["id"], timeout=5)["status"] == "done"
        assert client.get(f"/api/jobs/{first['id']}").json()["status"] == "transcribing"
    finally:
        release.set()
    assert wait_for_job(client, second["id"])["status"] == "done"


def test_notes_written_during_the_lecture_leave_only_the_end_and_the_merge(fake_ai, monkeypatch):
    monkeypatch.setattr(config, "SUMMARY_CHUNK_CHARS", 1000)
    transcript = "Week one covers sets and logic. " * 60 + "Finally we started induction proofs today. " * 10
    covered = len("Week one covers sets and logic. " * 60)
    parts = [summarize.part_notes(transcript[:covered // 2], 1), summarize.part_notes(transcript[covered // 2:covered], 2)]
    fake_ai.prompts.clear()
    notes = summarize.make_notes(transcript, lambda _: None, parts=parts, parts_chars=covered)
    assert notes["title"] == "Proofs by Induction"
    # Just the end of the lecture (one part) and the merge, not the whole lecture again.
    assert len(fake_ai.prompts) == 2
    assert "TRANSCRIPT PART 3:\nFinally we started induction" in fake_ai.prompts[0]
    assert fake_ai.prompts[1].startswith("Below are notes written for consecutive parts")


def test_live_part_notes_route_and_text_job_with_parts(client, fake_ai):
    part = client.post("/api/live/part-notes", json={"text": "Base case and inductive step.", "index": 1}).json()
    assert part["notes"]["title"] == "Proofs by Induction"
    import json as _json
    data = {"transcript": LECTURE, "place": "false", "parts": _json.dumps([part["notes"]]), "parts_chars": "200"}
    job = wait_for_job(client, client.post("/api/jobs/text", data=data).json()["id"])
    assert job["status"] == "done", job["error"]
    assert any(p.startswith("Below are notes written for consecutive parts") for p in fake_ai.prompts)
    bad = client.post("/api/jobs/text", data={"transcript": "x", "parts": "not json"})
    assert bad.status_code == 400


def test_jobs_use_the_devices_person_and_the_recording_time(client, fake_notion, fake_ai):
    data = {"transcript": "Today in management we cover motivation and leading teams. " * 10,
            "person_id": H("ryan"), "recorded_at": "Tuesday, October 7 at 11:47 AM"}
    job = wait_for_job(client, client.post("/api/jobs/text", data=data).json()["id"])
    assert job["placement"]["person_id"] == H("ryan") and job["placement"]["class_id"] == H("r_mgm")
    prompt = next(p for p in fake_ai.prompts if "which of their classes" in p.lower())
    assert "Recorded: Tuesday, October 7 at 11:47 AM" in prompt
