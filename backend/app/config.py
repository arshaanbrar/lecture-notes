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
# Models to try for notes, in order (comma-separated). The first one your Groq account can use is
# kept, so a retired or paid-only model is skipped automatically.
GROQ_MODELS = [m.strip() for m in _env(
    "GROQ_MODEL",
    "llama-3.3-70b-versatile,llama-3.1-8b-instant,openai/gpt-oss-120b,openai/gpt-oss-20b",
).split(",") if m.strip()]
# Small, fast models for quick questions (whose lecture is this, which class, where in Notion).
# They have their own free-tier limits, so these don't use up the notes model's. Falls back to GROQ_MODEL.
GROQ_FAST_MODELS = [m.strip() for m in _env(
    "GROQ_FAST_MODEL", "openai/gpt-oss-20b,llama-3.1-8b-instant").split(",") if m.strip()]
# How hard gpt-oss models "think" before answering: low is faster and uses far fewer tokens,
# and is plenty for notes. (Ignored by other models.)
GROQ_REASONING_EFFORT = _env("GROQ_REASONING_EFFORT", "low")
GROQ_WHISPER_MODEL = _env("GROQ_WHISPER_MODEL", "whisper-large-v3-turbo")

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
