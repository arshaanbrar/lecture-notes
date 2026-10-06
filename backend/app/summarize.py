"""Turn a transcript into notes with Llama 3 on Groq.

Long transcripts are summarised in parts and then merged, so no single request goes over
Groq's free-tier tokens-per-minute limit.
"""

import json
import re

from . import config, groq
from .groq import Progress
from .utils import AppError, split_text

SYSTEM = (
    "You are an expert note-taker. You turn raw speech-to-text transcripts of lectures and "
    "meetings into clear, accurate notes. Transcripts may contain recognition errors; fix obvious "
    "ones from context. Use only information from the input and never invent details. "
    "Always reply with a single JSON object and nothing else."
)

SCHEMA = """Return a JSON object with exactly these keys:
"title": a short descriptive title, at most 8 words
"summary": a 3-6 sentence summary of what was covered
"key_points": an array of the most important points, each one clear sentence (aim for 5-12)
"action_items": an array of concrete tasks, assignments, deadlines or follow-ups that were mentioned, \
written as instructions and including who/when if stated. Use an empty array if there are none."""

FULL_PROMPT = "Write notes for this transcript.\n\n{schema}\n\nTRANSCRIPT:\n{text}"
PART_PROMPT = (
    "This is part {i} of {n} of a longer transcript. Write notes for this part only.\n\n"
    "{schema}\n\nTRANSCRIPT PART {i}:\n{text}"
)
MERGE_PROMPT = (
    "Below are notes written for consecutive parts of one recording, in order. Merge them into "
    "one set of notes for the whole recording: combine the summaries into one, and remove "
    "duplicate key points and action items.\n\n{schema}\n\nPART NOTES (JSON):\n{text}"
)


def make_notes(transcript: str, progress: Progress) -> dict:
    limit = config.SUMMARY_CHUNK_CHARS
    chunks = split_text(transcript, limit)
    if len(chunks) <= 1:
        progress("Writing notes…")
        return _ask(FULL_PROMPT.format(schema=SCHEMA, text=transcript), progress)

    partials = []
    for i, chunk in enumerate(chunks, 1):
        progress(f"Writing notes… part {i} of {len(chunks)}")
        partials.append(_ask(PART_PROMPT.format(i=i, n=len(chunks), schema=SCHEMA, text=chunk), progress))

    progress("Combining notes…")
    # If the part notes are themselves too long for one request, merge them in groups first.
    while len(json.dumps(partials)) > limit and len(partials) > 1:
        groups = _group(partials, limit)
        if len(groups) == len(partials):
            break
        partials = [_merge(g, progress) for g in groups]
    return _merge(partials, progress)


def _merge(partials: list[dict], progress: Progress) -> dict:
    return _ask(MERGE_PROMPT.format(schema=SCHEMA, text=json.dumps(partials, ensure_ascii=False)), progress)


def _group(items: list[dict], limit: int) -> list[list[dict]]:
    groups, current, size = [], [], 0
    for item in items:
        n = len(json.dumps(item))
        if current and size + n > limit:
            groups.append(current)
            current, size = [], 0
        current.append(item)
        size += n
    if current:
        groups.append(current)
    return groups


def _ask(prompt: str, progress: Progress) -> dict:
    raw = groq.chat_json(SYSTEM, prompt, progress)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.S)
        if not match:
            raise AppError("The AI returned notes in an unexpected format. Try again.")
        data = json.loads(match.group(0))
    return _clean(data)


def _clean(data: dict) -> dict:
    def as_list(value) -> list[str]:
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            return []
        return [str(v).strip() for v in value if str(v).strip()]

    return {
        "title": str(data.get("title") or "Notes").strip()[:150],
        "summary": str(data.get("summary") or "").strip(),
        "key_points": as_list(data.get("key_points")),
        "action_items": as_list(data.get("action_items")),
    }
