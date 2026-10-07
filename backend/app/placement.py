"""Work out where in Notion a recorded lecture should go.

The user picks who it's for and which class; this finds the places that make sense in that
person's Notion (a lectures table linked to the class, the page where their other lectures for
the class live, the class page itself…), and the AI picks the best one.
"""

import json
import math
import re
import time
from datetime import date

from . import config, groq, notion
from .utils import AppError

# Tables whose entries are classes ("Courses", "Classes", or e.g. a template's "Domains").
COURSE_TABLE = re.compile(r"course|class|subject|module|domain|unit|semester", re.I)
# Pages/tables that hold lectures or class notes.
LECTURE_WORDS = re.compile(r"\blec\b|\blecs?\s*\d|lecture|\bweek\s*\d|\btut|\blab\b|class notes|\bnotes?\b|topics?", re.I)
# Tables and pages that mention classes but aren't where lecture notes belong.
NOT_LECTURES = re.compile(r"time\s*table|schedule|calend[ae]r|assess?ments?|\bexams?\b|deadlines?|"
                          r"\bgrades?\b|to.?dos?|\btasks?\b", re.I)
STOP_WORDS = {"for", "and", "the", "with", "intro", "introduction", "applications", "to", "of", "in", "a", "an"}
MAX_CANDIDATES = 6


def _by_id(nodes: list[dict]) -> dict[str, dict]:
    return {notion.normalize_id(n["id"]): n for n in nodes}


def _node(by_id: dict, node_id: str | None) -> dict | None:
    return by_id.get(notion.normalize_id(node_id or ""))


def _ancestors(by_id: dict, node: dict) -> list[dict]:
    chain, cur = [], _node(by_id, node.get("parent"))
    while cur and len(chain) < 30:
        chain.append(cur)
        cur = _node(by_id, cur.get("parent"))
    return chain


def _inside(by_id: dict, node: dict, person_id: str) -> bool:
    return any(notion.same_id(a["id"], person_id) for a in _ancestors(by_id, node))


def _path(by_id: dict, node: dict) -> str:
    return " › ".join(a["title"] for a in reversed(_ancestors(by_id, node)))


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z]+|\d{3,4}[a-z]?\d*", text.lower())
    return {w for w in words if (len(w) >= 3 or w.isdigit()) and w not in STOP_WORDS}


def _matches_class(title: str, tokens: set[str]) -> bool:
    if not tokens:
        return False
    hits = len(tokens & _tokens(title))
    return hits >= max(1, math.ceil(len(tokens) / 2)) or (hits and len(tokens) <= 2)


# ---------- who / which class ----------

def people() -> list[dict]:
    nodes = notion.page_tree()
    roots = [n for n in nodes if n["root"] and n["type"] == "page"
             and n["title"].strip().lower() not in config.HIDDEN_PEOPLE]
    return [{"id": n["id"], "title": n["title"], "icon": n["icon"]}
            for n in sorted(roots, key=lambda n: n["title"].lower())]


CLASSES_CACHE_SECONDS = 600
_classes_cache: dict[str, tuple[float, list[dict]]] = {}


def classes(person_id: str, note_title: str = "", summary: str = "") -> dict:
    """The classes in this person's Notion, plus the AI's guess at which one this lecture is for."""
    found = find_classes(person_id)
    return {"classes": found, "guess": _guess_class(found, note_title, summary)}


def find_classes(person_id: str) -> list[dict]:
    """Entries of the person's Courses/Classes table(s). Cached for a few minutes."""
    key = notion.normalize_id(person_id)
    cached = _classes_cache.get(key)
    if cached and time.time() - cached[0] < CLASSES_CACHE_SECONDS:
        return cached[1]
    nodes = notion.page_tree()
    by_id = _by_id(nodes)
    tables = [n for n in nodes if n["type"] == "database" and _inside(by_id, n, person_id)]

    found, seen = [], set()
    for table in tables[:25]:
        name = table["title"]
        if notion.is_generic_title(name):
            name = notion.data_source_label(table["id"]) or name
        if not COURSE_TABLE.search(name) or NOT_LECTURES.search(name):
            continue
        for entry in notion.table_entries(table["id"])[:80]:
            key = notion.normalize_id(entry["id"])
            if key in seen or notion.is_generic_title(entry["title"]):
                continue
            seen.add(key)
            found.append({"id": entry["id"], "title": entry["title"], "icon": entry["icon"], "group": name})
        if len(found) >= 120:
            break
    _classes_cache[key] = (time.time(), found)
    return found


