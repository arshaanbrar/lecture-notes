"""In-memory background jobs: download → convert → transcribe → summarise.

One job runs at a time so a small free-tier server isn't overwhelmed; others wait in line.
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

from . import audio, slides, summarize, transcribe
from .utils import AppError

log = logging.getLogger(__name__)

JOB_TTL_SECONDS = 6 * 3600

_executor = ThreadPoolExecutor(max_workers=1)
_jobs: dict[str, "Job"] = {}
_lock = threading.Lock()


@dataclass
class Job:
    id: str
    label: str
    status: str = "queued"  # queued | downloading | converting | transcribing | summarizing | done | error
    message: str = "Waiting in line…"
    transcript: str = ""
    notes: dict | None = None
    error: str = ""
    warning: str = ""  # something non-fatal the user should know (e.g. unreadable slides)
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
            "message": self.message,
            "transcript": self.transcript,
            "notes": self.notes,
            "error": self.error,
            "warning": self.warning,
        }


def new_workdir() -> Path:
    return Path(tempfile.mkdtemp(prefix="notes-"))


def get(job_id: str) -> Job | None:
    return _jobs.get(job_id)


def submit_file(path: Path, workdir: Path, label: str, slides_path: Path | None = None) -> Job:
    return _submit(label, workdir, path=path, slides_path=slides_path)


def submit_url(url: str, workdir: Path, slides_path: Path | None = None) -> Job:
    return _submit(url, workdir, url=url, slides_path=slides_path)


def _submit(label: str, workdir: Path, path: Path | None = None, url: str | None = None,
            slides_path: Path | None = None) -> Job:
    job = Job(id=uuid.uuid4().hex, label=label)
    with _lock:
        cutoff = time.time() - JOB_TTL_SECONDS
        for old_id in [k for k, j in _jobs.items() if j.created < cutoff]:
            del _jobs[old_id]
        _jobs[job.id] = job
    _executor.submit(_run, job, workdir, path, url, slides_path)
    return job


def _run(job: Job, workdir: Path, path: Path | None, url: str | None, slides_path: Path | None) -> None:
    progress = lambda msg: job.update(message=msg)  # noqa: E731
    try:
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

        slides_text = ""
        if slides_path:
            job.update("summarizing", "Reading the slides…")
            try:
                slides_text = slides.extract_text(slides_path)
            except AppError as e:  # slides are optional: carry on without them
                job.warning = str(e)

        job.update("summarizing", "Writing notes…")
        job.notes = summarize.make_notes(text, progress, slides_text)
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
