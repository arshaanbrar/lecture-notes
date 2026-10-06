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


def can_create_pages() -> bool:
    return bool(config.NOTION_TOKEN and parent_page_id())


def parent_page_id() -> str:
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


def _page_title(page: dict) -> str:
    for prop in page.get("properties", {}).values():
        if prop.get("type") == "title":
            return "".join(t.get("plain_text", "") for t in prop.get("title", [])) or "Untitled"
    return "Untitled"


def list_pages(query: str = "") -> list[dict]:
    body = {
        "filter": {"property": "object", "value": "page"},
        "sort": {"direction": "descending", "timestamp": "last_edited_time"},
        "page_size": 50,
    }
    if query.strip():
        body["query"] = query.strip()
    results = _request("POST", "/search", body).get("results", [])
    pages = []
    for page in results:
        icon = page.get("icon") or {}
        pages.append({
            "id": page["id"],
            "title": _page_title(page),
            "url": page.get("url", ""),
            "icon": icon.get("emoji", "") if icon.get("type") == "emoji" else "",
        })
    return pages


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


def create_page(title: str, notes: dict, transcript: str) -> str:
    parent = parent_page_id()
    if not parent:
        raise AppError("NOTION_PARENT_PAGE_ID isn't set, so new pages can't be created (see README).")
    blocks = build_blocks(notes, transcript)
    page = _request("POST", "/pages", {
        "parent": {"page_id": parent},
        "icon": {"type": "emoji", "emoji": "📝"},
        "properties": {"title": {"title": _rich_text(title)[:1]}},
        "children": blocks[:MAX_BLOCKS_PER_REQUEST],
    })
    _append(page["id"], blocks[MAX_BLOCKS_PER_REQUEST:])
    return page.get("url", "")


def append_to_page(page_id: str, title: str, notes: dict, transcript: str) -> str:
    page_id = normalize_id(page_id)
    if not page_id:
        raise AppError("Pick a Notion page first.")
    _append(page_id, build_blocks(notes, transcript, heading=title))
    return _request("GET", f"/pages/{page_id}").get("url", "")