def forget_cached() -> None:
    _classes_cache.clear()


def guess_owner(note_title: str, summary: str, usual_person_id: str = "") -> dict:
    """Guess whose lecture this is (and which class) by comparing what it's about with every
    person's classes, or, for people without a class list, the titles of their lecture pages."""
    everyone = people()
    if not everyone or not (note_title or summary):
        return {"person_id": None}
    nodes = notion.page_tree()
    by_id = _by_id(nodes)

    sections, class_index = [], []
    for p_num, person in enumerate(everyone):
        lines = [f"P{p_num}. {person['title']}"]
        for c in find_classes(person["id"])[:40]:
            lines.append(f"   C{len(class_index)}: {c['title']}")
            class_index.append((p_num, c))
        lectures = [n["title"] for n in nodes if n["type"] == "page" and _is_lecture_page(by_id, n)
                    and _inside(by_id, n, person["id"])][:12]
        if lectures:
            lines.append("   their lecture pages: " + "; ".join(lectures))
        if len(lines) == 1:
            lines.append("   (no classes or lecture pages found)")
        sections.append("\n".join(lines))

    usual = next((f"P{i}. {p['title']}" for i, p in enumerate(everyone)
                  if notion.same_id(p["id"], usual_person_id)), "")
    prompt = (
        f"Lecture title: {note_title}\nLecture summary: {summary[:1500]}\n\n"
        "These students share one Notion. Each has classes (C…) and/or lecture pages:\n"
        + "\n".join(sections) + "\n\n"
        + (f"This device usually sends notes for {usual}. Use that only to break a tie when more than "
           "one person has a matching class.\n" if usual else "")
        + "Whose lecture is this most likely, and for which class? Match the subject of the lecture to "
        "class names and course codes, and to the topics of their existing lecture pages. If the best "
        "person has no listed class that fits, give a short class name in class_name instead (e.g. a "
        "course code from their lecture pages like \"csc\").\n"
        'Reply as JSON: {"person": "P<number>", "class": "C<number>" or null, "class_name": "<text>" or null, '
        '"confident": true|false}'
    )
    try:
        data = json.loads(groq.chat_json(
            "You figure out which student and class a lecture recording belongs to. Reply with JSON only.",
            prompt, lambda _: None, fast=True))
        p_num = int(str(data.get("person", "")).lstrip("Pp"))
    except (AppError, ValueError, TypeError, json.JSONDecodeError):
        return {"person_id": None}
    if not 0 <= p_num < len(everyone) or data.get("confident") is False:
        return {"person_id": None}

    class_id, class_title = None, ""
    try:
        c_num = int(str(data.get("class") or "").lstrip("Cc"))
        if 0 <= c_num < len(class_index) and class_index[c_num][0] == p_num:
            class_id, class_title = class_index[c_num][1]["id"], class_index[c_num][1]["title"]
    except ValueError:
        pass
    class_name = "" if class_id else str(data.get("class_name") or "").strip()[:80]
    return {"person_id": everyone[p_num]["id"], "person_name": everyone[p_num]["title"],
            "class_id": class_id, "class_name": class_name, "class_title": class_title or class_name}


def _guess_class(found: list[dict], note_title: str, summary: str) -> str | None:
    if not found or not (note_title or summary):
        return None
    listing = "\n".join(f"{i}. {c['title']}" for i, c in enumerate(found))
    prompt = (f"Lecture title: {note_title}\nLecture summary: {summary[:1500]}\n\nThe student's classes:\n{listing}\n\n"
              'Which class is this lecture most likely from? Reply as JSON: {"index": <number>, '
              '"confident": true|false}. Use -1 if none of them fits.')
    try:
        data = json.loads(groq.chat_json(
            "You match lecture notes to the student's class. Reply with JSON only.", prompt, lambda _: None, fast=True))
        index = int(data.get("index", -1))
    except (AppError, ValueError, TypeError, json.JSONDecodeError):
        return None
    return found[index]["id"] if 0 <= index < len(found) and data.get("confident", True) else None


def _is_lecture_page(by_id: dict, node: dict) -> bool:
    return bool(LECTURE_WORDS.search(node["title"])) and not _not_for_lectures(by_id, node)


