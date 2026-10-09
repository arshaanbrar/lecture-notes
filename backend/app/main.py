import json
import logging
import re
import secrets
import shutil
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import assistant, audio, check, config, jobs, notion, placement, slides, summarize
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
    # `busy`: notes being made right now, so an update can wait until nobody's mid-way.
    return {"ok": True, "busy": jobs.busy()}


@public.get("/config")
def get_config():
    return {
        "password_required": bool(config.APP_PASSWORD),
        "notion_configured": notion.is_configured(),
        "transcribe_backend": config.TRANSCRIBE_BACKEND,
        "max_upload_mb": config.MAX_UPLOAD_MB,
        "summary_chunk_chars": config.SUMMARY_CHUNK_CHARS,
    }


@api.get("/auth/check")
def auth_check():
    return {"ok": True}


@api.get("/check")
def system_check():
    """✅/❌ for every piece the app needs (shown at /check)."""
    items = check.run()
    return {"ok": all(i["ok"] for i in items if i["required"]), "items": items}


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


MAX_SLIDE_FILES = 10


async def _save_slides(uploads: list[UploadFile | None], workdir: Path) -> list[Path]:
    """Optional lecture slides (PDFs or PowerPoints, one or more) that help the notes AI. Each is saved
    under its own name, so the notes AI can tell the decks apart."""
    uploads = [u for u in uploads if u is not None and u.filename]
    if len(uploads) > MAX_SLIDE_FILES:
        raise HTTPException(status_code=400, detail=f"Add at most {MAX_SLIDE_FILES} slide files.")
    paths = []
    for i, upload in enumerate(uploads, 1):
        suffix = Path(upload.filename).suffix.lower()
        if suffix not in slides.SUFFIXES:
            raise HTTPException(status_code=400, detail="Slides must be PDF or PowerPoint (.pptx) files.")
        name = re.sub(r"[^\w.\- ]+", "_", Path(upload.filename).stem)[:80] or "slides"
        dest = workdir / f"slides_{i}_{name}{suffix}"
        await _save(upload, dest, SLIDES_MAX_MB, f"slides file {upload.filename}")
        paths.append(dest)
    return paths


def _extras(value: str) -> list[str]:
    """The study extras the user ticked, e.g. "practice_questions,flashcards"."""
    return [e for e in value.split(",") if e in summarize.EXTRAS]


def _options(slides_paths: list[Path], extras: str, place: bool, usual_person_id: str, person_id: str = "",
             recorded_at: str = "", source: str = "recording") -> jobs.Options:
    """`person_id`: who this device sends for (only the class is guessed then); `recorded_at`: when the
    lecture was recorded, in the user's own time (e.g. "Tuesday, October 7 at 11:47 AM")."""
    return jobs.Options(slides_paths=slides_paths, extras=_extras(extras), place=place,
                        usual_person_id=usual_person_id[:64], person_id=person_id[:64],
                        recorded_at=recorded_at, source=source)


@api.post("/jobs/upload")
async def upload(file: UploadFile = File(...), slides_file: UploadFile | None = File(None),
                    slides_files: list[UploadFile] = File([]),
                 extras: str = Form(""), place: bool = Form(True), usual_person_id: str = Form(""),
                 person_id: str = Form(""), recorded_at: str = Form("", max_length=120)):
    workdir = jobs.new_workdir()
    try:
        suffix = Path(file.filename or "").suffix[:10] or ".bin"
        dest = workdir / f"input{suffix}"
        await _save(file, dest, config.MAX_UPLOAD_MB, "file")
        slides_paths = await _save_slides([slides_file, *slides_files], workdir)
    except HTTPException:
        shutil.rmtree(workdir, ignore_errors=True)
        raise
    return jobs.submit_file(dest, workdir, file.filename or "Recording",
                            _options(slides_paths, extras, place, usual_person_id, person_id, recorded_at)).public()


