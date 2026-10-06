import logging
import secrets
import shutil
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config, jobs, notion, placement
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
#
# Sending notes is: who is it for → which class → the AI picks the best spot → confirm.

@api.get("/notion/people")
def notion_people(refresh: bool = False):
    if refresh:
        notion.forget_cached()
    return {"people": placement.people()}


class NoteContext(BaseModel):
    note_title: str = Field(default="", max_length=300)
    summary: str = Field(default="", max_length=5000)


class ClassesBody(NoteContext):
    person_id: str


@api.post("/notion/classes")
def notion_classes(body: ClassesBody):
    if not notion.normalize_id(body.person_id):
        raise HTTPException(status_code=400, detail="Pick a person first.")
    return placement.classes(body.person_id, body.note_title, body.summary)


class PlanBody(NoteContext):
    person_id: str
    class_id: str | None = None
    class_text: str = Field(default="", max_length=200)


@api.post("/notion/plan")
def notion_plan(body: PlanBody):
    if not notion.normalize_id(body.person_id):
        raise HTTPException(status_code=400, detail="Pick a person first.")
    class_id = body.class_id if body.class_id and notion.normalize_id(body.class_id) else None
    return placement.plan(body.person_id, class_id, body.class_text, body.note_title, body.summary)


class Notes(BaseModel):
    summary: str = ""
    key_points: list[str] = []
    action_items: list[str] = []


class Place(BaseModel):
    kind: Literal["entry", "page", "append"]
    target_id: str = Field(pattern=r"[0-9a-fA-F-]{32,36}")
    link_to: str | None = Field(default=None, pattern=r"[0-9a-fA-F-]{32,36}")


class ExportBody(BaseModel):
    place: Place
    title: str = Field(min_length=1, max_length=200)
    notes: Notes
    transcript: str = ""
    local_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")


@api.post("/notion/export")
def notion_export(body: ExportBody):
    url = placement.send(body.place.model_dump(), body.title, body.notes.model_dump(),
                         body.transcript, body.local_date)
    return {"url": url}


app.include_router(public)
app.include_router(api)
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