def _not_for_lectures(by_id: dict, node: dict) -> bool:
    """A timetable row, an assessment, an exam calendar entry… (or the table itself)."""
    parent = _node(by_id, node.get("parent"))
    return bool(NOT_LECTURES.search(node["title"])
                or (parent and parent["type"] == "database" and NOT_LECTURES.search(parent["title"])))


# ---------- where should it go ----------

def plan(person_id: str, class_id: str | None, class_text: str, note_title: str, summary: str) -> dict:
    nodes = notion.page_tree()
    by_id = _by_id(nodes)
    person = _node(by_id, person_id)
    if not person:
        raise AppError("Couldn't find that person's page in Notion. Try refreshing.")
    mine = [n for n in nodes if _inside(by_id, n, person_id)]

    class_node = _node(by_id, class_id)
    class_title = (class_node["title"] if class_node else "") or class_text.strip()
    tokens = _tokens(class_title)
    course_table = notion.page_parent_table(class_id) if class_id else None

    candidates: dict[tuple, dict] = {}

    def add(kind: str, target: dict, score: float, label: str, examples: list[str], link_to: str | None = None):
        key = (kind, notion.normalize_id(target["id"]))
        if key not in candidates or candidates[key]["score"] < score:
            candidates[key] = {
                "kind": kind, "target_id": target["id"], "link_to": link_to, "score": score, "label": label,
                "where": " › ".join(filter(None, [_path(by_id, target), target["title"]])),
                "examples": examples[:6],
            }

    # 1. A lectures table that links to the class (e.g. Topics, with a "domain" column -> Domains).
    if class_id and course_table:
        tables = [n for n in mine if n["type"] == "database" and LECTURE_WORDS.search(n["title"])
                  and not NOT_LECTURES.search(n["title"])]
        for table in tables[:10]:
            info = notion.table_info(table["id"])
            column = info and notion.link_column(info, course_table)
            if not column:
                continue
            existing = notion.linked_entries(info, column, class_id)
            extras = ", ".join(filter(None, [f"{info['kind'][0]}: {info['kind'][2]}" if info["kind"] else "",
                                              "dated today" if info["date_prop"] else ""]))
            label = f"New lecture in {info['title']}, linked to {class_title}" + (f" ({extras})" if extras else "")
            add("entry", table, 100 + len(existing), label, [e["title"] for e in existing], link_to=class_id)
            if existing:
                add("append", existing[0], 12, f"Add to your latest {class_title} lecture: “{existing[0]['title']}”", [])

    # 2. Wherever this person's other lectures for the class already live.
    groups: dict[str, list[dict]] = {}
    for page in mine:
        if page["type"] == "page" and page["parent"] and not _not_for_lectures(by_id, page) \
                and (_matches_class(page["title"], tokens) or LECTURE_WORDS.search(page["title"])):
            groups.setdefault(notion.normalize_id(page["parent"]), []).append(page)
    for parent_key, pages in groups.items():
        parent = by_id.get(parent_key)
        # Skip the class page itself and the classes table (its entries are classes, not lectures).
        if not parent or notion.same_id(parent["id"], class_id) or notion.same_id(parent["id"], course_table):
            continue
        same_class = [p for p in pages if _matches_class(p["title"], tokens)]
        score = 40 + 12 * len(same_class) + 2 * len(pages)
        lectures_here = [p for p in pages if LECTURE_WORDS.search(p["title"])]
        if notion.same_id(parent["id"], person_id) and len(lectures_here) >= 3:
            # They keep their lectures right on their own page (e.g. Ryan's "csc lec 3", "mgm tut 1"…):
            # that's their spot, as clear a choice as a lectures table linked to the class.
            score = 100 + len(lectures_here)
        if parent["type"] == "database":
            info = notion.table_info(parent["id"])
            if not info:
                continue
            linked = bool(course_table and notion.link_column(info, course_table))
            label = f"New entry in {parent['title']}, next to your other lectures" + \
                    (f", linked to {class_title}" if linked else "")
            add("entry", parent, score, label, [p["title"] for p in same_class or pages],
                link_to=class_id if linked else None)
        else:
            add("page", parent, score, f"New page in {parent['title']}, next to your other lectures",
                [p["title"] for p in same_class or pages])

    # 3. Inside the class page itself. When the class is a card in a Courses table and there's no
    #    lectures table, that's where the class's notes live, so it beats loose pages elsewhere.
    if class_node and class_node["type"] == "page":
        inside = [n["title"] for n in mine if notion.same_id(n.get("parent"), class_id)]
        base = 60 if course_table else 30
        add("page", class_node, base + 3 * len(inside), f"New page inside {class_title}", inside)

    # 4. Last resort: the person's own page (or the site's default page).
    add("page", person, 1, f"New page in {person['title']}", [])
    default = _node(by_id, notion.parent_page_id())
    if default:
        add("page", default, 0, f"New page in {default['title']}", [])

    ranked = sorted(candidates.values(), key=lambda c: -c["score"])[:MAX_CANDIDATES]
    best, reason = _choose(ranked, person["title"], class_title, note_title, summary)
    for c in ranked:
        c.pop("score")
    return {"candidates": ranked, "best": best, "reason": reason}


