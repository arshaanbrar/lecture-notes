"""Groq API client (OpenAI-compatible endpoints), built around the free tier's limits.

Groq's free limits are per model: requests and tokens per minute and per day for chat models, audio
seconds per hour and per day for Whisper. Everyone using the site shares one key, so one model's
allowance runs out quickly. Instead of waiting when a model hits a limit, a request goes to the next
model in the list that still has room. Each model's state is remembered (from Groq's rate-limit
headers and its "limit reached" replies), so models that are out are skipped until they reset. It
only waits when every model is out, and then only as long as the soonest one needs.
"""

import json
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import httpx

from . import config
from .utils import AppError

BASE = "https://api.groq.com/openai/v1"
MAX_WAIT_SECONDS = 600      # longest a request waits for any model to have room
NETWORK_TRIES = 3           # per model, for connection errors and Groq server errors
UNAVAILABLE_SECONDS = 3600  # a model this key can't use is skipped for this long
LISTING_SECONDS = 1800

Progress = Callable[[str], None]


class ModelUnavailable(AppError):
    """The requested model is retired or not available to this Groq account."""


@dataclass
class ModelState:
    blocked_until: float = 0.0       # not usable by this key (missing / no access)
    limited_until: float = 0.0       # a free limit was hit; it frees up then
    limit: str = ""                  # which limit, e.g. "tokens per day"
    tokens_left: int | None = None   # this minute's token allowance, from Groq's headers...
    tokens_reset: float = 0.0        # ...and when it refills
    requests: int = 0                # successful requests since the server started


_state: dict[str, ModelState] = {}
_last_model: dict[str, str] = {}     # pool -> the model that answered last
_listing: dict = {"at": 0.0, "models": None}
_lock = threading.Lock()


def _headers() -> dict:
    if not config.GROQ_API_KEY:
        raise AppError("GROQ_API_KEY isn't set on the server (see README → Get a Groq key).")
    return {"Authorization": f"Bearer {config.GROQ_API_KEY}"}


def _model_state(model: str) -> ModelState:
    with _lock:
        return _state.setdefault(model, ModelState())


# ---------- reading Groq's replies ----------

def _error_message(resp: httpx.Response) -> str:
    try:
        return resp.json()["error"]["message"]
    except Exception:
        return resp.text[:300]


def _error_code(resp: httpx.Response) -> str:
    try:
        return resp.json()["error"].get("code", "") or ""
    except Exception:
        return ""


def _is_model_unavailable(resp: httpx.Response) -> bool:
    return resp.status_code in (403, 404) or _error_code(resp) in (
        "model_not_found", "model_decommissioned", "model_permission_blocked_org")


def _duration(text: str) -> float | None:
    """Seconds in Groq's duration format, e.g. "7.66s", "2m59.5s", "1h2m3s", "450ms"."""
    total, found = 0.0, False
    for number, unit in re.findall(r"([\d.]+)\s*(ms|h|m|s)", text or ""):
        found = True
        total += float(number) * {"ms": 0.001, "h": 3600, "m": 60, "s": 1}[unit]
    return total if found else None


def _retry_after(resp: httpx.Response) -> float:
    try:
        return max(1.0, float(resp.headers.get("retry-after", "")))
    except ValueError:
        pass
    found = re.search(r"try again in ([\dhms.]+)", _error_message(resp))
    return max(1.0, (found and _duration(found.group(1))) or 30.0)


LIMIT_NAMES = {"TPM": "tokens per minute", "TPD": "tokens per day", "RPM": "requests per minute",
               "RPD": "requests per day", "ASH": "audio per hour", "ASD": "audio per day"}


def _limit_name(resp: httpx.Response) -> str:
    found = re.search(r"\((TPM|TPD|RPM|RPD|ASH|ASD)\)", _error_message(resp))
    return LIMIT_NAMES[found.group(1)] if found else "rate limit"


def _note_headers(model: str, resp: httpx.Response) -> None:
    """Remember how much of this minute's token allowance is left, to avoid hitting it."""
    left = resp.headers.get("x-ratelimit-remaining-tokens")
    reset = _duration(resp.headers.get("x-ratelimit-reset-tokens", ""))
    if left is None or reset is None:
        return
    try:
        st = _model_state(model)
        st.tokens_left, st.tokens_reset = int(float(left)), time.time() + reset
    except ValueError:
        pass


