"""YouTube links from a cloud server: other YouTube apps, then the video's captions, then a clear message."""

import httpx
import pytest
import yt_dlp

from backend.app import audio, config
from backend.app.utils import AppError

URL = "https://www.youtube.com/watch?v=fOrOeZA-vdY"
BOT = "ERROR: [youtube] fOrOeZA-vdY: Sign in to confirm you’re not a bot. Use --cookies-from-browser"


def fake_ytdl(monkeypatch, tmp_path, works_for=(), info=None):
    """yt-dlp where only the YouTube apps in `works_for` get past the bot check."""
    tried = []

    class FakeYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=True):
            client = (self.opts.get("extractor_args") or {}).get("youtube", {}).get("player_client", ["default"])[0]
            tried.append((client, download, "cookiefile" in self.opts))
            if download and client in works_for:
                (tmp_path / "download.m4a").write_bytes(b"audio")
                return {"title": "Evaluating Limits By Factoring", "ext": "m4a"}
            if not download and info is not None:
                return info
            raise yt_dlp.utils.DownloadError(BOT)

        def prepare_filename(self, info):
            return str(tmp_path / "download.m4a")
    monkeypatch.setattr(audio.yt_dlp, "YoutubeDL", FakeYDL)
    return tried


def test_when_youtube_blocks_the_usual_way_other_youtube_apps_are_tried(monkeypatch, tmp_path):
    tried = fake_ytdl(monkeypatch, tmp_path, works_for={"web_embedded"})
    got = audio.download(URL, tmp_path)
    assert got.path == tmp_path / "download.m4a" and got.title == "Evaluating Limits By Factoring"
    assert [c for c, download, _ in tried if download] == ["default", "tv_simply", "web_embedded"]


def test_when_the_audio_stays_blocked_the_captions_are_used(monkeypatch, tmp_path):
    info = {"title": "Evaluating Limits", "automatic_captions": {"en": [{"ext": "json3", "url": "https://captions"}]}}
    fake_ytdl(monkeypatch, tmp_path, info=info)
    events = {"events": [{"segs": [{"utf8": "today we evaluate limits "}, {"utf8": "by factoring"}]},
                         {"segs": [{"utf8": "\n"}, {"utf8": "the numerator and cancel common factors " * 4}]}]}
    monkeypatch.setattr(audio.httpx, "get", lambda url, timeout: httpx.Response(200, json=events))
    got = audio.download(URL, tmp_path)
    assert got.path is None and got.title == "Evaluating Limits"
    assert got.captions.startswith("today we evaluate limits by factoring the numerator")


def test_when_nothing_works_the_message_says_what_to_do(monkeypatch, tmp_path):
    fake_ytdl(monkeypatch, tmp_path)
    with pytest.raises(AppError, match="YouTube is blocking downloads from this server.*upload the file"):
        audio.download(URL, tmp_path)


def test_youtube_cookies_are_used_when_set(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "YOUTUBE_COOKIES", "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tx")
    tried = fake_ytdl(monkeypatch, tmp_path, works_for={"default"})
    audio.download(URL, tmp_path)
    assert tried[0] == ("default", True, True)


def test_other_sites_are_tried_once(monkeypatch, tmp_path):
    tried = fake_ytdl(monkeypatch, tmp_path)
    with pytest.raises(AppError, match="Couldn't download that link"):
        audio.download("https://vimeo.com/123", tmp_path)
    assert len(tried) == 1


def test_youtube_caption_files_become_plain_text():
    vtt = ("WEBVTT\nKind: captions\nLanguage: en\n\n00:00:00.000 --> 00:00:02.000\nso today we're<00:00:01.000><c> going</c>\n\n"
           "00:00:02.000 --> 00:00:04.000\nso today we're going\nto factor limits\n")
    assert audio.vtt_text(vtt) == "so today we're going to factor limits"


def test_a_link_job_uses_the_captions_when_the_audio_is_blocked(client, fake_ai, monkeypatch):
    from tests.conftest import wait_for_job
    monkeypatch.setattr(audio, "download", lambda url, workdir: audio.Download(None, "Limits", "factor the top " * 30))
    job = wait_for_job(client, client.post("/api/jobs/url", data={"url": URL, "place": "false"}).json()["id"])
    assert job["status"] == "done", job["error"]
    assert job["transcript"].startswith("factor the top") and "captions were used" in job["warning"]
    assert job["label"] == "Limits"