def _obvious(ranked: list[dict]) -> bool:
    """True when the heuristic's top place clearly beats the rest, so asking the AI is a waste."""
    if len(ranked) < 2:
        return True
    top, second = ranked[0]["score"], ranked[1]["score"]
    return (top >= 100 > second) or top >= 1.5 * second + 10


def _choose(ranked: list[dict], person: str, class_title: str, note_title: str, summary: str):
    """Pick the best place: the obvious one, or ask the AI when it's close."""
    if _obvious(ranked):
        return 0, ""
    listing = "\n".join(
        f"{i}. {c['label']} — at: {c['where']}"
        + (f" — existing titles there: {'; '.join(c['examples'])}" if c["examples"] else "")
        for i, c in enumerate(ranked))
    prompt = (
        f"A lecture was recorded for {person}" + (f", class: {class_title}" if class_title else "") + ".\n"
        f"Today is {date.today():%A, %B %d, %Y}.\nLecture title from the notes: {note_title}\n"
        f"Summary: {summary[:800]}\n\nPlaces it could be saved (listed best-first by a heuristic):\n{listing}\n\n"
        "Pick where it should go. Rules:\n"
        "- It's a lecture recording, so it belongs with this person's other lectures for this class.\n"
        "- A lectures/topics table linked to the class is usually best.\n"
        "- Never pick a place that belongs to a different class.\n"
        "- Only pick 'Add to your latest … lecture' if the notes are clearly a continuation of it.\n"
        'Reply as JSON: {"choice": <number>, "reason": "<one short sentence>"}'
    )
    try:
        data = json.loads(groq.chat_json(
            "You file lecture notes into the right place in a student's Notion. Reply with JSON only.",
            prompt, lambda _: None, fast=True))
        choice = int(data.get("choice", 0))
        reason = str(data.get("reason") or "").strip()[:200]
    except (AppError, ValueError, TypeError, json.JSONDecodeError):
        choice, reason = 0, ""
    if not 0 <= choice < len(ranked):
        choice = 0
    return choice, reason


def send(place: dict, title: str, notes: dict, transcript: str, day: str | None) -> str:
    kind, target = place.get("kind"), place.get("target_id") or ""
    if kind == "entry":
        return notion.create_entry(target, title, notes, transcript, day, link_to=place.get("link_to"))
    if kind == "page":
        return notion.create_page(title, notes, transcript, target)
    if kind == "append":
        return notion.append_to_page(target, title, notes, transcript)
    raise AppError("Pick where to save the notes first.")


# ---------- all at once (while the notes are being written) ----------

def excerpt(transcript: str, limit: int = 1500) -> str:
    """The start and the latest part of a transcript: enough to tell what the lecture is about."""
    if len(transcript) <= limit:
        return transcript
    head = limit // 3
    return transcript[:head] + " … " + transcript[-(limit - head):]


def prepare(transcript: str, usual_person_id: str = "") -> dict | None:
    """Guess whose lecture it is and which class, and plan where it goes, from the transcript alone.
    Returns {"person_id", "class_id", "class_name", "plan", "plan_for"} or None if it can't tell."""
    about = excerpt(transcript)
    guess = guess_owner("", about, usual_person_id)
    if not guess.get("person_id"):
        return None
    result = {**guess, "plan": None}
    if guess.get("class_id") or guess.get("class_name"):
        result["plan"] = plan(guess["person_id"], guess.get("class_id"), guess.get("class_name") or "", "", about)
        result["plan_for"] = {"person_id": guess["person_id"], "class_id": guess.get("class_id"),
                              "class_text": "" if guess.get("class_id") else guess.get("class_name") or ""}
    return result
