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


class ModelUnavailable(AppError):
    """The requested model is retired or not available to this Groq account."""


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


def _is_model_unavailable(resp: httpx.Response) -> bool:
    if resp.status_code in (403, 404):
        return True
    try:
        code = resp.json()["error"].get("code", "")
    except Exception:
        return False
    return code in ("model_not_found", "model_decommissioned", "model_permission_blocked_org")


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
            if _is_model_unavailable(resp):
                raise ModelUnavailable(_error_message(resp))
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


_working_model: str | None = None


def _chat(messages: list[dict], progress: Progress, json_mode: bool, temperature: float) -> str:
    """Run a chat completion with the first model in GROQ_MODELS this account can access."""
    global _working_model
    models = [_working_model] if _working_model else config.GROQ_MODELS
    errors = []
    for model in models:
        body = {"model": model, "messages": messages, "temperature": temperature}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        try:
            data = _post("/chat/completions", progress, lambda: {"json": body})
        except ModelUnavailable as e:
            errors.append(f"{model}: {e}")
            continue
        _working_model = model
        return data["choices"][0]["message"]["content"] or ""
    _working_model = None
    raise AppError("None of the Groq models are available to your account ("
                   + "; ".join(errors) + "). Set GROQ_MODEL to one listed at https://console.groq.com/docs/models.")


def chat_json(system: str, user: str, progress: Progress) -> str:
    """Ask for a JSON reply."""
    return _chat([{"role": "system", "content": system}, {"role": "user", "content": user}], progress, True, 0.2)


def chat_text(system: str, messages: list[dict], progress: Progress = lambda _: None) -> str:
    """A normal conversational reply. `messages` alternate user/assistant turns."""
    return _chat([{"role": "system", "content": system}, *messages], progress, False, 0.4).strip()
