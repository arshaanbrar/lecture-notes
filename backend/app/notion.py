"""Notion export using one shared internal integration (set up once by the site owner)."""

import re
import time
from datetime import date

import httpx

from . import config
from .utils import AppError, split_text

API = "https://api.notion.com/v1"
VERSION = "2022-06-28"
# Newer API version, used only for tables. It understands "data sources", so it can open
# linked views (e.g. a "Courses" gallery that shows another table) and tell their real names.
DATA_SOURCES_VERSION = "2025-09-03"
MAX_BLOCKS_PER_REQUEST = 100


def is_configured() -> bool:
    return bool(config.NOTION_TOKEN)


def parent_page_id() -> str:
    """Optional default parent for new pages when the user hasn't picked one."""
    return normalize_id(config.NOTION_PARENT_PAGE_ID)


def normalize_id(value: str) -> str:
    """Accept a raw ID or a full Notion URL and return the 32-char page ID."""
    hexes = re.findall(r"[0-9a-f]{32}", (value or "").replace("-", "").lower())
    return hexes[-1] if hexes else ""


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


# ---------- page tree (for the folder-style picker) ----------

TREE_TTL_SECONDS = 60
MAX_SEARCH_REQUESTS = 30  # 100 results each → up to 3000 pages/databases

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
    """Every page/database shared with the integration, each with its parent's ID (None = top level)."""
    if not refresh and _tree_cache["nodes"] is not None and time.time() - _tree_cache["at"] < TREE_TTL_SECONDS:
        return _tree_cache["nodes"]
    if refresh:
        _children_cache.clear()
        _sources_cache.clear()

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
        icon = obj.get("icon") or {}
        nodes[obj["id"]] = {
            "id": obj["id"],
            "type": obj["object"],
            "title": _title(obj),
            "icon": icon.get("emoji", "") if icon.get("type") == "emoji" else "",
            "parent": parent,
        }
    # A page whose parent the integration can't see is shown at the top level.
    for node in nodes.values():
        if node["parent"] not in nodes:
            node["parent"] = None

    _tree_cache.update(at=time.time(), nodes=list(nodes.values()))
    return _tree_cache["nodes"]


# ---------- ordered children (what's inside one page, in Notion's order) ----------

# Blocks that can hold sub-pages inside them on a page (columns, toggles, callouts…).
CONTAINER_BLOCKS = {"column_list", "column", "toggle", "callout", "quote", "synced_block",
                    "heading_1", "heading_2", "heading_3", "bulleted_list_item", "numbered_list_item"}
MAX_CONTAINER_DEPTH = 4
MAX_BLOCK_REQUESTS = 60
# Toggles that hold a template's internals (e.g. "⚠️ DO NOT DELETE. This stuff runs the template!").
# Their contents stay searchable but aren't listed. Other toggles are listed as normal.
TEMPLATE_INTERNALS = re.compile(r"(do not|don'?t) delete|runs the template|template (stuff|files|internals)", re.I)

_children_cache: dict[str, tuple[float, list[dict]]] = {}


def _icon_lookup() -> dict[str, str]:
    return {n["id"]: n["icon"] for n in (_tree_cache["nodes"] or [])}


def _block_text(block: dict) -> str:
    content = block.get(block.get("type", ""), {}) or {}
    return "".join(t.get("plain_text", "") for t in content.get("rich_text", [])).strip()


def _is_template_internals(block: dict) -> bool:
    return bool(TEMPLATE_INTERNALS.search(_block_text(block)))


def _page_children(page_id: str) -> list[dict]:
    """Sub-pages and tables on a page, top to bottom. Items inside a template's "do not delete"
    section are marked hidden; an unnamed table takes the text around it as its name."""
    icons = _icon_lookup()
    found: list[dict] = []
    budget = [MAX_BLOCK_REQUESTS]

    def walk(block_id: str, depth: int, label: str, hidden: bool) -> None:
        cursor = None
        while budget[0] > 0:
            budget[0] -= 1
            data = _request("GET", f"/blocks/{block_id}/children?page_size=100"
                            + (f"&start_cursor={cursor}" if cursor else ""))
            nearby = label  # the last bit of text seen, e.g. a "Courses" heading above a table
            for block in data.get("results", []):
                kind = block.get("type")
                if kind == "child_page":
                    found.append({"id": block["id"], "type": "page", "icon": icons.get(block["id"], ""),
                                  "title": block["child_page"].get("title") or "Untitled", "hidden": hidden})
                elif kind == "child_database":
                    title = block["child_database"].get("title", "").strip()
                    if _is_generic_title(title):
                        title = nearby or _data_source_label(block["id"]) or "Untitled table"
                    found.append({"id": block["id"], "type": "database", "icon": icons.get(block["id"], ""),
                                  "title": title, "hidden": hidden})
                elif kind in CONTAINER_BLOCKS and block.get("has_children") and depth < MAX_CONTAINER_DEPTH:
                    # Layout blocks (columns) have no text of their own, so they keep the nearby label.
                    layout = kind in ("column_list", "column", "synced_block")
                    walk(block["id"], depth + 1, nearby if layout else _block_text(block)[:60],
                         hidden or _is_template_internals(block))
                elif _block_text(block):
                    nearby = _block_text(block)[:60]
            if not data.get("has_more"):
                return
            cursor = data.get("next_cursor")

    walk(page_id, 0, "", False)
    return found


