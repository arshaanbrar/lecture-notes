"""The Groq client spreads work over several models, each with its own free limits."""

import httpx
import pytest

from backend.app import config, groq
from backend.app.utils import AppError

OK = {"choices": [{"message": {"content": "{}"}}]}


def limited(kind="TPD", seconds="420"):
    return httpx.Response(429, headers={"retry-after": seconds}, json={"error": {
        "message": f"Rate limit reached for model `x` on tokens per day ({kind}): Limit 200000, Used 199999. "
                   "Please try again in 7m0s.", "code": "rate_limit_exceeded"}})


@pytest.fixture
def groq_replies(monkeypatch):
    """Answer each request with the next reply queued for its model (default: OK)."""
    queued, sent = {}, []

    def fake_send(path, kwargs):
        body = kwargs.get("json") or kwargs.get("data")
        sent.append(body)
        replies = queued.get(body["model"], [])
        return replies.pop(0) if replies else httpx.Response(200, json=OK if "chat" in path else {"text": "hi"})
    monkeypatch.setattr(groq, "_send", fake_send)
    monkeypatch.setattr(groq.time, "sleep", lambda s: None)
    monkeypatch.setattr(config, "GROQ_MODELS", ["big", "kimi", "small"])
    monkeypatch.setattr(config, "GROQ_FAST_MODELS", ["small"])
    monkeypatch.setattr(config, "GROQ_WHISPER_MODELS", ["whisper-turbo", "whisper"])
    return queued, sent


def models(sent):
    return [b["model"] for b in sent]


def test_a_used_up_model_hands_over_to_the_next_without_waiting(groq_replies):
    queued, sent = groq_replies
    queued["big"] = [limited("TPD")]
    groq.chat_json("s", "u", lambda _: None)
    assert models(sent) == ["big", "kimi"]
    # Remembered: the next request goes straight to the model with room.
    groq.chat_json("s", "u", lambda _: None)
    assert models(sent) == ["big", "kimi", "kimi"]
    big = next(r for r in groq.status() if r["model"] == "big")
    assert big["state"].startswith("tokens per day used up")


def test_models_this_key_cant_use_are_skipped_and_remembered(groq_replies):
    queued, sent = groq_replies
    queued["big"] = [httpx.Response(404, json={"error": {"message": "model not found", "code": "model_not_found"}})]
    groq.chat_json("s", "u", lambda _: None)
    groq.chat_json("s", "u", lambda _: None)
    assert models(sent) == ["big", "kimi", "kimi"]


def test_it_waits_only_when_every_model_is_out_and_says_so(groq_replies, monkeypatch):
    queued, sent = groq_replies
    for m in ("big", "kimi", "small"):
        queued[m] = [limited("TPM", "20")]
    progress = []
    clock = [1000.0]
    monkeypatch.setattr(groq.time, "time", lambda: clock[0])
    monkeypatch.setattr(groq.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    groq.chat_json("s", "u", progress.append)
    assert models(sent) == ["big", "kimi", "small", "big"]
    assert any("used up on every AI model for now; waiting" in p for p in progress)


def test_when_everything_is_out_for_hours_it_says_when_to_come_back(groq_replies):
    queued, _ = groq_replies
    for m in ("big", "kimi", "small"):
        queued[m] = [limited("TPD", "7200")]
    with pytest.raises(AppError, match=r"used up on every model for now \(tokens per day\).*about 2h 0m"):
        groq.chat_json("s", "u", lambda _: None)


def test_a_request_too_big_for_one_model_goes_to_another(groq_replies):
    queued, sent = groq_replies
    queued["big"] = [httpx.Response(413, json={"error": {"message": "Request too large on tokens per minute (TPM)"}})]
    groq.chat_json("s", "u", lambda _: None)
    assert models(sent) == ["big", "kimi"]
    groq.chat_json("s", "u", lambda _: None)  # only skipped for that request
    assert models(sent)[-1] == "big"


def test_this_minutes_token_allowance_is_respected_before_asking(groq_replies):
    queued, sent = groq_replies
    queued["big"] = [httpx.Response(200, json=OK, headers={"x-ratelimit-remaining-tokens": "300",
                                                           "x-ratelimit-reset-tokens": "40s"})]
    groq.chat_json("s", "u", lambda _: None)
    groq.chat_json("s", "u", lambda _: None)  # needs more than 300 tokens: goes elsewhere, no 429 first
    assert models(sent) == ["big", "kimi"]


def test_transcription_switches_whisper_models_too(groq_replies, tmp_path):
    queued, sent = groq_replies
    queued["whisper-turbo"] = [limited("ASH", "900")]
    (tmp_path / "a.mp3").write_bytes(b"audio")
    assert groq.transcribe_file(tmp_path / "a.mp3", lambda _: None) == "hi"
    assert models(sent) == ["whisper-turbo", "whisper"]


def test_reasoning_settings_per_model(groq_replies, monkeypatch):
    queued, sent = groq_replies
    monkeypatch.setattr(config, "GROQ_MODELS", ["openai/gpt-oss-120b", "qwen/qwen3-32b"])
    queued["openai/gpt-oss-120b"] = [limited()]
    groq.chat_json("s", "u", lambda _: None)
    assert sent[0]["reasoning_effort"] == "low" and sent[1]["reasoning_effort"] == "none"
    groq.chat_json("s", "u", lambda _: None, effort="medium")
    assert sent[-1]["model"] == "qwen/qwen3-32b" and sent[-1]["reasoning_effort"] == "default"


def test_thinking_tags_are_removed_from_answers(groq_replies):
    queued, _ = groq_replies
    queued["big"] = [httpx.Response(200, json={"choices": [{"message": {"content": "<think>hmm</think>\n{\"a\": 1}"}}]})]
    assert groq.chat_json("s", "u", lambda _: None) == '{"a": 1}'


def test_quick_questions_use_the_quick_models_first(groq_replies):
    _, sent = groq_replies
    groq.chat_text("s", [{"role": "user", "content": "hi"}], fast=True)
    assert models(sent) == ["small"] and groq.last_model("fast") == "small"
