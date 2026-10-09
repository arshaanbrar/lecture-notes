"""All settings come from environment variables (or a local .env file).

⚠️  You never paste keys into the code. Put them in:
    - `.env` in the project root when running locally (copy `.env.example`)
    - Render dashboard → your service → Environment when deployed
"""

import os

from dotenv import load_dotenv

load_dotenv()


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


# ⚠️ KEY — Groq API key (https://console.groq.com/keys). Used for transcription and notes.
GROQ_API_KEY = _env("GROQ_API_KEY")
def _models(name: str, default: str) -> list[str]:
    return [m.strip() for m in _env(name, default).split(",") if m.strip()]


# Groq's free limits are per model, and everyone using the site shares one key. So each list below is
# several models in order of preference: when one hits a free limit, the next one takes over (see
# groq.py). Models this key can't use are skipped automatically. Comma-separated to override.
# Notes, merges and study extras: the best models first.
GROQ_MODELS = _models("GROQ_MODEL", ",".join([
    "openai/gpt-oss-120b",
    "moonshotai/kimi-k2-instruct-0905",
    "moonshotai/kimi-k2-instruct",
    "llama-3.3-70b-versatile",
    "meta-llama/llama-4-maverick-17b-128e-instruct",
    "qwen/qwen3-32b",
    "meta-llama/llama-4-scout-17b-16e-instruct",
    "openai/gpt-oss-20b",
]))
# Quick questions and the study helper chat: smaller models with their own limits, so they don't use
# up the notes models' allowance. The notes models are tried after these.
GROQ_FAST_MODELS = _models("GROQ_FAST_MODEL", ",".join([
    "openai/gpt-oss-20b",
    "meta-llama/llama-4-scout-17b-16e-instruct",
    "qwen/qwen3-32b",
    "llama-3.1-8b-instant",
]))
# How hard gpt-oss models "think" before answering: low is faster and uses far fewer tokens,
# and is plenty for notes. (Ignored by other models.)
GROQ_REASONING_EFFORT = _env("GROQ_REASONING_EFFORT", "low")
# Transcription: both Whisper models have their own audio allowance (per hour and per day).
GROQ_WHISPER_MODELS = _models("GROQ_WHISPER_MODEL", "whisper-large-v3-turbo,whisper-large-v3")
GROQ_WHISPER_MODEL = GROQ_WHISPER_MODELS[0] if GROQ_WHISPER_MODELS else "whisper-large-v3-turbo"

# "groq" = open-source Whisper hosted free by Groq (fast, works on Render's free tier)
# "local" = run Whisper on this server with faster-whisper (needs a real CPU + ~1 GB RAM)
TRANSCRIBE_BACKEND = _env("TRANSCRIBE_BACKEND", "groq").lower()
LOCAL_WHISPER_MODEL = _env("LOCAL_WHISPER_MODEL", "base")

# ⚠️ KEY — Notion internal integration secret (starts with "ntn_").
NOTION_TOKEN = _env("NOTION_TOKEN")
# ⚠️ ID — The Notion page new notes are created under (paste the page URL or its ID).
NOTION_PARENT_PAGE_ID = _env("NOTION_PARENT_PAGE_ID")

# People whose top-level Notion page is left off the site (comma-separated page titles).
# Their Notion isn't touched; they just don't show up in "Who is it for?" or the AI's guesses.
HIDDEN_PEOPLE = {n.strip().lower() for n in _env("HIDDEN_PEOPLE", "").split(",") if n.strip()}

# Optional shared password so strangers can't use up your free quotas. Empty = no password.
APP_PASSWORD = _env("APP_PASSWORD")

MAX_UPLOAD_MB = int(_env("MAX_UPLOAD_MB", "300"))
# Transcript characters sent to the LLM per request. ~4 chars per token, so 12000 ≈ 3k tokens,
# which keeps each request under Groq's free-tier tokens-per-minute limit.
SUMMARY_CHUNK_CHARS = int(_env("SUMMARY_CHUNK_CHARS", "12000"))
