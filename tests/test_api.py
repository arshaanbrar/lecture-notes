from tests.conftest import wait_for_job
from tests.fakes import H

AUDIO = ("lecture.webm", b"fake audio")


def test_site_and_health(client):
    assert client.get("/healthz").json() == {"ok": True}
    page = client.get("/")
    assert page.status_code == 200 and "Built by Arshaan" in page.text
    assert page.headers["cache-control"] == "no-cache"  # browsers always pick up new versions
    for asset in ("/app.js", "/chat.js", "/store.js", "/styles.css"):
        assert client.get(asset).status_code == 200


def test_password_is_required_when_set(client, password):
    assert client.get("/api/config").json()["password_required"] is True
    assert client.get("/api/auth/check").status_code == 401
    assert client.get("/api/auth/check", headers={"X-App-Password": "wrong"}).status_code == 401
    assert client.get("/api/auth/check", headers={"X-App-Password": password}).status_code == 200


def test_upload_makes_notes_with_extras(client, stub_audio, fake_ai):
    job = client.post("/api/jobs/upload", files={"file": AUDIO}, data={"extras": "quiz,flashcards"}).json()
    job = wait_for_job(client, job["id"])
    assert job["status"] == "done", job["error"]
    assert job["notes"]["title"] == "Proofs by Induction"
    assert job["notes"]["quiz"] and job["notes"]["flashcards"] and job["notes"]["key_terms"]


def test_upload_a_document_makes_notes_without_transcribing(client, fake_ai, monkeypatch):
    from backend.app import audio
    monkeypatch.setattr(audio, "normalize", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no audio step")))
    doc = ("Week 3 reading.txt", b"Induction: prove P(1), then P(k) implies P(k+1).")
    job = wait_for_job(client, client.post("/api/jobs/upload", files={"file": doc},
                                           data={"extras": "flashcards"}).json()["id"])
    assert job["status"] == "done", job["error"]
    assert job["source"] == "document" and job["transcript"].startswith("Induction: prove P(1)")
    assert job["notes"]["source"] == "document" and job["notes"]["flashcards"]
    # The AI is told it's reading a document, not a speech transcript.
    assert any(p.startswith("Note: the input below is the text of a document") for p in fake_ai.prompts)


def test_upload_rejects_empty_files_and_wrong_slides(client):
    assert client.post("/api/jobs/upload", files={"file": ("a.webm", b"")}).status_code == 400
    bad = client.post("/api/jobs/upload", files={"file": AUDIO, "slides_file": ("notes.docx", b"x")})
    assert bad.status_code == 400 and "PDF or PowerPoint" in bad.json()["detail"]


def test_unreadable_slides_still_make_notes(client, stub_audio, fake_ai):
    job = client.post("/api/jobs/upload", files={"file": AUDIO, "slides_file": ("deck.pdf", b"not a pdf")}).json()
    job = wait_for_job(client, job["id"])
    assert job["status"] == "done" and "slides" in job["warning"]


def test_link_jobs_validate_the_url(client):
    assert client.post("/api/jobs/url", data={"url": "ftp://nope"}).status_code == 422


def test_unknown_job_is_a_clear_404(client):
    assert "restarted" in client.get("/api/jobs/nope").json()["detail"]


def test_send_to_notion_flow(client, fake_notion, fake_ai):
    people = client.get("/api/notion/people").json()["people"]
    assert [p["title"] for p in people] == ["Arshaan", "Efrain", "Ryan"]
    guess = client.post("/api/notion/guess", json={"note_title": "Induction", "summary": "Discrete math"}).json()
    assert guess["person_id"] == H("arshaan")
    plan = client.post("/api/notion/plan", json={"person_id": guess["person_id"], "class_id": guess["class_id"],
                                                 "note_title": "Induction"}).json()
    place = plan["candidates"][plan["best"]]
    sent = client.post("/api/notion/export", json={
        "place": {k: place[k] for k in ("kind", "target_id", "link_to")}, "title": "Induction",
        "notes": {"summary": "s", "quiz": [{"question": "q", "options": ["a", "b", "c", "d"], "answer": 0}]},
        "transcript": "t", "local_date": "2026-10-05"})
    assert sent.status_code == 200 and sent.json()["url"]


def test_export_rejects_bad_places(client):
    bad = client.post("/api/notion/export", json={"place": {"kind": "delete-everything", "target_id": "x"},
                                                  "title": "T", "notes": {}})
    assert bad.status_code == 422


def test_chat_uses_the_lecture(client, fake_ai):
    reply = client.post("/api/assistant/chat", json={
        "messages": [{"role": "user", "content": "Explain the base case"}],
        "context": {"title": "Induction", "summary": "s", "transcript": "We prove P(1) first."}}).json()
    assert reply["reply"] == "Answer to: Explain the base case"
    assert "We prove P(1) first." in fake_ai.prompts[-1]


def test_chat_during_a_recording(client, fake_ai, stub_audio):
    text = client.post("/api/assistant/transcribe", files={"audio_file": ("s.webm", b"audio")},
                       data={"skip_seconds": "1"}).json()["text"]
    assert text == "Today: proof by induction."
    client.post("/api/assistant/chat", json={"messages": [{"role": "user", "content": "What was just said?"}],
                                             "context": {"live": True, "transcript": text}})
    assert "RIGHT NOW" in fake_ai.prompts[-1]


def test_chat_needs_a_question(client, fake_ai):
    assert client.post("/api/assistant/chat", json={"messages": []}).status_code == 400


def test_health_check_says_how_busy_the_server_is(client):
    data = client.get("/healthz").json()
    assert data["ok"] is True and data["busy"] == 0