GENERIC_TITLES = {"", "untitled", "untitled table", "untitled database", "new database", "new table"}

_sources_cache: dict[str, list[dict]] = {}


def _is_generic_title(title: str) -> bool:
    t = title.strip().lower()
    return t in GENERIC_TITLES or t.startswith("view of ")


def _data_sources(database_id: str) -> list[dict]:
    """The table(s) a database block shows. A linked view's source is another table."""
    if database_id not in _sources_cache:
        try:
            db = _request("GET", f"/databases/{database_id}", version=DATA_SOURCES_VERSION)
            _sources_cache[database_id] = db.get("data_sources", [])
        except AppError:
            _sources_cache[database_id] = []
    return _sources_cache[database_id]


def _data_source_label(database_id: str) -> str:
    names = [s.get("name", "").strip() for s in _data_sources(database_id)]
    names = [n for n in names if not _is_generic_title(n) and not n.lower().startswith("new data source")]
    return " + ".join(names)


def _query_pages(path: str, version: str) -> list[dict]:
    found, cursor = [], None
    for _ in range(5):  # up to 500 entries
        body = {"page_size": 100, **({"start_cursor": cursor} if cursor else {})}
        data = _request("POST", path, body, version=version)
        for page in data.get("results", []):
            if page.get("object") != "page" or page.get("in_trash") or page.get("archived"):
                continue
            icon = page.get("icon") or {}
            found.append({"id": page["id"], "type": "page", "title": _title(page),
                          "icon": icon.get("emoji", "") if icon.get("type") == "emoji" else ""})
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return found


def _database_children(database_id: str) -> list[dict]:
    sources = _data_sources(database_id)
    if sources:
        pages, seen = [], set()
        for source in sources[:4]:
            try:
                for page in _query_pages(f"/data_sources/{source['id']}/query", DATA_SOURCES_VERSION):
                    if page["id"] not in seen:
                        seen.add(page["id"])
                        pages.append(page)
            except AppError:
                continue
        if pages:
            return pages
    return _query_pages(f"/databases/{database_id}/query", VERSION)


def children(parent_id: str, kind: str, refresh: bool = False) -> list[dict]:
    """Pages/databases directly inside a page (in on-page order) or a database's entries."""
    key = f"{kind}:{parent_id}"
    cached = _children_cache.get(key)
    if cached and not refresh and time.time() - cached[0] < TREE_TTL_SECONDS:
        return cached[1]
    items = _database_children(parent_id) if kind == "database" else _page_children(parent_id)
    _children_cache[key] = (time.time(), items)
    return items


# ---------- block building ----------

def _rich_text(text: str) -> list[dict]:
    # Notion caps each text object at 2000 chars; a block can hold up to 100 of them.
    return [{"type": "text", "text": {"content": piece}} for piece in split_text(text, 2000)][:100]


def _block(kind: str, text: str, **extra) -> dict:
    return {"object": "block", "type": kind, kind: {"rich_text": _rich_text(text), **extra}}


def build_blocks(notes: dict, transcript: str, heading: str | None = None) -> list[dict]:
    blocks: list[dict] = []
    if heading:
        blocks.append(_block("heading_1", heading))
    blocks.append(_block("paragraph", f"Generated {date.today():%B %d, %Y}"))

    blocks.append(_block("heading_2", "Summary"))
    blocks.append(_block("paragraph", notes.get("summary") or "—"))

    blocks.append(_block("heading_2", "Key points"))
    points = notes.get("key_points") or []
    blocks += [_block("bulleted_list_item", p) for p in points] or [_block("paragraph", "—")]

    blocks.append(_block("heading_2", "Action items"))
    actions = notes.get("action_items") or []
    blocks += [_block("to_do", a, checked=False) for a in actions] or [_block("paragraph", "None mentioned.")]

    if transcript.strip():
        blocks.append({"object": "block", "type": "divider", "divider": {}})
        blocks.append(_block("heading_2", "Full transcript"))
        blocks += [_block("paragraph", para) for para in split_text(transcript, 1500)]
    return blocks


def _append(page_id: str, blocks: list[dict]) -> None:
    for i in range(0, len(blocks), MAX_BLOCKS_PER_REQUEST):
        _request("PATCH", f"/blocks/{page_id}/children", {"children": blocks[i:i + MAX_BLOCKS_PER_REQUEST]})


