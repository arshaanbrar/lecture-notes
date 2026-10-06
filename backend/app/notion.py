"""Notion export using one shared internal integration (set up once by the site owner)."""

import re
import time
from datetime import date

import httpx

from . import config
from .utils import AppError, split_text

API = "https://api.notion.com/v1"
VERSION = "2022-06-28"
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


def _request(method: str, path: str, body: dict | None = None) -> dict:
    if not config.NOTION_TOKEN:
        raise AppError("Notion isn't connected yet. The site owner needs to set NOTION_TOKEN (see README).")
    headers = {
        "Authorization": f"Bearer {config.NOTION_TOKEN}",
        "Notion-Version": VERSION,
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
MAX_CONTAINER_DEPTH = 3
MAX_BLOCK_REQUESTS = 40

_children_cache: dict[str, tuple[float, list[dict]]] = {}


def _icon_lookup() -> dict[str, str]:
    return {n["id"]: n["icon"] for n in (_tree_cache["nodes"] or [])}


def _page_children(page_id: str) -> list[dict]:
    icons = _icon_lookup()
    found: list[dict] = []
    budget = [MAX_BLOCK_REQUESTS]

    def walk(block_id: str, depth: int) -> None:
        cursor = None
        while budget[0] > 0:
            budget[0] -= 1
            data = _request("GET", f"/blocks/{block_id}/children?page_size=100"
                            + (f"&start_cursor={cursor}" if cursor else ""))
            for block in data.get("results", []):
                kind = block.get("type")
                if kind == "child_page":
                    found.append({"id": block["id"], "type": "page", "icon": icons.get(block["id"], ""),
                                  "title": block["child_page"].get("title") or "Untitled"})
                elif kind == "child_database":
                    found.append({"id": block["id"], "type": "database", "icon": icons.get(block["id"], ""),
                                  "title": block["child_database"].get("title") or "Untitled"})
                elif kind in CONTAINER_BLOCKS and block.get("has_children") and depth < MAX_CONTAINER_DEPTH:
                    walk(block["id"], depth + 1)
            if not data.get("has_more"):
                return
            cursor = data.get("next_cursor")

    walk(page_id, 0)
    return found


def _database_children(database_id: str) -> list[dict]:
    found, cursor = [], None
    for _ in range(5):  # up to 500 entries
        body = {"page_size": 100, **({"start_cursor": cursor} if cursor else {})}
        data = _request("POST", f"/databases/{database_id}/query", body)
        for page in data.get("results", []):
            icon = page.get("icon") or {}
            found.append({"id": page["id"], "type": "page", "title": _title(page),
                          "icon": icon.get("emoji", "") if icon.get("type") == "emoji" else ""})
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return found


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


def append_to_page(page_id: str, title: str, notes: dict, transcript: str) -> str:
    page_id = normalize_id(page_id)
    if not page_id:
        raise AppError("Pick a Notion page first.")
    _append(page_id, build_blocks(notes, transcript, heading=title))
    return _request("GET", f"/pages/{page_id}").get("url", "")
