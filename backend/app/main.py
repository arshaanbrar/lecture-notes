import logging
import secrets
import shutil
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import assistant, config, jobs, notion, placement, slides, summarize
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

SLIDES_MAX_MB = 50


async def _save(upload: UploadFile, dest: Path, limit_mb: int, what: str) -> None:
    """Stream an upload to disk, refusing empty or oversized files."""
    limit, size = limit_mb * 1024 * 1024, 0
    with dest.open("wb") as out:
        while chunk := await upload.read(1024 * 1024):
            size += len(chunk)
            if size > limit:
                raise HTTPException(status_code=413, detail=f"The {what} is larger than {limit_mb} MB.")
            out.write(chunk)
    if size == 0:
        raise HTTPException(status_code=400, detail=f"The {what} is empty.")


async def _save_slides(upload: UploadFile | None, workdir: Path) -> Path | None:
    """Optional lecture slides (PDF or PowerPoint) that help the notes AI."""
    if not upload or not upload.filename:
        return None
    suffix = Path(upload.filename).suffix.lower()
    if suffix not in slides.SUFFIXES:
        raise HTTPException(status_code=400, detail="Slides must be a PDF or PowerPoint (.pptx) file.")
    dest = workdir / f"slides{suffix}"
    await _save(upload, dest, SLIDES_MAX_MB, "slides file")
    return dest


def _extras(value: str) -> list[str]:
    """The study extras the user ticked, e.g. "practice_questions,flashcards"."""
    return [e for e in value.split(",") if e in summarize.EXTRAS]


@api.post("/jobs/upload")
async def upload(file: UploadFile = File(...), slides_file: UploadFile | None = File(None),
                 extras: str = Form("")):
    workdir = jobs.new_workdir()
    try:
        suffix = Path(file.filename or "").suffix[:10] or ".bin"
        dest = workdir / f"input{suffix}"
        await _save(file, dest, config.MAX_UPLOAD_MB, "file")
        slides_path = await _save_slides(slides_file, workdir)
    except HTTPException:
        shutil.rmtree(workdir, ignore_errors=True)
        raise
    return jobs.submit_file(dest, workdir, label=file.filename or "Recording", slides_path=slides_path,
                            extras=_extras(extras)).public()


@api.post("/jobs/url")
async def from_url(url: str = Form(..., min_length=8, max_length=2000, pattern=r"^https?://"),
                   slides_file: UploadFile | None = File(None), extras: str = Form("")):
    workdir = jobs.new_workdir()
    try:
        slides_path = await _save_slides(slides_file, workdir)
    except HTTPException:
        shutil.rmtree(workdir, ignore_errors=True)
        raise
    return jobs.submit_url(url, workdir, slides_path, _extras(extras)).public()


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
        placement.forget_cached()
    return {"people": placement.people()}


class NoteContext(BaseModel):
    note_title: str = Field(default="", max_length=300)
    summary: str = Field(default="", max_length=5000)


class GuessBody(NoteContext):
    usual_person_id: str = Field(default="", max_length=64)


@api.post("/notion/guess")
def notion_guess(body: GuessBody):
    return placement.guess_owner(body.note_title, body.summary, body.usual_person_id)


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


class Question(BaseModel):
    q: str
    a: str


class Term(BaseModel):
    term: str
    definition: str


class Flashcard(BaseModel):
    front: str
    back: str


class QuizQuestion(BaseModel):
    question: str
    options: list[str]
    answer: int
    explanation: str = ""


class Explanation(BaseModel):
    topic: str
    explanation: str


class Notes(BaseModel):
    summary: str = ""
    key_points: list[str] = []
    action_items: list[str] = []
    practice_questions: list[Question] = []
    key_terms: list[Term] = []
    flashcards: list[Flashcard] = []
    quiz: list[QuizQuestion] = []
    cheat_sheet: list[str] = []
    explanations: list[Explanation] = []


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


# ---------- study helper chat ----------

SNIPPET_MAX_MB = 25


@api.post("/assistant/transcribe")
async def assistant_transcribe(audio_file: UploadFile = File(...), skip_seconds: float = Form(0, ge=0, le=10)):
    """Transcribe the audio recorded since the last question (used mid-lecture)."""
    workdir = jobs.new_workdir()
    try:
        dest = workdir / ("snippet" + (Path(audio_file.filename or "").suffix[:10] or ".webm"))
        await _save(audio_file, dest, SNIPPET_MAX_MB, "audio")
        try:
            text = assistant.transcribe_snippet(dest, workdir, skip_seconds)
        except AppError:
            text = ""  # too short or silent: nothing new to add
        return {"text": text}
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=8000)


class ChatContext(BaseModel):
    live: bool = False
    title: str = ""
    summary: str = ""
    key_points: list[str] = []
    key_terms: list[Term] = []
    transcript: str = Field(default="", max_length=400_000)


class ChatBody(BaseModel):
    messages: list[ChatMessage] = Field(max_length=40)
    context: ChatContext = ChatContext()


@api.post("/assistant/chat")
def assistant_chat(body: ChatBody):
    context = body.context.model_dump()
    return {"reply": assistant.answer([m.model_dump() for m in body.messages], context)}


app.include_router(public)
app.include_router(api)
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
