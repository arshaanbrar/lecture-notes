import logging
import secrets
import shutil
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config, jobs, notion
from .utils import AppError

logging.basicConfig(level=logging.INFO)

FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"

app = FastAPI(title="Lecture Notes")


@app.middleware("http")
async def no_stale_frontend(request, call_next):
    # Make browsers re-check the page, JS and CSS on every load (cheap: unchanged files return 304),
    # so nobody runs an old app.js against a new index.html after a deploy.
    response = await call_next(request)
    if not request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.exception_handler(AppError)
async def app_error_handler(_, exc: AppError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


def require_password(x_app_password: str | None = Header(default=None)) -> None:
    if config.APP_PASSWORD and not secrets.compare_digest(
        (x_app_password or "").encode(), config.APP_PASSWORD.encode()
    ):
        raise HTTPException(status_code=401, detail="Wrong or missing password.")


public = APIRouter(prefix="/api")
api = APIRouter(prefix="/api", dependencies=[Depends(require_password)])


@app.get("/healthz")
def healthz():
    return {"ok": True}


@public.get("/config")
def get_config():
    return {
        "password_required": bool(config.APP_PASSWORD),
        "notion_configured": notion.is_configured(),
        "notion_default_parent": bool(notion.parent_page_id()),
        "transcribe_backend": config.TRANSCRIBE_BACKEND,
        "max_upload_mb": config.MAX_UPLOAD_MB,
    }


@api.get("/auth/check")
def auth_check():
    return {"ok": True}


# ---------- jobs ----------

@api.post("/jobs/upload")
async def upload(file: UploadFile = File(...)):
    workdir = jobs.new_workdir()
    suffix = Path(file.filename or "").suffix[:10] or ".bin"
    dest = workdir / f"input{suffix}"
    limit = config.MAX_UPLOAD_MB * 1024 * 1024
    size = 0
    with dest.open("wb") as out:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > limit:
                break
            out.write(chunk)
    if size > limit or size == 0:
        shutil.rmtree(workdir, ignore_errors=True)
        detail = f"File is larger than {config.MAX_UPLOAD_MB} MB." if size else "The file is empty."
        raise HTTPException(status_code=413 if size else 400, detail=detail)
    return jobs.submit_file(dest, workdir, label=file.filename or "Recording").public()


class UrlBody(BaseModel):
    url: str = Field(min_length=8, max_length=2000, pattern=r"^https?://")


@api.post("/jobs/url")
def from_url(body: UrlBody):
    return jobs.submit_url(body.url).public()


@api.get("/jobs/{job_id}")
def job_status(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found — the server may have restarted. Please try again.")
    return job.public()


# ---------- notion ----------

@api.get("/notion/tree")
def notion_tree(refresh: bool = False):
    return {"nodes": notion.page_tree(refresh)}


@api.get("/notion/children/{parent_id}")
def notion_children(parent_id: str, kind: Literal["page", "database"] = "page", refresh: bool = False):
    clean_id = notion.normalize_id(parent_id)
    if not clean_id:
        raise HTTPException(status_code=400, detail="Invalid Notion page ID.")
    return {"children": notion.children(clean_id, kind, refresh)}


class Notes(BaseModel):
    summary: str = ""
    key_points: list[str] = []
    action_items: list[str] = []


class ExportBody(BaseModel):
    mode: Literal["new", "existing"]
    page_id: str | None = None
    title: str = Field(min_length=1, max_length=200)
    notes: Notes
    transcript: str = ""


@api.post("/notion/export")
def notion_export(body: ExportBody):
    notes = body.notes.model_dump()
    if body.mode == "new":
        url = notion.create_page(body.title, notes, body.transcript, body.page_id)
    else:
        url = notion.append_to_page(body.page_id or "", body.title, notes, body.transcript)
    return {"url": url}


app.include_router(public)
app.include_router(api)
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
