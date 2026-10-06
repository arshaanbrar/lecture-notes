"""Notion API helpers, using one shared internal integration (set up once by the site owner)."""

import re
import time
from datetime import date

import httpx

from . import config
from .utils import AppError, split_text

API = "https://api.notion.com/v1"
VERSION = "2022-06-28"
# Newer API version, used only to look inside tables. It understands "data sources", so it can
# name linked views (e.g. a "Courses" gallery that shows another table).
DATA_SOURCES_VERSION = "2025-09-03"
MAX_BLOCKS_PER_REQUEST = 100
CACHE_SECONDS = 60


def is_configured() -> bool:
    return bool(config.NOTION_TOKEN)


def parent_page_id() -> str:
    """Optional default page for notes when nothing better is found."""
    return normalize_id(config.NOTION_PARENT_PAGE_ID)


def normalize_id(value: str) -> str:
    """Accept a raw ID or a full Notion URL and return the 32-char page ID."""
    hexes = re.findall(r"[0-9a-f]{32}", (value or "").replace("-", "").lower())
    return hexes[-1] if hexes else ""


def same_id(a: str | None, b: str | None) -> bool:
    return bool(a and b) and normalize_id(a) == normalize_id(b)


def _request(method: str, path: str, body: dict | None = None, version: str = VERSION) -> dict:
    if not config.NOTION_TOKEN:
        raise AppError("Notion isn't connected yet. The site owner needs to set NOTION_TOKEN (see README).")
    headers = {
        "Authorization": f"Bearer {config.NOTION_TOKEN}",
        "Notion-Version": version,
        "Content-Type": "application/json",
    }
    for attempt in range(5):
        try:
            resp = httpx.request(method, API + path, headers=headers, json=body, timeout=30)
        except httpx.TransportError:
            time.sleep(1.0 + attempt)
            continue
        if resp.status_code == 429 or resp.status_code >= 500:
            time.sleep(float(resp.headers.get("retry-after", 1.0 + attempt)))
            continue
        if resp.status_code >= 400:
            try:
                msg = resp.json().get("message", resp.text)
            except ValueError:
                msg = resp.text
            if resp.status_code == 404:
                msg += " — make sure the page is shared with your integration (••• → Connections)."
            raise AppError(f"Notion error: {msg}")
        return resp.json()
    raise AppError("Notion is busy right now. Try again in a minute.")


def _title(obj: dict) -> str:
    if obj.get("object") == "database":
        parts = obj.get("title", [])
    else:
        parts = next((p.get("title", []) for p in obj.get("properties", {}).values() if p.get("type") == "title"), [])
    return "".join(t.get("plain_text", "") for t in parts).strip() or "Untitled"


def _emoji(obj: dict) -> str:
    icon = obj.get("icon") or {}
    return icon.get("emoji", "") if icon.get("type") == "emoji" else ""


# ---------- everything shared with the integration ----------

MAX_SEARCH_REQUESTS = 30  # 100 results each → up to 3000 pages/tables

_tree_cache: dict = {"at": 0.0, "nodes": None}
_block_parents: dict[str, str | None] = {}


def _raw_parent(obj: dict) -> tuple[str, str | None]:
    parent = obj.get("parent") or {}
    kind = parent.get("type", "workspace")
    return kind, parent.get(kind) if kind != "workspace" else None


def _resolve_block_parent(block_id: str) -> str | None:
    """Pages inside columns/toggles have a block as parent; walk up to the page that holds it."""
    if block_id in _block_parents:
        return _block_parents[block_id]
    result, current = None, block_id
    for _ in range(6):
        try:
            kind, pid = _raw_parent(_request("GET", f"/blocks/{current}"))
        except AppError:
            break
        if kind != "block_id":
            result = pid
            break
        current = pid
    _block_parents[block_id] = result
    return result


