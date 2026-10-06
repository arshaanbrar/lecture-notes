"""Audio helpers: download from links (yt-dlp) and convert/split with ffmpeg."""

import subprocess
from pathlib import Path

import yt_dlp

from . import config
from .utils import AppError


def _run(cmd: list[str]) -> str:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        raise AppError("ffmpeg isn't installed on the server (see README → Run locally).")
    if proc.returncode != 0:
        tail = " | ".join(proc.stderr.strip().splitlines()[-3:])
        raise AppError(f"Couldn't read that audio/video file ({tail or 'ffmpeg error'}).")
    return proc.stdout


def duration(path: Path) -> float:
    out = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=nw=1:nk=1", str(path)])
    try:
        return float(out.strip())
    except ValueError:
        return 0.0


def normalize(src: Path, workdir: Path) -> Path:
    """Convert any audio/video into small 16 kHz mono MP3 — all Whisper needs (~14 MB per hour)."""
    out = workdir / "audio.mp3"
    _run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
          "-vn", "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame", "-b:a", "32k", str(out)])
    if duration(out) < 0.5:
        raise AppError("The audio is empty or too short to transcribe.")
    return out


def split(path: Path, workdir: Path, seconds: int) -> list[Path]:
    """Split into `seconds`-long chunks (no re-encoding). Returns [path] if it's already short."""
    if duration(path) <= seconds + 5:
        return [path]
    _run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(path),
          "-f", "segment", "-segment_time", str(seconds), "-c", "copy", "-reset_timestamps", "1",
          str(workdir / "chunk_%03d.mp3")])
    return sorted(workdir.glob("chunk_*.mp3"))


def download(url: str, workdir: Path) -> tuple[Path, str]:
    """Download the audio from a link (YouTube, Vimeo, direct .mp3/.mp4 URLs, and ~1000 other sites)."""
    opts = {
        "format": "bestaudio/best",
        "outtmpl": str(workdir / "download.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "max_filesize": config.MAX_UPLOAD_MB * 1024 * 1024,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            path = Path(ydl.prepare_filename(info))
    except yt_dlp.utils.DownloadError as e:
        msg = str(e).replace("ERROR: ", "").strip()
        raise AppError(f"Couldn't download that link: {msg[:300]}")
    if not path.exists():
        found = sorted(workdir.glob("download.*"))
        if not found:
            raise AppError("Couldn't download that link (it may be private or too large).")
        path = found[0]
    return path, (info.get("title") or "Linked recording")
