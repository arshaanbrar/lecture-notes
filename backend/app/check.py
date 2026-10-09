"""System check: is every piece the app needs working? Shown at /check (password-protected).

Each check makes a small real call (e.g. one tiny AI request) so a ✅ means it actually works, not
just that a key is filled in.
"""

import importlib
import shutil
import subprocess
from importlib import metadata

import httpx

from . import config, documents, groq, notion, placement
from .utils import AppError


def _item(name: str, ok: bool, detail: str, required: bool = True) -> dict:
    return {"name": name, "ok": ok, "detail": detail, "required": required}


def _version(cmd: list[str]) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return (out.stdout or out.stderr).strip().splitlines()[0][:80]
    except (OSError, subprocess.SubprocessError, IndexError):
        return ""


def _groq_models() -> set[str]:
    resp = httpx.get(groq.BASE + "/models", headers=groq._headers(), timeout=20)
    if resp.status_code == 401:
        raise AppError("Groq says the key is invalid.")
    resp.raise_for_status()
    return {m["id"] for m in resp.json().get("data", [])}


def _try_model(fast: bool) -> tuple[bool, str]:
    """One tiny real request, to prove a model actually answers for this account."""
    try:
        groq.chat_json("Reply with JSON only.", 'Reply with {"ok": true}', lambda _: None, fast=fast, max_wait=0)
        return True, groq.last_model("fast" if fast else "main") or "the configured model"
    except AppError as e:
        return False, str(e)


def _groq_limits(listed: set[str]) -> dict:
    """Which models share the work, and any that are out of free allowance right now."""
    rows = [r for r in groq.status() if r["model"] in listed]
    usable = {r["model"] for r in rows if r["state"] == "ready"}
    out = [f"{r['model'].split('/')[-1]}: {r['state']}" for r in rows if r["state"] != "ready"]
    notes = [m.split("/")[-1] for m in config.GROQ_MODELS if m in usable]
    detail = (f"{len(notes)} notes models share the free limits ({', '.join(notes) or 'none'})."
              + (" Out for now: " + "; ".join(dict.fromkeys(out)) + "." if out else " None are out of allowance."))
    return _item("Groq free limits", bool(notes), detail)


def run() -> list[dict]:
    items = []

    # --- AI (Groq) ---
    if not config.GROQ_API_KEY:
        items.append(_item("Groq key", False, "GROQ_API_KEY isn't set on the server."))
    else:
        try:
            models = _groq_models()
            items.append(_item("Groq key", True, f"Works ({len(models)} models available)."))
            ok, detail = _try_model(fast=False)
            items.append(_item("AI for notes", ok, f"Answering with {detail}." if ok else detail))
            ok, detail = _try_model(fast=True)
            items.append(_item("Quick AI (chat helper, quick questions)", ok,
                               f"Answering with {detail}." if ok else detail, required=False))
            whisper = [m for m in config.GROQ_WHISPER_MODELS if m in models]
            items.append(_item("Transcription (Whisper)", bool(whisper),
                               f"Available: {', '.join(whisper)}." if whisper
                               else "None of GROQ_WHISPER_MODEL is available to this key."))
            items.append(_groq_limits(models))
        except (AppError, httpx.HTTPError) as e:
            items.append(_item("Groq key", False, f"Couldn't reach Groq: {e}"))

    # --- Notion ---
    if not notion.is_configured():
        items.append(_item("Notion", False, "NOTION_TOKEN isn't set, so Send to Notion is off."))
    else:
        try:
            nodes = notion.page_tree(refresh=True)
            people = [p["title"] for p in placement.people()]
            hidden = sorted(n.title() for n in config.HIDDEN_PEOPLE)
            items.append(_item("Notion", bool(nodes),
                               f"Connected. It can see {len(nodes)} pages and tables. People: "
                               f"{', '.join(people) or 'none found'}"
                               + (f" (hidden: {', '.join(hidden)})" if hidden else "") + "."
                               if nodes else "Connected, but it can't see any pages. Share the top page with "
                               "the integration (••• → Connections)."))
        except AppError as e:
            items.append(_item("Notion", False, str(e)))

    # --- tools on the server ---
    for name, cmd in (("ffmpeg (audio)", "ffmpeg"), ("ffprobe (audio)", "ffprobe")):
        path = shutil.which(cmd)
        items.append(_item(name, bool(path), _version([cmd, "-version"]) if path else f"{cmd} isn't installed."))
    ocr = documents.ocr_available()
    items.append(_item("OCR for scans (server backup)", ocr,
                       _version(["tesseract", "--version"]) if ocr
                       else "tesseract/pdftoppm aren't installed. Scans are still read in the browser.",
                       required=False))
    for name, module in (("PDF reader", "pypdf"), ("PowerPoint reader", "pptx")):
        try:
            importlib.import_module(module)
            items.append(_item(name, True, f"{module} installed."))
        except ImportError:
            items.append(_item(name, False, f"{module} isn't installed (requirements.txt)."))
    try:
        items.append(_item("Links (yt-dlp)", True, f"yt-dlp {metadata.version('yt-dlp')}.", required=False))
    except metadata.PackageNotFoundError:
        items.append(_item("Links (yt-dlp)", False, "yt-dlp isn't installed.", required=False))

    # --- settings ---
    items.append(_item("Password", bool(config.APP_PASSWORD),
                       "Set." if config.APP_PASSWORD else "No APP_PASSWORD: anyone with the link can use it.",
                       required=False))
    return items
