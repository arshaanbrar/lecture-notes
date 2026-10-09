import os
import time

# Settings are read when the app is imported, so set fake keys first.
os.environ.update(GROQ_API_KEY="test", NOTION_TOKEN="test", APP_PASSWORD="", NOTION_PARENT_PAGE_ID="")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from backend.app import audio, config, groq, main, notion, placement, transcribe  # noqa: E402
from backend.app.utils import AppError  # noqa: E402
from tests.fakes import FakeAI, FakeNotion  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_caches():
    notion._tree_cache.update(at=0.0, nodes=None)
    for cache in (notion._table_cache, notion._sources_cache, notion._block_parents, placement._classes_cache):
        cache.clear()
    yield


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Tests never reach the real Notion or Groq; the fakes below replace these where needed."""
    def offline(*args, **kwargs):
        raise AppError("offline in tests")
    monkeypatch.setattr(notion, "_request", offline)
    monkeypatch.setattr(groq, "_send", offline)
    monkeypatch.setattr(groq, "_listed_models", lambda: None)
    monkeypatch.setattr(groq, "_state", {})
    monkeypatch.setattr(groq, "_last_model", {})


@pytest.fixture
def fake_notion(monkeypatch):
    fake = FakeNotion()
    monkeypatch.setattr(notion, "_request", fake.request)
    return fake


@pytest.fixture
def fake_ai(monkeypatch):
    fake = FakeAI()
    monkeypatch.setattr(groq, "chat_json", fake.chat_json)
    monkeypatch.setattr(groq, "chat_text", fake.chat_text)
    return fake


@pytest.fixture
def stub_audio(monkeypatch):
    """Skip ffmpeg and Whisper: every upload 'transcribes' to the same short lecture."""
    monkeypatch.setattr(audio, "normalize", lambda src, workdir, skip_seconds=0: src)
    monkeypatch.setattr(transcribe, "transcribe", lambda path, workdir, progress: "Today: proof by induction.")


@pytest.fixture
def client():
    return TestClient(main.app)


@pytest.fixture
def password(monkeypatch):
    monkeypatch.setattr(config, "APP_PASSWORD", "secret")
    return "secret"


def wait_for_job(client, job_id, headers=None, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}", headers=headers or {}).json()
        if job["status"] in ("done", "error"):
            return job
        time.sleep(0.05)
    raise AssertionError("job did not finish")
