"""Turn a transcript or a document's text (and optionally the lecture slides) into notes with an LLM on Groq.

1. Notes: title, summary, key points, key terms, action items. Long transcripts are summarised in parts
   and then merged, so no single request goes over Groq's free-tier tokens-per-minute limit.
2. Study extras the user picked (practice questions, flashcards, a quiz…). For a short transcript
   they're made in the same request as the notes; for a long one, in one extra request from the
   finished notes plus excerpts of the transcript.

Slides, when given, are added to the single-pass, merge and extras prompts as context.
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
"title": a short title saying what the lecture was about, at most 8 words
"summary": a 3-6 sentence summary of what was covered
"key_points": an array of the most important points, each one clear sentence (aim for 5-12)
"key_terms": an array of up to 12 objects {"term": "...", "definition": "..."} for the important terms, \
concepts or formulas, each defined in one sentence as it was used. Use an empty array if there are none.
"action_items": an array of concrete tasks, assignments, deadlines or follow-ups that were mentioned, \
written as instructions and including who/when if stated. Use an empty array if there are none."""

# Study extras the user can pick before recording. key -> (what to ask for, max items)
EXTRAS = {
    "practice_questions": ('an array of 3-6 objects {"q": "...", "a": "..."}: exam-style questions that test '
                           "understanding of the most important ideas, each with a short correct answer", 8),
    "flashcards": ('an array of 8-15 objects {"front": "...", "back": "..."}: short flashcards for memorising '
                   "facts, definitions and formulas (front: a cue or question; back: a brief answer)", 20),
    "quiz": ('an array of 4-6 objects {"question": "...", "options": ["...", "...", "...", "..."], "answer": <index 0-3 '
             'of the correct option>, "explanation": "..."}: multiple-choice questions with exactly 4 plausible '
             "options and one correct answer, plus a one-sentence explanation", 8),
    "cheat_sheet": ("an array of 5-12 short lines: the must-know formulas, rules, definitions and facts from the "
                    "lecture, written compactly like a one-page cheat sheet", 15),
    "explanations": ('an array of 2-4 objects {"topic": "...", "explanation": "..."}: the hardest ideas from the '
                     "lecture explained simply, in plain language, with an everyday analogy or example if helpful", 5),
}

SLIDES_NOTE = (
    "\n\nLECTURE SLIDES (extra context from the same lecture). The transcript is the main source; use the "
    "slides to get names, terms, formulas and the structure right, and to fix words the transcript "
    "misheard. Don't add slide content that wasn't covered in the recording.\n{slides}"
)
SINGLE_SLIDES_CHARS = 8000
MERGE_SLIDES_CHARS = 5000
EXTRAS_SLIDES_CHARS = 5000
EXTRAS_TRANSCRIPT_CHARS = 9000

DOCUMENT_NOTE = (
    "Note: the input below is the text of a document (e.g. a reading, handout or lecture notes), "
    "not a speech transcript. Treat \"transcript\" in these instructions as meaning that text.\n\n"
)

FULL_PROMPT = "Write notes for this transcript.\n\n{schema}\n\nTRANSCRIPT:\n{text}"
PART_PROMPT = (
    "This is part {i} of {n} of a longer transcript. Write notes for this part only.\n\n"
    "{schema}\n\nTRANSCRIPT PART {i}:\n{text}"
)
# Written while the lecture is still being recorded, a part at a time (see part_notes).
LIVE_PART_PROMPT = (
    "This is part {i} of a longer lecture transcript; more parts may follow. Write notes for this part "
    "only.\n\n{schema}\n\nTRANSCRIPT PART {i}:\n{text}"
)
MERGE_PROMPT = (
    "Below are notes written for consecutive parts of one recording, in order. Merge them into "
    "one set of notes for the whole recording: combine the summaries into one, and remove duplicate "
    "key points, key terms and action items.\n\n{schema}\n\nPART NOTES (JSON):\n{text}"
)
EXTRAS_PROMPT = (
    "Here are notes from a lecture and excerpts of its transcript. Make study material from them. "
    "Use only what was covered; never invent facts. If the recording has no teachable content (e.g. a "
    "short admin meeting), return empty arrays.\n\nReturn a JSON object with exactly these keys:\n{schema}"
    "\n\nNOTES:\n{notes}\n\nTRANSCRIPT EXCERPTS:\n{transcript}"
)


def _with_slides(prompt: str, slides: str, limit: int) -> str:
    return prompt + SLIDES_NOTE.format(slides=slides[:limit]) if slides.strip() else prompt


def part_notes(text: str, index: int, progress: Progress = lambda _: None) -> dict:
    """Notes for one part of a lecture that's still being recorded. Sent back with the full transcript
    at the end (make_notes `parts`), so only the last part and the merge are left to do then."""
    return _ask(LIVE_PART_PROMPT.format(i=index, schema=SCHEMA, text=text), progress)


