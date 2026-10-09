"""Whisper transcription — hosted on Groq (default) or run locally with faster-whisper."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import audio, config, groq
from .groq import Progress
from .utils import AppError

# 10-minute chunks at 32 kbps ≈ 2.4 MB each, far under Groq's 25 MB free-tier upload limit.
GROQ_CHUNK_SECONDS = 600
# Long recordings: transcribe this many pieces at once (Groq's free tier allows 20 requests a minute).
PARALLEL_PIECES = 3

_local_model = None


def transcribe(path: Path, workdir: Path, progress: Progress, max_wait: float = groq.MAX_WAIT_SECONDS) -> str:
    """`max_wait`: how long to wait if Groq's free transcription limits are used up on every model."""
    if config.TRANSCRIBE_BACKEND == "local":
        return _local(path, progress)
    if config.TRANSCRIBE_BACKEND != "groq":
        raise AppError("TRANSCRIBE_BACKEND must be 'groq' or 'local'.")
    return _groq(path, workdir, progress, max_wait)


def _groq(path: Path, workdir: Path, progress: Progress, max_wait: float) -> str:
    chunks = audio.split(path, workdir, GROQ_CHUNK_SECONDS)
    if len(chunks) == 1:
        progress("Transcribing…")
        return groq.transcribe_file(chunks[0], progress, max_wait)
    done = 0

    def one(chunk: Path) -> str:
        nonlocal done
        text = groq.transcribe_file(chunk, progress, max_wait)
        done += 1
        progress(f"Transcribing… {done} of {len(chunks)} parts done")
        return text

    progress(f"Transcribing {len(chunks)} parts…")
    with ThreadPoolExecutor(max_workers=PARALLEL_PIECES) as pool:
        texts = list(pool.map(one, chunks))  # keeps the original order
    return " ".join(t for t in texts if t)


def _local(path: Path, progress: Progress) -> str:
    global _local_model
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise AppError("TRANSCRIBE_BACKEND=local but faster-whisper isn't installed (see README).")
    if _local_model is None:
        progress(f"Loading Whisper '{config.LOCAL_WHISPER_MODEL}' model (first run downloads it)…")
        _local_model = WhisperModel(config.LOCAL_WHISPER_MODEL, device="cpu", compute_type="int8")
    segments, info = _local_model.transcribe(str(path), vad_filter=True, beam_size=1)
    texts = []
    for seg in segments:
        texts.append(seg.text.strip())
        if info.duration:
            progress(f"Transcribing locally… {min(99, int(seg.end / info.duration * 100))}%")
    return " ".join(t for t in texts if t)
