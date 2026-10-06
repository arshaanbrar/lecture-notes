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
GROQ_MODEL = _env("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_WHISPER_MODEL = _env("GROQ_WHISPER_MODEL", "whisper-large-v3-turbo")

# "groq" = open-source Whisper hosted free by Groq (fast, works on Render's free tier)
# "local" = run Whisper on this server with faster-whisper (needs a real CPU + ~1 GB RAM)
TRANSCRIBE_BACKEND = _env("TRANSCRIBE_BACKEND", "groq").lower()
LOCAL_WHISPER_MODEL = _env("LOCAL_WHISPER_MODEL", "base")

# ⚠️ KEY — Notion internal integration secret (starts with "ntn_").
NOTION_TOKEN = _env("NOTION_TOKEN")
# ⚠️ ID — The Notion page new notes are created under (paste the page URL or its ID).
NOTION_PARENT_PAGE_ID = _env("NOTION_PARENT_PAGE_ID")

# Optional shared password so strangers can't use up your free quotas. Empty = no password.
APP_PASSWORD = _env("APP_PASSWORD")

MAX_UPLOAD_MB = int(_env("MAX_UPLOAD_MB", "300"))
# Transcript characters sent to the LLM per request. ~4 chars per token, so 12000 ≈ 3k tokens,
# which keeps each request under Groq's free-tier tokens-per-minute limit.
SUMMARY_CHUNK_CHARS = int(_env("SUMMARY_CHUNK_CHARS", "12000"))