# ---------- choosing a model ----------

def _listed_models() -> set[str] | None:
    """The models this key can see (cached). None if Groq couldn't be asked: then nothing is filtered."""
    if _listing["models"] is not None and time.time() - _listing["at"] < LISTING_SECONDS:
        return _listing["models"]
    try:
        resp = httpx.get(BASE + "/models", headers=_headers(), timeout=15)
        resp.raise_for_status()
        models = {m["id"] for m in resp.json().get("data", [])}
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return None
    _listing.update(at=time.time(), models=models or None)
    return _listing["models"]


def _usable(models: list[str]) -> list[str]:
    listed = _listed_models()
    now = time.time()
    return [m for m in models if (listed is None or m in listed) and _model_state(m).blocked_until <= now]


def _free_at(model: str, need_tokens: int) -> float:
    """When this model can take a request needing about `need_tokens` (now, or later)."""
    st, now = _model_state(model), time.time()
    at = max(now, st.limited_until)
    if st.tokens_left is not None and st.tokens_reset > now and st.tokens_left < need_tokens:
        at = max(at, st.tokens_reset)
    return at


def _short(model: str) -> str:
    return model.split("/")[-1]


def _call(path: str, models: list[str], build: Callable[[str], dict], progress: Progress, what: str,
          need_tokens: int = 0) -> tuple[str, dict]:
    """Send a request to the first model in `models` that has room; returns (model, reply)."""
    deadline = time.time() + MAX_WAIT_SECONDS
    skipped: set[str] = set()   # failed for this request only (too large, rejected, server trouble)
    errors: list[str] = []
    while True:
        candidates = [m for m in _usable(models) if m not in skipped]
        if not candidates:
            raise AppError("None of the Groq models could take this request ("
                           + ("; ".join(errors[-4:]) or "none are available to this key")
                           + "). Check /check, or set GROQ_MODEL to models listed at https://console.groq.com/docs/models.")
        now = time.time()
        ready = [m for m in candidates if _free_at(m, need_tokens) <= now]
        if not ready:  # every model is out of room for now
            soonest = min(_free_at(m, need_tokens) for m in candidates) - now
            if now + soonest > deadline:
                raise AppError(_used_up_message(candidates, soonest))
            progress(f"Groq's free limits are used up on every {what} model for now; waiting {int(soonest) + 1}s…")
            time.sleep(min(soonest, 30) + 0.5)
            continue

        model = ready[0]  # the lists are in order of preference
        st = _model_state(model)
        resp = _send(path, build(model))
        if resp is None:  # network or Groq server trouble that didn't clear up
            skipped.add(model)
            errors.append(f"{_short(model)}: no answer")
            continue
        _note_headers(model, resp)
        if resp.status_code == 429:
            st.limited_until, st.limit = time.time() + _retry_after(resp), _limit_name(resp)
            if len(candidates) > 1:
                progress(f"Groq's free limit ({st.limit}) reached for {_short(model)}; switching to another model…")
            continue
        if _is_model_unavailable(resp):
            st.blocked_until, st.limit = time.time() + UNAVAILABLE_SECONDS, "not available to this key"
            errors.append(f"{_short(model)}: {_error_message(resp)}")
            continue
        if resp.status_code >= 400:
            # e.g. 413 "too large for this model's per-minute limit", or a model that rejected the request
            # or returned invalid JSON. Another model may well manage it.
            errors.append(f"{_short(model)}: {_error_message(resp)}")
            if len(candidates) > 1:
                skipped.add(model)
                continue
            raise AppError(f"Groq error ({resp.status_code}): {_error_message(resp)}")
        st.requests += 1
        return model, resp.json()


