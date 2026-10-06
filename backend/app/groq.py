"""Thin Groq API client (OpenAI-compatible endpoints) with retries for free-tier rate limits."""

import time
from pathlib import Path
from typing import Callable

import httpx

from . import config
from .utils import AppError

BASE = "https://api.groq.com/openai/v1"
MAX_ATTEMPTS = 8

Progress = Callable[[str], None]


def _headers() -> dict:
    if not config.GROQ_API_KEY:
        raise AppError("GROQ_API_KEY isn't set on the server (see README → Get a Groq key).")
    return {"Authorization": f"Bearer {config.GROQ_API_KEY}"}


def _wait_seconds(resp: httpx.Response, attempt: int) -> float:
    try:
        return min(float(resp.headers.get("retry-after", "")), 120.0)
    except ValueError:
        return min(2.0 * 2**attempt, 60.0)


def _error_message(resp: httpx.Response) -> str:
    try:
        return resp.json()["error"]["message"]
    except Exception:
        return resp.text[:300]


def _post(path: str, progress: Progress, make_kwargs: Callable[[], dict]) -> dict:
    with httpx.Client(timeout=httpx.Timeout(300.0, connect=15.0)) as client:
        for attempt in range(MAX_ATTEMPTS):
            try:
                resp = client.post(BASE + path, headers=_headers(), **make_kwargs())
            except httpx.TransportError:
                time.sleep(2.0 * (attempt + 1))
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = _wait_seconds(resp, attempt)
                progress(f"Groq free-tier limit hit, waiting {int(wait)}s and retrying…")
                time.sleep(wait)
                continue
            if resp.status_code >= 400:
                raise AppError(f"Groq error ({resp.status_code}): {_error_message(resp)}")
            return resp.json()
    raise AppError("Groq kept rate-limiting us. Wait a few minutes and try again.")


def transcribe_file(path: Path, progress: Progress) -> str:
    def kwargs():
        return {
            "data": {"model": config.GROQ_WHISPER_MODEL, "response_format": "json", "temperature": "0"},
            "files": {"file": (path.name, path.read_bytes(), "audio/mpeg")},
        }
    return _post("/audio/transcriptions", progress, kwargs).get("text", "").strip()


def chat_json(system: str, user: str, progress: Progress) -> str:
    body = {
        "model": config.GROQ_MODEL,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }
    data = _post("/chat/completions", progress, lambda: {"json": body})
    return data["choices"][0]["message"]["content"]