@api.post("/jobs/url")
async def from_url(url: str = Form(..., min_length=8, max_length=2000, pattern=r"^https?://"),
                   slides_file: UploadFile | None = File(None),
                    slides_files: list[UploadFile] = File([]), extras: str = Form(""),
                   place: bool = Form(True), usual_person_id: str = Form(""),
                 person_id: str = Form(""), recorded_at: str = Form("", max_length=120)):
    workdir = jobs.new_workdir()
    try:
        slides_paths = await _save_slides([slides_file, *slides_files], workdir)
    except HTTPException:
        shutil.rmtree(workdir, ignore_errors=True)
        raise
    return jobs.submit_url(url, workdir, _options(slides_paths, extras, place, usual_person_id, person_id, recorded_at)).public()


@api.post("/jobs/text")
async def from_text(transcript: str = Form(..., min_length=1, max_length=400_000),
                    label: str = Form("Recording", max_length=200), slides_file: UploadFile | None = File(None),
                    slides_files: list[UploadFile] = File([]),
                    extras: str = Form(""), place: bool = Form(True), usual_person_id: str = Form(""),
                 person_id: str = Form(""), recorded_at: str = Form("", max_length=120),
                    source: Literal["recording", "document"] = Form("recording"),
                    parts: str = Form("", max_length=500_000), parts_chars: int = Form(0, ge=0)):
    """Text the page already has: a recording it transcribed while it was being made, or a scanned
    document it read itself. Only the notes are left to write. `parts` (JSON) are notes the page had
    written during the recording for the first `parts_chars` characters of the transcript."""
    try:
        done = json.loads(parts) if parts else []
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Bad notes from the recording.")
    if not isinstance(done, list) or len(done) > 100 or not all(isinstance(p, dict) for p in done):
        raise HTTPException(status_code=400, detail="Bad notes from the recording.")
    workdir = jobs.new_workdir()
    try:
        slides_paths = await _save_slides([slides_file, *slides_files], workdir)
    except HTTPException:
        shutil.rmtree(workdir, ignore_errors=True)
        raise
    options = _options(slides_paths, extras, place, usual_person_id, person_id, recorded_at, source)
    options.parts, options.parts_chars = done or None, parts_chars if done else 0
    return jobs.submit_text(transcript, workdir, label, options).public()


class PartBody(BaseModel):
    text: str = Field(min_length=1, max_length=40_000)
    index: int = Field(ge=1, le=100)


@api.post("/live/part-notes")
def live_part_notes(body: PartBody):
    """Notes for one finished part of a lecture that's still being recorded."""
    return {"notes": summarize.part_notes(body.text, body.index)}


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
    recorded_at: str = Field(default="", max_length=120)  # when, in the user's time (matches timetables)


class GuessBody(NoteContext):
    usual_person_id: str = Field(default="", max_length=64)
    person_id: str = Field(default="", max_length=64)  # this device's person: then only the class is guessed


@api.post("/notion/guess")
def notion_guess(body: GuessBody):
    return placement.guess_owner(body.note_title, body.summary, body.usual_person_id,
                                 only_person_id=body.person_id, recorded_at=body.recorded_at)


class ClassesBody(NoteContext):
    person_id: str


@api.post("/notion/classes")
def notion_classes(body: ClassesBody):
    if not notion.normalize_id(body.person_id):
        raise HTTPException(status_code=400, detail="Pick a person first.")
    return placement.classes(body.person_id, body.note_title, body.summary, body.recorded_at)


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
    source: Literal["recording", "document"] = "recording"


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
    """Transcribe the audio recorded since the last time (used all through a recording)."""
    workdir = jobs.new_workdir()
    try:
        dest = workdir / ("snippet" + (Path(audio_file.filename or "").suffix[:10] or ".webm"))
        await _save(audio_file, dest, SNIPPET_MAX_MB, "audio")
        try:
            text = assistant.transcribe_snippet(dest, workdir, skip_seconds)
        except audio.TooShort:
            text = ""  # too short or silent: nothing new to add
        except AppError as e:
            # Unreadable audio or Groq trouble: the page then transcribes the whole recording at the end.
            return {"text": "", "ok": False, "error": str(e)}
        return {"text": text, "ok": True}
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


@app.get("/check", include_in_schema=False)
def check_page():
    """The system check page (its data comes from /api/check, which needs the password)."""
    return FileResponse(FRONTEND_DIR / "check.html", headers={"Cache-Control": "no-cache"})


app.include_router(public)
app.include_router(api)
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
