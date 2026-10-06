"""Turn a transcript (and optionally the lecture slides) into notes with an LLM on Groq.

Long transcripts are summarised in parts and then merged, so no single request goes over
Groq's free-tier tokens-per-minute limit. Slides, when given, are used in the single-pass and
final merge prompts to get terms, formulas and structure right.
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
written as instructions and including who/when if stated. Use an empty array if there are none.
"practice_questions": an array of 3-6 objects {"q": "...", "a": "..."}: exam-style questions that test \
understanding of the most important ideas, each with a short correct answer taken from the material. \
Use an empty array if the recording has no teachable content (e.g. a short admin meeting).
"key_terms": an array of up to 12 objects {"term": "...", "definition": "..."} for the important terms, \
concepts or formulas, each defined in one sentence as they were used. Use an empty array if there are none."""

SLIDES_NOTE = (
    "\n\nLECTURE SLIDES (extra context from the same lecture). The transcript is the main source; use the "
    "slides to get names, terms, formulas and the structure right, and to fix words the transcript "
    "misheard. Don't add slide content that wasn't covered in the recording.\n{slides}"
)
SINGLE_SLIDES_CHARS = 8000
MERGE_SLIDES_CHARS = 5000

FULL_PROMPT = "Write notes for this transcript.\n\n{schema}\n\nTRANSCRIPT:\n{text}"
PART_PROMPT = (
    "This is part {i} of {n} of a longer transcript. Write notes for this part only.\n\n"
    "{schema}\n\nTRANSCRIPT PART {i}:\n{text}"
)
MERGE_PROMPT = (
    "Below are notes written for consecutive parts of one recording, in order. Merge them into "
    "one set of notes for the whole recording: combine the summaries into one, remove duplicate "
    "key points, action items and key terms, and keep the 3-6 best practice questions."
    "\n\n{schema}\n\nPART NOTES (JSON):\n{text}"
)


def _with_slides(prompt: str, slides: str, limit: int) -> str:
    return prompt + SLIDES_NOTE.format(slides=slides[:limit]) if slides.strip() else prompt


def make_notes(transcript: str, progress: Progress, slides: str = "") -> dict:
    limit = config.SUMMARY_CHUNK_CHARS
    chunks = split_text(transcript, limit)
    if len(chunks) <= 1:
        progress("Writing notes…")
        prompt = FULL_PROMPT.format(schema=SCHEMA, text=transcript)
        return _ask(_with_slides(prompt, slides, SINGLE_SLIDES_CHARS), progress)

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
    return _merge(partials, progress, slides)


def _merge(partials: list[dict], progress: Progress, slides: str = "") -> dict:
    prompt = MERGE_PROMPT.format(schema=SCHEMA, text=json.dumps(partials, ensure_ascii=False))
    return _ask(_with_slides(prompt, slides, MERGE_SLIDES_CHARS), progress)


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

    def as_pairs(value, first: str, second: str, limit: int) -> list[dict]:
        if not isinstance(value, list):
            return []
        pairs = []
        for item in value:
            if isinstance(item, dict) and str(item.get(first, "")).strip() and str(item.get(second, "")).strip():
                pairs.append({first: str(item[first]).strip(), second: str(item[second]).strip()})
        return pairs[:limit]

    return {
        "title": str(data.get("title") or "Notes").strip()[:150],
        "summary": str(data.get("summary") or "").strip(),
        "key_points": as_list(data.get("key_points")),
        "action_items": as_list(data.get("action_items")),
        "practice_questions": as_pairs(data.get("practice_questions"), "q", "a", 8),
        "key_terms": as_pairs(data.get("key_terms"), "term", "definition", 15),
    }