def page_tree(refresh: bool = False) -> list[dict]:
    """Every page/table shared with the integration, with its parent's ID (None = top level).
    `root` marks pages at the very top of the workspace (e.g. one page per person)."""
    if not refresh and _tree_cache["nodes"] is not None and time.time() - _tree_cache["at"] < CACHE_SECONDS:
        return _tree_cache["nodes"]

    results, cursor = [], None
    for _ in range(MAX_SEARCH_REQUESTS):
        body = {"page_size": 100, **({"start_cursor": cursor} if cursor else {})}
        data = _request("POST", "/search", body)
        results += data.get("results", [])
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")

    nodes = {}
    for obj in results:
        if obj.get("object") not in ("page", "database") or obj.get("archived") or obj.get("in_trash"):
            continue
        kind, parent = _raw_parent(obj)
        if kind == "block_id":
            parent = _resolve_block_parent(parent)
        nodes[obj["id"]] = {
            "id": obj["id"],
            "type": obj["object"],
            "title": _title(obj),
            "icon": _emoji(obj),
            "parent": parent,
            "root": kind == "workspace",
        }
    # A page whose parent the integration can't see is treated as top level.
    for node in nodes.values():
        if node["parent"] not in nodes:
            node["parent"] = None

    _tree_cache.update(at=time.time(), nodes=list(nodes.values()))
    return _tree_cache["nodes"]


def forget_cached() -> None:
    _tree_cache["nodes"] = None
    _table_cache.clear()


# ---------- tables ----------

GENERIC_TITLES = {"", "untitled", "untitled table", "untitled database", "new database", "new table"}

_sources_cache: dict[str, list[dict]] = {}
_table_cache: dict[str, tuple[float, dict | None]] = {}


def is_generic_title(title: str) -> bool:
    t = title.strip().lower()
    return t in GENERIC_TITLES or t.startswith("view of ")


def _data_sources(database_id: str) -> list[dict]:
    """The table(s) a database block shows. A linked view's source is another table."""
    if database_id not in _sources_cache:
        try:
            db = _request("GET", f"/databases/{database_id}", version=DATA_SOURCES_VERSION)
            sources = list(db.get("data_sources", []))
            # A linked view is often titled "View of <source table>".
            title = "".join(t.get("plain_text", "") for t in db.get("title", [])).strip()
            if title.lower().startswith("view of "):
                sources.append({"id": "", "name": title[8:].strip()})
            _sources_cache[database_id] = sources
        except AppError:
            _sources_cache[database_id] = []
    return _sources_cache[database_id]


def data_source_label(database_id: str) -> str:
    names = [s.get("name", "").strip() for s in _data_sources(database_id)]
    names = [n for n in names if not is_generic_title(n) and not n.lower().startswith("new data source")]
    return " + ".join(names)


def _query_pages(path: str, version: str, body: dict | None = None, limit: int = 500) -> list[dict]:
    found, cursor = [], None
    while len(found) < limit:
        req = {"page_size": 100, **(body or {}), **({"start_cursor": cursor} if cursor else {})}
        data = _request("POST", path, req, version=version)
        for page in data.get("results", []):
            if page.get("object") != "page" or page.get("in_trash") or page.get("archived"):
                continue
            found.append({"id": page["id"], "type": "page", "title": _title(page), "icon": _emoji(page)})
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return found[:limit]


def table_entries(database_id: str) -> list[dict]:
    """The entries (pages) of a table. Tries the newer data-source API, the search index and
    the old table query, since linked views can't always be opened directly."""
    pages, seen = [], set()
    sources = _data_sources(database_id)
    for source in [s for s in sources if s.get("id")][:4]:
        try:
            for page in _query_pages(f"/data_sources/{source['id']}/query", DATA_SOURCES_VERSION):
                if page["id"] not in seen:
                    seen.add(page["id"])
                    pages.append(page)
        except AppError:
            continue
    if pages:
        return pages
    try:
        return _query_pages(f"/databases/{database_id}/query", VERSION)
    except AppError:
        return []


def table_info(table_id: str) -> dict | None:
    """The columns of a table we can add entries to, or None if Notion won't let us read it."""
    key = normalize_id(table_id)
    cached = _table_cache.get(key)
    if cached and time.time() - cached[0] < CACHE_SECONDS * 10:
        return cached[1]
    info = None
    try:
        db = _request("GET", f"/databases/{key}")
        props = db.get("properties", {})
        title_prop = next((n for n, p in props.items() if p.get("type") == "title"), None)
        if title_prop:
            kind = None  # a select/multi-select column with a "lecture" option, e.g. type: lecture
            for name, p in props.items():
                if p.get("type") in ("select", "multi_select"):
                    option = next((o["name"] for o in p[p["type"]].get("options", [])
                                   if o["name"].strip().lower() in ("lecture", "lectures", "lec", "class")), None)
                    if option:
                        kind = (name, p["type"], option)
                        break
            dates = [n for n, p in props.items() if p.get("type") == "date"]
            preferred = [n for n in dates if n.strip().lower() == "date"] + \
                        [n for n in dates if "due" not in n.lower()] + dates
            info = {
                "id": db["id"],
                "title": _title(db),
                "title_prop": title_prop,
                "relations": {n: p["relation"].get("database_id") for n, p in props.items()
                              if p.get("type") == "relation" and p["relation"].get("database_id")},
                "kind": kind,
                "date_prop": preferred[0] if preferred else None,
            }
    except AppError:
        info = None
    _table_cache[key] = (time.time(), info)
    return info