def _send(path: str, kwargs: dict) -> httpx.Response | None:
    with httpx.Client(timeout=httpx.Timeout(300.0, connect=15.0)) as client:
        for attempt in range(NETWORK_TRIES):
            try:
                resp = client.post(BASE + path, headers=_headers(), **kwargs)
            except httpx.TransportError:
                time.sleep(2.0 * (attempt + 1))
                continue
            if resp.status_code >= 500:
                time.sleep(2.0 * (attempt + 1))
                continue
            return resp
    return None


def _used_up_message(models: list[str], seconds: float) -> str:
    limits = sorted({_model_state(m).limit for m in models if _model_state(m).limit})
    hours, minutes = divmod(max(1, round(seconds / 60)), 60)
    when = f"{hours}h {minutes}m" if hours else f"{minutes} min"
    return (f"Groq's free limits are used up on every model for now ({', '.join(limits) or 'rate limits'}); "
            f"the soonest frees up in about {when}. Try again then (README → Groq limits explains how to raise them).")


# ---------- public ----------

def transcribe_file(path: Path, progress: Progress) -> str:
    def build(model: str) -> dict:
        return {
            "data": {"model": model, "response_format": "json", "temperature": "0"},
            "files": {"file": (path.name, path.read_bytes(), "audio/mpeg")},
        }
    model, data = _call("/audio/transcriptions", config.GROQ_WHISPER_MODELS, build, progress, "transcription")
    _last_model["whisper"] = model
    return data.get("text", "").strip()


def _pool(name: str) -> list[str]:
    if name == "fast":
        return config.GROQ_FAST_MODELS + [m for m in config.GROQ_MODELS if m not in config.GROQ_FAST_MODELS]
    return config.GROQ_MODELS


def _model_options(model: str, effort: str | None) -> dict:
    """Per-model settings: how much reasoning models "think" before answering."""
    if "gpt-oss" in model:
        effort = effort or config.GROQ_REASONING_EFFORT
        return {"reasoning_effort": effort} if effort else {}
    if "qwen3" in model:
        return {"reasoning_effort": "default" if effort in ("medium", "high") else "none"}
    return {}


def _chat(messages: list[dict], progress: Progress, json_mode: bool, temperature: float, pool: str = "main",
          effort: str | None = None) -> str:
    def build(model: str) -> dict:
        body = {"model": model, "messages": messages, "temperature": temperature, **_model_options(model, effort)}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        return {"json": body}

    need = len(json.dumps(messages)) // 3 + 1500  # rough: the prompt's tokens plus room for the answer
    model, data = _call("/chat/completions", _pool(pool), build, progress,
                        "quick-question" if pool == "fast" else "AI", need)
    _last_model[pool] = model
    content = data["choices"][0]["message"]["content"] or ""
    return re.sub(r"<think>.*?</think>", "", content, flags=re.S).strip()  # some models show their thinking


def chat_json(system: str, user: str, progress: Progress, fast: bool = False, effort: str | None = None) -> str:
    """Ask for a JSON reply. `fast` uses the quick models (their own limits, so the notes models' are
    saved); `effort` asks reasoning models to think harder ("medium") on small but tricky questions."""
    return _chat([{"role": "system", "content": system}, {"role": "user", "content": user}], progress, True, 0.2,
                 "fast" if fast else "main", effort)


def chat_text(system: str, messages: list[dict], progress: Progress = lambda _: None, fast: bool = False) -> str:
    """A normal conversational reply. `messages` alternate user/assistant turns."""
    return _chat([{"role": "system", "content": system}, *messages], progress, False, 0.4,
                 "fast" if fast else "main")


def last_model(pool: str) -> str:
    return _last_model.get(pool, "")


def status() -> list[dict]:
    """Each model's state right now, for the system check."""
    now, rows = time.time(), []
    for kind, models in (("notes", config.GROQ_MODELS), ("quick", config.GROQ_FAST_MODELS),
                         ("transcription", config.GROQ_WHISPER_MODELS)):
        for model in models:
            st = _model_state(model)
            if st.blocked_until > now:
                state = "not available to this key"
            elif st.limited_until > now:
                state = f"{st.limit} used up, frees up in {int((st.limited_until - now) // 60) + 1} min"
            else:
                state = "ready"
            rows.append({"model": model, "kind": kind, "state": state, "requests": st.requests})
    return rows
