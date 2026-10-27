# Lecture Notes: notes for AI agents

Shared by Claude (Claude Code) and ChatGPT (Codex). Both read this file before working here, so
keep it current: if you change how something works, update this file in the same commit.

**This repo is public.** Never commit keys, tokens, passwords or anyone's personal details.

## What it is
A web app that records or uploads a lecture, meeting or document, transcribes it (Whisper on
Groq), writes AI notes (Groq models), and sends them to Notion. Full details are in `README.md`.
- `backend/app/` FastAPI server
- `frontend/` the website (plain HTML/JS, no build step)
- `tests/` pytest tests (use the fakes in `tests/fakes.py`; no real network calls)
- Hosted on Render's free tier from `render.yaml` (Docker), redeploys by itself on push to `main`
- Several people sign in with their own name (`PEOPLE`), each writing to their own Notion.
  Don't break other people's setups.

## Changing it
- Run before pushing: `python -m pytest -q` and `for f in frontend/*.js; do node --check "$f"; done`
  (the same checks as `.github/workflows/tests.yml`).
- Keep everything on free tiers, and keep AI usage low (see "Speed and AI usage" in `README.md`).
- Render's free hours are shared with the dashboard app.
- Only one AI works in this repo at a time. Pull first, push to `main` when done.
- Commit messages: plain English, describing what changed for the people using it.
- When done, reply with the finished result. No step-by-step commentary.