def create_page(title: str, notes: dict, transcript: str, parent_id: str | None = None) -> str:
    """Create a new page inside `parent_id` (or the default parent page)."""
    parent = normalize_id(parent_id or "") or parent_page_id()
    if not parent:
        raise AppError("Open the Notion page you want the new page created inside.")
    blocks = build_blocks(notes, transcript)
    page = _request("POST", "/pages", {
        "parent": {"page_id": parent},
        "icon": {"type": "emoji", "emoji": "📝"},
        "properties": {"title": {"title": _rich_text(title)[:1]}},
        "children": blocks[:MAX_BLOCKS_PER_REQUEST],
    })
    _append(page["id"], blocks[MAX_BLOCKS_PER_REQUEST:])
    _tree_cache["nodes"] = None  # so the new page shows up in the picker straight away
    _children_cache.clear()
    return page.get("url", "")


# ---------- "Add as a lecture" (course/lecture templates) ----------
#
# Many student templates keep courses in one table (e.g. "Domains") and lectures in another
# (e.g. "Topics") that links back to the course. When the picked page is a course like that,
# notes can be created as a new lecture entry linked to it, so they show up in the template's
# calendars and course views instead of as a loose sub-page.

_lecture_cache: dict[str, tuple[float, dict | None]] = {}


def _same_id(a: str | None, b: str | None) -> bool:
    return bool(a and b) and normalize_id(a) == normalize_id(b)


def lecture_target(course_id: str) -> dict | None:
    """If `course_id` is an entry in a table that another table links to, describe that table."""
    course_id = normalize_id(course_id)
    cached = _lecture_cache.get(course_id)
    if cached and time.time() - cached[0] < TREE_TTL_SECONDS * 10:
        return cached[1]
    result = None
    try:
        course = _request("GET", f"/pages/{course_id}")
        kind, courses_db = _raw_parent(course)
        if kind == "database_id":
            schema = _request("GET", f"/databases/{courses_db}").get("properties", {})
            linked = {p["relation"].get("database_id") for p in schema.values() if p.get("type") == "relation"}
            linked = {d for d in linked if d and not _same_id(d, courses_db)}
            for lectures_db in linked:
                db = _request("GET", f"/databases/{lectures_db}")
                props = db.get("properties", {})
                link = next((name for name, p in props.items() if p.get("type") == "relation"
                             and _same_id(p["relation"].get("database_id"), courses_db)), None)
                title = next((name for name, p in props.items() if p.get("type") == "title"), None)
                if not (link and title):
                    continue
                kind_prop, kind_value = None, None
                for name, p in props.items():
                    if p.get("type") == "select":
                        match = next((o["name"] for o in p["select"].get("options", [])
                                      if o["name"].strip().lower() == "lecture"), None)
                        if match:
                            kind_prop, kind_value = name, match
                            break
                if not kind_prop and not re.search(r"lecture|topic|note|class", _title(db), re.I):
                    continue  # e.g. an assessments table that links to courses: not for lecture notes
                dates = [name for name, p in props.items() if p.get("type") == "date"]
                result = {
                    "database_id": lectures_db,
                    "database_title": _title(db),
                    "course_title": _title(course),
                    "title_prop": title,
                    "link_prop": link,
                    "kind_prop": kind_prop,
                    "kind_value": kind_value,
                    "date_prop": "date" if "date" in dates else (dates[0] if dates else None),
                }
                if kind_prop:  # prefer the table that actually has a "lecture" type
                    break
    except AppError:
        result = None
    _lecture_cache[course_id] = (time.time(), result)
    return result


def create_lecture(course_id: str, title: str, notes: dict, transcript: str, day: str | None) -> str:
    target = lecture_target(course_id)
    if not target:
        raise AppError("That page isn't a course with a lectures table. Pick another option.")
    props = {
        target["title_prop"]: {"title": _rich_text(title)[:1]},
        target["link_prop"]: {"relation": [{"id": normalize_id(course_id)}]},
    }
    if target["kind_prop"]:
        props[target["kind_prop"]] = {"select": {"name": target["kind_value"]}}
    if target["date_prop"]:
        props[target["date_prop"]] = {"date": {"start": day or date.today().isoformat()}}
    blocks = build_blocks(notes, transcript)
    page = _request("POST", "/pages", {
        "parent": {"database_id": target["database_id"]},
        "icon": {"type": "emoji", "emoji": "📝"},
        "properties": props,
        "children": blocks[:MAX_BLOCKS_PER_REQUEST],
    })
    _append(page["id"], blocks[MAX_BLOCKS_PER_REQUEST:])
    _tree_cache["nodes"] = None
    _children_cache.clear()
    return page.get("url", "")


def append_to_page(page_id: str, title: str, notes: dict, transcript: str) -> str:
    page_id = normalize_id(page_id)
    if not page_id:
        raise AppError("Pick a Notion page first.")
    _append(page_id, build_blocks(notes, transcript, heading=title))
    return _request("GET", f"/pages/{page_id}").get("url", "")