def make_notes(transcript: str, progress: Progress, slides: str = "", extras: list[str] | None = None,
               warn: Progress = lambda _: None, source: str = "recording",
               parts: list[dict] | None = None, parts_chars: int = 0) -> dict:
    """`source` is "recording" (a transcript) or "document" (text read from a PDF, Word file…).
    `parts` are notes already written during the recording for transcript[:parts_chars]."""
    note = DOCUMENT_NOTE if source == "document" else ""
    wanted = [e for e in (extras or []) if e in EXTRAS]
    done = (parts, parts_chars) if parts and 0 < parts_chars <= len(transcript) else None
    notes = None
    if wanted and not done and len(split_text(transcript, config.SUMMARY_CHUNK_CHARS)) <= 1:
        # Short enough for one request: make the notes and the extras together (one AI call, not two).
        progress("Writing notes and study extras…")
        schema = SCHEMA + "\n" + "\n".join(f'"{key}": {EXTRAS[key][0]}' for key in wanted)
        prompt = FULL_PROMPT.format(schema=schema, text=transcript)
        try:
            data = _ask_json(note + _with_slides(prompt, slides, SINGLE_SLIDES_CHARS), progress)
            notes, wanted = {**_clean(data), **_clean_extras(data, wanted)}, []
        except AppError:
            pass  # try again the usual way: the notes first, then the extras on their own
    if notes is None:
        notes = _base_notes(transcript, progress, slides, note, done)
    if wanted:
        progress("Making study extras…")
        try:
            notes.update(_make_extras(notes, transcript, slides, wanted, progress, note))
        except AppError:  # the notes are fine without extras; don't fail the whole job
            warn("Couldn't make the study extras this time (the AI was busy). The notes are fine. "
                 "Try again later if you need the extras.")
    if source == "document":
        notes["source"] = "document"  # so the full text is labelled as such, here and in Notion
    return notes


def _base_notes(transcript: str, progress: Progress, slides: str, note: str = "",
                done: tuple[list[dict], int] | None = None) -> dict:
    limit = config.SUMMARY_CHUNK_CHARS
    if done:
        # Most of it was written during the lecture: only the rest of the transcript is left.
        partials, covered = [_clean(p) for p in done[0]], done[1]
        rest = split_text(transcript[covered:], limit) if len(transcript[covered:].split()) >= 30 else []
        for chunk in rest:
            progress("Writing notes for the end of the lecture…")
            partials.append(_ask(LIVE_PART_PROMPT.format(i=len(partials) + 1, schema=SCHEMA, text=chunk), progress))
    else:
        chunks = split_text(transcript, limit)
        if len(chunks) <= 1:
            progress("Writing notes…")
            prompt = FULL_PROMPT.format(schema=SCHEMA, text=transcript)
            return _ask(note + _with_slides(prompt, slides, SINGLE_SLIDES_CHARS), progress)

        partials = []
        for i, chunk in enumerate(chunks, 1):
            progress(f"Writing notes… part {i} of {len(chunks)}")
            partials.append(_ask(note + PART_PROMPT.format(i=i, n=len(chunks), schema=SCHEMA, text=chunk), progress))

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


def _excerpts(transcript: str, limit: int) -> str:
    """The whole transcript if it fits, otherwise slices from the start, middle and end."""
    if len(transcript) <= limit:
        return transcript
    third = limit // 3
    mid = len(transcript) // 2 - third // 2
    return "\n…\n".join([transcript[:third], transcript[mid:mid + third], transcript[-third:]])


def _make_extras(notes: dict, transcript: str, slides: str, wanted: list[str], progress: Progress,
                 note: str = "") -> dict:
    schema = "\n".join(f'"{key}": {EXTRAS[key][0]}' for key in wanted)
    prompt = EXTRAS_PROMPT.format(schema=schema, notes=json.dumps(notes, ensure_ascii=False),
                                  transcript=_excerpts(transcript, EXTRAS_TRANSCRIPT_CHARS))
    data = _ask_json(note + _with_slides(prompt, slides, EXTRAS_SLIDES_CHARS), progress)
    return _clean_extras(data, wanted)


def _ask_json(prompt: str, progress: Progress) -> dict:
    raw = groq.chat_json(SYSTEM, prompt, progress)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.S)
        if not match:
            raise AppError("The AI returned notes in an unexpected format. Try again.")
        return json.loads(match.group(0))


def _ask(prompt: str, progress: Progress) -> dict:
    return _clean(_ask_json(prompt, progress))


def _as_list(value) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v).strip()]


def _clean(data: dict) -> dict:
    return {
        "title": str(data.get("title") or "Notes").strip()[:150],
        "summary": str(data.get("summary") or "").strip(),
        "key_points": _as_list(data.get("key_points")),
        "key_terms": _pairs(data.get("key_terms"), "term", "definition", 15),
        "action_items": _as_list(data.get("action_items")),
    }


def _pairs(value, first: str, second: str, limit: int) -> list[dict]:
    if not isinstance(value, list):
        return []
    pairs = []
    for item in value:
        if isinstance(item, dict) and str(item.get(first, "")).strip() and str(item.get(second, "")).strip():
            pairs.append({first: str(item[first]).strip(), second: str(item[second]).strip()})
    return pairs[:limit]


def _quiz(value, limit: int) -> list[dict]:
    if not isinstance(value, list):
        return []
    quiz = []
    for item in value:
        if not isinstance(item, dict):
            continue
        options = _as_list(item.get("options"))[:4]
        try:
            answer = int(item.get("answer"))
        except (TypeError, ValueError):
            continue
        question = str(item.get("question") or "").strip()
        if question and len(options) == 4 and 0 <= answer < 4:
            quiz.append({"question": question, "options": options, "answer": answer,
                         "explanation": str(item.get("explanation") or "").strip()})
    return quiz[:limit]


def _clean_extras(data: dict, wanted: list[str]) -> dict:
    cleaners = {
        "practice_questions": lambda v, n: _pairs(v, "q", "a", n),
        "flashcards": lambda v, n: _pairs(v, "front", "back", n),
        "quiz": _quiz,
        "cheat_sheet": lambda v, n: _as_list(v)[:n],
        "explanations": lambda v, n: _pairs(v, "topic", "explanation", n),
    }
    return {key: cleaners[key](data.get(key), EXTRAS[key][1]) for key in wanted}