def link_column(info: dict, table_id: str | None) -> str | None:
    """The column in `info`'s table that links to `table_id` (e.g. Topics.domain -> Domains)."""
    return next((n for n, target in info["relations"].items() if same_id(target, table_id)), None)


def page_parent_table(page_id: str) -> str | None:
    """The table a page is an entry of, if any."""
    try:
        kind, parent = _raw_parent(_request("GET", f"/pages/{normalize_id(page_id)}"))
    except AppError:
        return None
    return parent if kind == "database_id" else None


def linked_entries(info: dict, column: str, page_id: str, limit: int = 8) -> list[dict]:
    """Entries of a table that link to `page_id`, newest first (e.g. a course's lectures)."""
    body = {"filter": {"property": column, "relation": {"contains": normalize_id(page_id)}}}
    if info["date_prop"]:
        body["sorts"] = [{"property": info["date_prop"], "direction": "descending"}]
    try:
        return _query_pages(f"/databases/{normalize_id(info['id'])}/query", VERSION, body, limit)
    except AppError:
        return []


# ---------- writing ----------

def _rich_text(text: str) -> list[dict]:
    # Notion caps each text object at 2000 chars; a block can hold up to 100 of them.
    return [{"type": "text", "text": {"content": piece}} for piece in split_text(text, 2000)][:100]


def _bold_then(bold: str, rest: str) -> list[dict]:
    """Rich text: a bold lead-in followed by normal text, e.g. "Term — definition"."""
    text = [{"type": "text", "text": {"content": bold[:2000]}, "annotations": {"bold": True}}]
    if rest:
        text.append({"type": "text", "text": {"content": rest[:2000]}})
    return text


def _block(kind: str, text: str, **extra) -> dict:
    return {"object": "block", "type": kind, kind: {"rich_text": _rich_text(text), **extra}}


def build_blocks(notes: dict, heading: str | None = None) -> list[dict]:
    """The notes as Notion blocks (the transcript is added separately, inside a toggle)."""
    blocks: list[dict] = []
    if heading:
        blocks.append(_block("heading_1", heading))
    blocks.append(_block("paragraph", f"Generated {date.today():%B %d, %Y}"))

    blocks.append(_block("heading_2", "Summary"))
    blocks.append(_block("paragraph", notes.get("summary") or "—"))

    blocks.append(_block("heading_2", "Key points"))
    points = notes.get("key_points") or []
    blocks += [_block("bulleted_list_item", p) for p in points] or [_block("paragraph", "—")]

    terms = notes.get("key_terms") or []
    if terms:
        blocks.append(_block("heading_2", "Key terms"))
        for item in terms:
            blocks.append({"object": "block", "type": "bulleted_list_item",
                           "bulleted_list_item": {"rich_text": _bold_then(item["term"], f" — {item['definition']}")}})

    blocks.append(_block("heading_2", "Action items"))
    actions = notes.get("action_items") or []
    blocks += [_block("to_do", a, checked=False) for a in actions] or [_block("paragraph", "None mentioned.")]

    explained = notes.get("explanations") or []
    if explained:
        blocks.append(_block("heading_2", "Explained simply"))
        for item in explained:
            blocks.append(_block("paragraph", ""))
            blocks[-1]["paragraph"]["rich_text"] = _bold_then(item["topic"], f": {item['explanation']}")

    cheat = notes.get("cheat_sheet") or []
    if cheat:
        blocks.append(_block("heading_2", "Cheat sheet"))
        blocks += [_block("bulleted_list_item", line) for line in cheat]

    questions = notes.get("practice_questions") or []
    if questions:
        blocks.append(_block("heading_2", "Practice questions"))
        blocks.append(_block("paragraph", "Click a question to reveal the answer."))
        for item in questions:
            toggle = _block("toggle", item["q"])
            toggle["toggle"]["children"] = [_block("paragraph", item["a"])]
            blocks.append(toggle)

    quiz = notes.get("quiz") or []
    if quiz:
        blocks.append(_block("heading_2", "Quiz"))
        for n, item in enumerate(quiz, 1):
            blocks.append(_block("paragraph", ""))
            blocks[-1]["paragraph"]["rich_text"] = _bold_then(f"{n}. {item['question']}", "")
            blocks += [_block("paragraph", f"{'ABCD'[i]}) {opt}") for i, opt in enumerate(item["options"])]
            answer = _block("toggle", "Show answer")
            correct = f"{'ABCD'[item['answer']]}) {item['options'][item['answer']]}"
            answer["toggle"]["children"] = [_block("paragraph", "✅ " + correct + (f" — {item['explanation']}" if item.get("explanation") else ""))]
            blocks.append(answer)

    cards = notes.get("flashcards") or []
    if cards:
        blocks.append(_block("heading_2", "Flashcards"))
        rows = [{"type": "table_row", "table_row": {"cells": [_rich_text("Front"), _rich_text("Back")]}}]
        rows += [{"type": "table_row", "table_row": {"cells": [_rich_text(c["front"]), _rich_text(c["back"])]}}
                 for c in cards[:99]]
        blocks.append({"object": "block", "type": "table",
                       "table": {"table_width": 2, "has_column_header": True, "children": rows}})
    return blocks


