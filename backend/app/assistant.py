"""The study helper chat: answers questions about the lecture, during or after it."""

from pathlib import Path

from . import audio, groq, transcribe
from .utils import AppError

# Kept small: every question resends this, and Groq's free limits are counted in tokens. ~9,000
# characters is the last ~10 minutes of a lecture; after it, the notes carry most of what matters.
TRANSCRIPT_CHARS = 9_000
MAX_TURNS = 12  # earlier messages are dropped to keep each request small

SYSTEM = """You are a friendly study helper for a university student, available during and after a lecture.

{context}

How to answer:
- Answer the student's question directly, in plain language, short enough to read in class (a few \
sentences or a short list). Go longer only if they ask for detail.
- Base your answer on the lecture first. If you add something the lecture didn't cover, say so briefly \
(e.g. "Beyond what was said in class: …").
- If the transcript seems garbled or is missing what they ask about, say so and help from general knowledge.
- Use examples and simple analogies when explaining a confusing idea. Don't make up what the professor said."""


def _lecture_context(context: dict) -> str:
    live = context.get("live")
    transcript = (context.get("transcript") or "").strip()
    if live:
        if not transcript:
            return ("The lecture is being recorded right now, but nothing has been transcribed yet. Help with "
                    "general knowledge, and mention that you'll know what was said once there's more recording.")
        # Mid-lecture the most recent part matters most.
        recent = transcript[-TRANSCRIPT_CHARS:]
        return ("The lecture is being recorded RIGHT NOW. Here is what has been said so far (most recent at the end; "
                f"\"the last few minutes\" means the end of this):\nTRANSCRIPT SO FAR:\n{recent}")
    if not transcript and not context.get("summary"):
        return "No lecture is open, so help as a general study tutor."
    parts = [f"LECTURE: {context.get('title') or 'Untitled'}"]
    if context.get("summary"):
        parts.append(f"SUMMARY: {context['summary']}")
    if context.get("key_points"):
        parts.append("KEY POINTS:\n" + "\n".join(f"- {p}" for p in context["key_points"][:20]))
    if context.get("key_terms"):
        parts.append("KEY TERMS:\n" + "\n".join(f"- {t['term']}: {t['definition']}" for t in context["key_terms"][:20]))
    if transcript:
        if len(transcript) > TRANSCRIPT_CHARS:
            half = TRANSCRIPT_CHARS // 2
            transcript = transcript[:half] + "\n…\n" + transcript[-half:]
        parts.append(f"TRANSCRIPT:\n{transcript}")
    return "The lecture has finished. Here it is:\n" + "\n\n".join(parts)


def answer(messages: list[dict], context: dict) -> str:
    turns = [m for m in messages if m.get("role") in ("user", "assistant") and str(m.get("content", "")).strip()]
    turns = turns[-MAX_TURNS:]
    if not turns or turns[-1]["role"] != "user":
        raise AppError("Type a question first.")
    system = SYSTEM.format(context=_lecture_context(context))
    # The quick models answer chat: they have their own free limits, so questions don't use up the notes'.
    reply = groq.chat_text(system, [{"role": m["role"], "content": str(m["content"])[:4000]} for m in turns], fast=True)
    return reply or "Sorry, I couldn't come up with an answer. Try asking another way."


def transcribe_snippet(path: Path, workdir: Path, skip_seconds: float = 0) -> str:
    """Transcribe the audio recorded since the last question, so the helper knows what was just said.
    `skip_seconds` drops the recording's first piece, which is resent only for its file header."""
    clean = audio.normalize(path, workdir, skip_seconds)
    return transcribe.transcribe(clean, workdir, lambda _: None)
