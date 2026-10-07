"""In-memory background jobs: download → convert → transcribe → summarise (or read a document → summarise).

Two lines, one job at a time in each so a small free-tier server isn't overwhelmed: audio and video
(slow: converting and transcribing), and quick jobs (text the page already has, and documents), so a
quick job never waits behind someone's hour-long recording.
Jobs live in memory only — the browser keeps the results once they're done.
"""

import logging
import shutil
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from . import audio, config, documents, placement, slides, summarize, transcribe
from .utils import AppError

log = logging.getLogger(__name__)

JOB_TTL_SECONDS = 6 * 3600

_lanes = {"audio": ThreadPoolExecutor(max_workers=1), "quick": ThreadPoolExecutor(max_workers=1)}
_jobs: dict[str, "Job"] = {}
_lock = threading.Lock()


@dataclass
class Job:
    id: str
    label: str
    status: str = "queued"  # queued | downloading | converting | transcribing | reading | summarizing | done | error
    message: str = "Waiting in line…"
    transcript: str = ""
    notes: dict | None = None
    error: str = ""
    warning: str = ""  # something non-fatal the user should know (e.g. unreadable slides)
    source: str = "recording"  # "recording" or "document" (a PDF, Word file… summarised directly)
    placement: dict | None = None  # who/which class/where in Notion, worked out while the notes were written
    lane: str = "audio"  # which line it waits in: "audio" or "quick"
    created: float = field(default_factory=time.time)

    def update(self, status: str | None = None, message: str | None = None) -> None:
        if status:
            self.status = status
        if message:
            self.message = message

    def public(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "status": self.status,
            "message": _waiting_message(self) if self.status == "queued" else self.message,
            "transcript": self.transcript,
            "notes": self.notes,
            "error": self.error,
            "warning": self.warning,
            "source": self.source,
            "placement": self.placement,
        }


def _waiting_message(job: Job) -> str:
    """What a queued job is waiting for, e.g. "Waiting for 1 other file to finish (Transcribing… 2 of 5 parts done)"."""
    with _lock:
        ahead = [j for j in _jobs.values() if j.lane == job.lane and j.id != job.id
                 and j.status not in ("done", "error") and j.created <= job.created]
    if not ahead:
        return "Starting…"
    running = next((j for j in ahead if j.status != "queued"), None)
    files = "1 other file" if len(ahead) == 1 else f"{len(ahead)} other files"
    return f"Waiting for {files} to finish first" + (f" ({running.message})" if running else "")


def busy() -> int:
    """How many jobs are waiting or running (restarting the server would interrupt them)."""
    with _lock:
        return sum(j.status not in ("done", "error") for j in _jobs.values())


def new_workdir() -> Path:
    return Path(tempfile.mkdtemp(prefix="notes-"))


def get(job_id: str) -> Job | None:
    return _jobs.get(job_id)


@dataclass
class Options:
    slides_path: Path | None = None
    extras: list[str] = field(default_factory=list)
    place: bool = True          # also work out where in Notion it goes, alongside the notes
    usual_person_id: str = ""   # who this device usually sends notes for (breaks ties)
    source: str = "recording"   # for text sent by the page: "recording" (live transcript) or "document"
    parts: list[dict] | None = None  # notes the page had written during the recording...
    parts_chars: int = 0             # ...for this much of the transcript


def submit_file(path: Path, workdir: Path, label: str, options: Options) -> Job:
    return _submit(label, workdir, options, path=path)


def submit_url(url: str, workdir: Path, options: Options) -> Job:
    return _submit(url, workdir, options, url=url)


def submit_text(text: str, workdir: Path, label: str, options: Options) -> Job:
    """A recording that was already transcribed while it was being made: just write the notes."""
    return _submit(label, workdir, options, text=text)


def _submit(label: str, workdir: Path, options: Options, path: Path | None = None, url: str | None = None,
            text: str | None = None) -> Job:
    quick = text is not None or (path is not None and documents.is_document(path))
    job = Job(id=uuid.uuid4().hex, label=label, lane="quick" if quick else "audio")
    with _lock:
        cutoff = time.time() - JOB_TTL_SECONDS
        for old_id in [k for k, j in _jobs.items() if j.created < cutoff]:
            del _jobs[old_id]
        _jobs[job.id] = job
    _lanes[job.lane].submit(_run, job, workdir, options, path, url, text)
    return job


PLACEMENT_WAIT_SECONDS = 30  # after the notes are done, how long to wait for the Notion guess


def _start_placement(job: Job, text: str, options: Options) -> threading.Thread | None:
    """Guess whose lecture it is and where in Notion it goes, while the notes are being written."""
    if not options.place or not config.NOTION_TOKEN:
        return None

    def work():
        try:
            job.placement = placement.prepare(text, options.usual_person_id)
        except Exception:  # only a head start: the page works it out itself if this fails
            log.warning("Placement for job %s failed", job.id, exc_info=True)

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    return thread


def _run(job: Job, workdir: Path, options: Options, path: Path | None, url: str | None, text: str | None) -> None:
    progress = lambda msg: job.update(message=msg)  # noqa: E731
    slides_path, extras = options.slides_path, options.extras

    def warn(message: str) -> None:
        job.warning = " ".join(filter(None, [job.warning, message]))

    try:
        if text is not None:
            job.source = options.source  # transcribed live, or a scan the page read itself
        elif path and documents.is_document(path):
            # A document (PDF, Word…): no audio to transcribe, its text is what gets summarised.
            job.source = "document"
            job.update("reading", "Reading the document…")
            text = documents.full_text(path, warn, progress)
        else:
            if url:
                job.update("downloading", "Downloading audio from the link…")
                path, title = audio.download(url, workdir)
                job.label = title
            job.update("converting", "Preparing audio…")
            clean = audio.normalize(path, workdir)

            job.update("transcribing", "Transcribing…")
            text = transcribe.transcribe(clean, workdir, progress)
            if not text.strip():
                raise AppError("No speech was detected in the audio.")
        job.transcript = text
        placing = _start_placement(job, text, options)

        slides_text = ""
        if slides_path:
            job.update("summarizing", "Reading the slides…")
            try:
                slides_text = slides.extract_text(slides_path, progress)
            except AppError as e:  # slides are optional: carry on without them
                job.warning = str(e)

        job.update("summarizing", "Writing notes…")
        job.notes = summarize.make_notes(text, progress, slides_text, extras, warn, source=job.source,
                                         parts=options.parts, parts_chars=options.parts_chars)
        if placing:
            progress("Finding where it goes in Notion…")
            placing.join(PLACEMENT_WAIT_SECONDS)
        job.update("done", "Done")
    except AppError as e:
        job.error = str(e)
        job.update("error", "Failed")
    except Exception as e:  # unexpected — log the traceback, show a short message
        log.exception("Job %s failed", job.id)
        job.error = f"Something went wrong: {e}"
        job.update("error", "Failed")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
