"""Audio helpers: download from links (yt-dlp) and convert/split with ffmpeg."""

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import httpx
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


class TooShort(AppError):
    """The audio is empty or too short: nothing to transcribe, but nothing is broken either."""


def duration(path: Path) -> float:
    out = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=nw=1:nk=1", str(path)])
    try:
        return float(out.strip())
    except ValueError:
        return 0.0


def normalize(src: Path, workdir: Path, skip_seconds: float = 0) -> Path:
    """Convert any audio/video into small 16 kHz mono MP3 — all Whisper needs (~14 MB per hour).
    `skip_seconds` drops audio from the start (e.g. a repeated header piece)."""
    out = workdir / "audio.mp3"
    skip = ["-ss", f"{skip_seconds:.2f}"] if skip_seconds > 0 else []
    _run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), *skip,
          "-vn", "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame", "-b:a", "32k", str(out)])
    if duration(out) < 0.5:
        raise TooShort("The audio is empty or too short to transcribe.")
    return out


def split(path: Path, workdir: Path, seconds: int) -> list[Path]:
    """Split into `seconds`-long chunks (no re-encoding). Returns [path] if it's already short."""
    if duration(path) <= seconds + 5:
        return [path]
    _run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(path),
          "-f", "segment", "-segment_time", str(seconds), "-c", "copy", "-reset_timestamps", "1",
          str(workdir / "chunk_%03d.mp3")])
    return sorted(workdir.glob("chunk_*.mp3"))


# YouTube asks cloud servers (like this one on Render) to "sign in to confirm you're not a bot". yt-dlp can
# ask YouTube as different YouTube apps, and the check doesn't hit them all the same, so after the usual
# way these are tried in turn.
YOUTUBE_CLIENTS = ["tv_simply", "web_embedded", "mweb", "android_vr", "ios", "tv", "web_safari"]


@dataclass
class Download:
    path: Path | None  # the audio, or None when only the captions could be had
    title: str
    captions: str = ""  # the video's own captions, used as the transcript when the audio was blocked


def _is_youtube(url: str) -> bool:
    return bool(re.search(r"(^|\.|//)(youtube\.com|youtu\.be)/", url))


def _ydl_options(workdir: Path, client: str | None, cookies: Path | None, download: bool) -> dict:
    opts = {
        "format": "bestaudio/best",
        "outtmpl": str(workdir / "download.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "max_filesize": config.MAX_UPLOAD_MB * 1024 * 1024,
        "skip_download": not download,
    }
    if client:
        opts["extractor_args"] = {"youtube": {"player_client": [client]}}
    if cookies:
        opts["cookiefile"] = str(cookies)
    return opts


def download(url: str, workdir: Path) -> Download:
    """The audio from a link (YouTube, Vimeo, direct .mp3/.mp4 links and ~1000 other sites). For YouTube,
    if the audio can't be had from this server, the video's own captions instead (Download.captions)."""
    youtube = _is_youtube(url)
    cookies = None
    if youtube and config.YOUTUBE_COOKIES:
        cookies = workdir / "youtube-cookies.txt"
        cookies.write_text(config.YOUTUBE_COOKIES)
    attempts = [None] + (YOUTUBE_CLIENTS if youtube else [])
    first_error, captions = "", None
    for client in attempts:
        try:
            with yt_dlp.YoutubeDL(_ydl_options(workdir, client, cookies, True)) as ydl:
                info = ydl.extract_info(url, download=True)
                path = Path(ydl.prepare_filename(info))
        except yt_dlp.utils.DownloadError as e:
            first_error = first_error or str(e).replace("ERROR: ", "").strip()
            if youtube and captions is None:
                captions = _captions(url, workdir, client, cookies)
            continue
        if not path.exists():
            found = sorted(workdir.glob("download.*"))
            if not found:
                raise AppError("Couldn't download that link (it may be private or too large).")
            path = found[0]
        return Download(path, info.get("title") or "Linked recording")
    if captions:
        return captions
    if youtube and re.search(r"not a bot|sign in to confirm", first_error, re.I):
        raise AppError("YouTube is blocking downloads from this server right now (it asks cloud servers to "
                       "sign in), and this video has no captions to use instead. Download the video and upload "
                       "the file here: videos are shrunk to just their sound first, so it's quick.")
    raise AppError(f"Couldn't download that link: {first_error[:300]}")


def _captions(url: str, workdir: Path, client: str | None, cookies: Path | None) -> Download | None:
    """The video's captions as plain text (its own, else YouTube's automatic ones), or None."""
    try:
        with yt_dlp.YoutubeDL(_ydl_options(workdir, client, cookies, False)) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError:
        return None
    for tracks in (info.get("subtitles") or {}, info.get("automatic_captions") or {}):
        lang = next((code for code in tracks if code in ("en", "en-US", "en-GB")), None) \
            or next((code for code in tracks if code.startswith("en")), None)
        formats = {f.get("ext"): f.get("url") for f in tracks.get(lang, []) if f.get("url")}
        if not formats:
            continue
        try:
            if formats.get("json3"):
                text = _json3_text(httpx.get(formats["json3"], timeout=30).json())
            elif formats.get("vtt"):
                text = vtt_text(httpx.get(formats["vtt"], timeout=30).text)
            else:
                continue
        except (httpx.HTTPError, ValueError):
            continue
        if len(text.split()) >= 20:
            return Download(None, info.get("title") or "Linked recording", text)
    return None


def _json3_text(data: dict) -> str:
    words = "".join(seg.get("utf8", "") for event in data.get("events", []) for seg in event.get("segs") or [])
    return re.sub(r"\s+", " ", words).strip()


def vtt_text(vtt: str) -> str:
    """Plain text from WebVTT captions: no header, timings or tags, and no lines repeated (YouTube's
    automatic captions repeat each line as the next one scrolls in)."""
    lines = []
    for line in vtt.splitlines():
        line = re.sub(r"<[^>]+>", "", line).strip()
        if not line or line == "WEBVTT" or "-->" in line or re.match(r"^(Kind|Language|NOTE)\b", line):
            continue
        if not lines or lines[-1] != line:
            lines.append(line)
    return re.sub(r"\s+", " ", " ".join(lines)).strip()
