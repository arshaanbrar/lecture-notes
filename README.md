# 📝 Lecture Notes

Record or upload a lecture or meeting. The app gives you:

1. A full **transcript** (Whisper)
2. **AI notes**: summary, key points, action items (Llama 3 on Groq, falling back to Groq's free `gpt-oss` models if your account can't use Llama)
3. A **Notion page** with the notes and transcript, either new or added to an existing page

Everything runs on free tiers: Render (hosting), Groq (Whisper + Llama 3) and the Notion API.

---

## ⚠️ Where your keys go

You never edit code to add keys. All keys are environment variables:

| Variable | What it is | Where to get it | Required? |
|---|---|---|---|
| `GROQ_API_KEY` | Groq API key (`gsk_…`) | [Step 1](#step-1--get-a-free-groq-api-key) | **Yes** |
| `NOTION_TOKEN` | Notion integration secret (`ntn_…`) | [Step 2](#step-2--connect-notion-one-time-for-everyone) | For Notion export |
| `NOTION_PARENT_PAGE_ID` | A fallback page offered as a last-resort place for notes (paste its URL) | [Step 2](#step-2--connect-notion-one-time-for-everyone) | Optional |
| `APP_PASSWORD` | Optional password that every visitor must enter | You make it up | Recommended |

- **Locally:** copy `.env.example` to `.env` and fill it in. `.env` is git-ignored.
- **On Render:** use your service's **Environment** tab. The Blueprint setup also asks for these values.

> 🔒 **Set `APP_PASSWORD` before sharing the link.** Without it, anyone who finds your URL can use up your free Groq quota and write into your Notion.

---

## How it works

```
Browser (HTML/JS)                         FastAPI server (Docker on Render)
─────────────────                         ─────────────────────────────────
Record mic / system / both ──upload──▶  ffmpeg → 16 kHz mono MP3, 10-min chunks
Upload file / paste link   ──────────▶  yt-dlp downloads links
                                         Whisper (Groq) → transcript
           ◀──── polls job status ────  Llama 3 (Groq) → summary / key points / action items
Send to Notion             ──────────▶  Notion API (one shared integration)
```

```
lecture-notes/
├── backend/app/
│   ├── main.py        # API routes + serves the frontend
│   ├── config.py      # reads env vars (⚠️ keys)
│   ├── jobs.py        # background pipeline: download → convert → transcribe → summarise
│   ├── audio.py       # ffmpeg + yt-dlp
│   ├── transcribe.py  # Whisper via Groq (default) or local faster-whisper
│   ├── summarize.py   # Llama 3 notes, chunked for long transcripts
│   ├── groq.py        # Groq client with retries for free-tier rate limits
│   ├── notion.py      # list pages, create page, append to page
│   └── utils.py
├── frontend/          # index.html, styles.css, app.js (no build step)
├── Dockerfile         # installs ffmpeg
├── render.yaml        # one-click Render Blueprint
├── requirements.txt
└── .env.example       # ⚠️ copy to .env and paste keys
```

### Why Whisper runs on Groq by default

Render's free tier provides 512 MB RAM and a small share of one CPU. Running Whisper on it would take hours for one lecture, or crash. Groq hosts the same open-source Whisper model (`whisper-large-v3-turbo`) for free, and it transcribes an hour of audio in under a minute. If you have a server with real CPU, you can run Whisper yourself; see [Running Whisper locally](#optional-running-whisper-locally).

### Why Notion uses one shared connection

You want every visitor to use the same Notion account, so the app uses a Notion **internal integration** instead of per-user login. You set it up once and add the token to the server, and everyone who uses the site writes to your workspace. Visitors never sign in to Notion.

---

## Step 1: Get a free Groq API key

1. Go to **https://console.groq.com** and sign up (Google, GitHub or email). No credit card is needed.
2. Open **API Keys** in the left sidebar (direct link: https://console.groq.com/keys).
3. Click **Create API Key**, name it (e.g. `lecture-notes`) and click **Submit**.
4. **Copy the key now.** It starts with `gsk_` and is shown only once.
5. ⚠️ Paste it as `GROQ_API_KEY`.

You can see your free-tier limits at https://console.groq.com/settings/limits. They're per model, and Groq changes them over time. The app chunks audio and text to stay under those limits and automatically waits and retries when it gets rate-limited.

## Step 2: Connect Notion (one time, for everyone)

**A. Create the integration**

1. Go to **https://www.notion.so/profile/integrations** and click **New integration**.
2. Name it (e.g. `Lecture Notes`), choose your workspace, set the type to **Internal**, and save.
3. On the integration page, check that **Read content**, **Update content** and **Insert content** are enabled under Capabilities.
4. Next to **Internal Integration Secret**, click **Show**, then **Copy**. It starts with `ntn_`.
5. ⚠️ Paste it as `NOTION_TOKEN`.

**B. Choose where new notes go**

1. In Notion, create a page called something like **📚 Lecture Notes**. Every "Create a new page" export becomes a sub-page of it.
2. On that page, click **•••** (top right) → **Connections** → find your integration → **Confirm**.
3. Copy the page link (**Share** → **Copy link**, or copy it from the address bar).
4. ⚠️ Paste the full link as `NOTION_PARENT_PAGE_ID`. The app pulls the ID out of the link.

**C. (Optional) Allow "Add to an existing page"**

The integration can only see pages that are shared with it. The parent page from step B and all its sub-pages are already covered. To append notes to other pages, such as a page per course, add the connection to those pages (or a page above them) the same way: **•••** → **Connections**.

## Step 3: Run locally (optional, for testing)

You need Python 3.10+ and ffmpeg (`brew install ffmpeg` on Mac).

```bash
cd lecture-notes
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # ⚠️ then open .env and paste your keys
uvicorn backend.app.main:app --reload
```

Open http://localhost:8000. Browsers only allow recording on `localhost` or `https://`, so open it at that address rather than via an IP.

## Step 4: Deploy free on Render

1. **Put the code on GitHub.** Create a new repo at https://github.com/new (private is fine), then run:
   ```bash
   cd lecture-notes
   git init && git add . && git commit -m "Lecture Notes app"
   git branch -M main
   git remote add origin https://github.com/YOUR-USERNAME/lecture-notes.git
   git push -u origin main
   ```
   Your `.env` won't be uploaded because it's in `.gitignore`.
2. Sign up at **https://render.com** with your GitHub account. No credit card is needed for the free plan.
3. In the Render dashboard, click **New +** → **Blueprint**, connect your GitHub account if asked, and pick your `lecture-notes` repo.
4. Render reads `render.yaml` and asks for the secret values:
   - ⚠️ `GROQ_API_KEY`: your Groq key
   - ⚠️ `NOTION_TOKEN`: your Notion secret
   - ⚠️ `NOTION_PARENT_PAGE_ID`: your parent page link
   - ⚠️ `APP_PASSWORD`: a password to share with your users (or leave it blank)
5. Click **Apply**. The first build takes about 3–6 minutes. Then open the `https://lecture-notes-xxxx.onrender.com` URL shown on the service page.

To change a key later, go to your service → **Environment** → edit the value → **Save changes**. Render redeploys automatically. Every `git push` to `main` also redeploys.

**Without a Blueprint:** **New +** → **Web Service** → pick the repo → Language **Docker** → Instance type **Free** → add the environment variables above → **Deploy**.

### Free-tier things to know

- **The site goes to sleep.** Render stops free services after 15 minutes with no visitors. The next visit takes 30–60 seconds to wake it up, and the page shows a "couldn't reach the server" message until it's awake. This is normal.
- **Long recordings are fine.** The browser records locally, and the server only works on the file after you click *Transcribe*. Use **Save recording** if you want a backup copy of the audio.
- **One job at a time.** Jobs queue up, which keeps the small free server from running out of memory.
- **Results aren't stored on the server.** Results stay in your browser tab, and in Notion once exported. A server restart (deploy or sleep) cancels in-progress jobs.
- **Railway:** it no longer has a permanent free plan (only trial credits), so this project targets Render.

---

## Using the app

### Recording sources

| Button | What it records | Browsers |
|---|---|---|
| **Mic only** | Your microphone | All modern browsers, including phones |
| **System audio only** | Audio from a tab, window or the whole screen | Chrome / Edge / Opera on a computer |
| **Both** | Mic and system audio mixed together | Chrome / Edge / Opera on a computer |

For system audio, the browser shows a share dialog. **Pick a tab, window or screen and turn on "Share audio".** Sharing a **browser tab** (e.g. Zoom/Meet/Teams on the web, or a YouTube lecture) is the most reliable option. On macOS, sharing a whole window or screen may not include audio in every version, so a tab is the safest choice. Firefox and Safari can't capture system audio, so the app disables those buttons there.

### Sending to Notion

After the notes are written, the **Send to Notion** box asks two things:

1. **Who is it for?** A name from the top level of the shared Notion (one page per person). Each device remembers the last choice.
2. **Which class?** The site lists the classes it finds in that person's Notion, e.g. the entries of their *Courses*, *Classes* or *Domains* table. The AI pre-selects the class that matches the lecture. If no class list is found, pick **Something else** and type the class name (e.g. `csc108`).

The AI then suggests where the notes should go, preferring where that person's lectures for the class already live:

- a **lectures/topics table linked to the class**: a new entry, with the class link, a `lecture` type and today's date filled in when the table has those columns;
- the page where their **other lecture pages** for the class are;
- **inside the class page**, or adding to their **latest lecture**.

The Notion page gets the lecture's title (the one at the top of the notes, which you can edit). Check the suggestion, then click **Send to Notion**, or open **Put it somewhere else** to choose another of the suggested places. Nothing is written until you click Send.

### Upload / link

- **Files:** any audio or video format ffmpeg reads (mp3, m4a, wav, mp4, mov, webm, mkv, …), up to `MAX_UPLOAD_MB` (default 300 MB).
- **Links:** YouTube, Vimeo, Loom, Google Drive files shared publicly, direct `.mp3`/`.mp4` links, and [many other sites](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md). The link must be public. YouTube sometimes blocks downloads from cloud servers. If a YouTube link fails on Render, download the video yourself and upload the file instead.

---

## Optional: Running Whisper locally

To run Whisper yourself instead of using Groq's hosted copy, use a machine or paid server with at least 2 GB RAM. The app uses [faster-whisper](https://github.com/SYSTRAN/faster-whisper), which runs the official OpenAI Whisper weights about 4× faster on CPU.

```bash
pip install -r requirements-local-whisper.txt
# in .env:
TRANSCRIBE_BACKEND=local
LOCAL_WHISPER_MODEL=base     # tiny | base | small | medium | large-v3 (bigger = slower but more accurate)
```

With Docker, build with `--build-arg LOCAL_WHISPER=true`. Groq is still used for the notes.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| "No audio was shared" | In the share dialog, turn on **Share audio**. Choose a browser tab if the option isn't shown. |
| System audio is silent | Make sure the shared tab is actually playing sound. The level bar should move. |
| "Groq free-tier limit hit, waiting…" | Normal for long recordings. The app waits and retries automatically. |
| Notion: "Could not find page…" | Share that page with your integration: **•••** → **Connections**. |
| Notion page list is empty | Only pages shared with the integration appear. See Step 2C. |
| "Couldn't reach the server" | The free server is waking up. Wait about a minute and refresh. |
| "None of the Groq models are available" | Groq retires models or makes them paid-only over time. The app tries several automatically; if all fail, pick a current one from https://console.groq.com/docs/models and set `GROQ_MODEL` (comma-separated list, tried in order). |
| Link downloads started failing | Update yt-dlp: redeploy on Render (it installs the latest) or run `pip install -U yt-dlp` locally. |

---

Built by Arshaan