def _append(block_id: str, blocks: list[dict]) -> list[dict]:
    """Add blocks under `block_id`, 100 at a time. Returns the blocks Notion created."""
    created = []
    for i in range(0, len(blocks), MAX_BLOCKS_PER_REQUEST):
        data = _request("PATCH", f"/blocks/{block_id}/children", {"children": blocks[i:i + MAX_BLOCKS_PER_REQUEST]})
        created += data.get("results", [])
    return created


def _write_notes(page_id: str, notes: dict, transcript: str, heading: str | None = None) -> None:
    """Write the notes, then the transcript inside a collapsed "Full transcript" toggle heading."""
    blocks = build_blocks(notes, heading)
    words = len(transcript.split())
    if words:
        blocks.append({"object": "block", "type": "divider", "divider": {}})
        blocks.append(_block("heading_2", f"Full transcript ({words:,} words)", is_toggleable=True))
    created = _append(page_id, blocks)
    if words and created:
        toggle_id = created[-1]["id"]
        _append(toggle_id, [_block("paragraph", para) for para in split_text(transcript, 1500)])


def _create(parent: dict, properties: dict, notes: dict, transcript: str) -> str:
    page = _request("POST", "/pages", {
        "parent": parent,
        "icon": {"type": "emoji", "emoji": "📝"},
        "properties": properties,
    })
    _write_notes(page["id"], notes, transcript)
    forget_cached()
    return page.get("url", "")


def create_page(title: str, notes: dict, transcript: str, parent_id: str) -> str:
    """A new sub-page inside `parent_id`."""
    parent = normalize_id(parent_id)
    if not parent:
        raise AppError("No Notion page to put the notes in.")
    return _create({"page_id": parent}, {"title": {"title": _rich_text(title)[:1]}}, notes, transcript)


def create_entry(table_id: str, title: str, notes: dict, transcript: str,
                 day: str | None = None, link_to: str | None = None) -> str:
    """A new entry in a table. Fills in what the table has: a link to the class (`link_to`),
    a "lecture" type, and the date."""
    info = table_info(table_id)
    if not info:
        raise AppError("Notion won't let the site add to that table. Pick another place.")
    props = {info["title_prop"]: {"title": _rich_text(title)[:1]}}
    if link_to:
        column = link_column(info, page_parent_table(link_to))
        if column:
            props[column] = {"relation": [{"id": normalize_id(link_to)}]}
    if info["kind"]:
        name, kind, option = info["kind"]
        props[name] = {"select": {"name": option}} if kind == "select" else {"multi_select": [{"name": option}]}
    if info["date_prop"]:
        props[info["date_prop"]] = {"date": {"start": day or date.today().isoformat()}}
    return _create({"database_id": normalize_id(table_id)}, props, notes, transcript)


def append_to_page(page_id: str, title: str, notes: dict, transcript: str) -> str:
    page_id = normalize_id(page_id)
    if not page_id:
        raise AppError("No Notion page to add the notes to.")
    _write_notes(page_id, notes, transcript, heading=title)
    return _request("GET", f"/pages/{page_id}").get("url", "")
