# 📝 Lecture Notes

Record or upload a lecture or meeting, or drop in a document. The app gives you:

1. A full **transcript** (Whisper)
2. **AI notes**: summary, key points, key terms and action items, plus optional study extras (see below) (Llama 3 on Groq, falling back to Groq's free `gpt-oss` models if your account can't use Llama)
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
| `HIDDEN_PEOPLE` | Names of people's top-level Notion pages to leave off the site, comma-separated (default `Ryan`). Their Notion isn't changed | — | Optional |
| `GROQ_FAST_MODEL` | Small quick models for "whose lecture / which class / where", comma-separated, tried in order (default `openai/gpt-oss-20b,llama-3.1-8b-instant`). They have their own free-tier limits, so they don't eat into the notes model's | — | Optional |
| `GROQ_REASONING_EFFORT` | How much gpt-oss models "think" before answering: `low` (default) is faster and uses far fewer tokens | — | Optional |
| `APP_PASSWORD` | Optional password that every visitor must enter | You make it up | Recommended |

- **Locally:** copy `.env.example` to `.env` and fill it in. `.env` is git-ignored.
- **On Render:** use your service's **Environment** tab. The Blueprint setup also asks for these values.

> 🔒 **Set `APP_PASSWORD` before sharing the link.** Without it, anyone who finds your URL can use up your free Groq quota and write into your Notion.

---

## How it works

```
Browser (HTML/JS)                         FastAPI server (Docker on Render)
─────────────────                         ─────────────────────────────────
Record mic / system / both
  every 3 min: new audio   ──────────▶  ffmpeg → Whisper (Groq) → that part of the transcript
  once there's enough       ──────────▶  AI guesses who / which class, then the Notion spot
  stop: transcript text    ──────────▶  notes (Groq)
Upload file / paste link   ──────────▶  yt-dlp downloads links; ffmpeg → 10-min pieces,
                                         Whisper 3 at a time; notes + Notion guess side by side
Upload a document          ──────────▶  text read (OCR for scans); notes + Notion guess side by side
           ◀──── polls job status ────
Send to Notion             ──────────▶  Notion API (one shared integration)
```

**Speed and AI usage.** A recording is transcribed every 3 minutes while it's being made. When you stop, only the last few minutes are left: there's no big upload, and the lecture isn't transcribed twice (the study helper chat reuses the same transcript). While recording, the AI also guesses whose lecture it is and which class, checking again as it goes until two guesses in a row agree, then works out the Notion spot. So when the notes are ready, Send to Notion is too. For uploads, links and documents, the server does the same guess while the notes are being written. To save AI usage:
- quick questions use a small, fast model;
- gpt-oss models "think" less (`GROQ_REASONING_EFFORT=low`);
- short lectures get their notes and study extras in one AI call;
- the AI is only asked where to put the notes when it's a close call.

Every ~15 minutes of transcript also gets its notes written right away, so at the end only the last part and combining them are left: notes for a long lecture arrive seconds after you stop.

The server has two lines of jobs, each doing one at a time: audio/video (slow), and quick jobs (notes from text the page already has, and documents). So a quick job never waits behind someone's hour-long recording. A waiting job says what it's waiting for, e.g. "Waiting for 1 other file to finish first (Transcribing… 2 of 5 parts done)".

Notes start **automatically when you stop recording** (anything over 10 seconds). The recording stays on the page, so you can still download it or try again.

Jobs live in the server's memory, so a restart (e.g. an update going live mid-job) forgets them. The page notices and **sends the job again by itself** once the server is back (it waits up to ~3 minutes for it). It still has everything it needs: the transcript, the file or the link.

If a piece of the live transcript can't be read (e.g. a browser that records in a format that can't be cut into pieces), the recording is uploaded and transcribed in full at the end, as before.

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
│   ├── placement.py   # whose lecture, which class, where in Notion
│   └── utils.py
├── frontend/          # index.html, styles.css, app.js, live.js (transcribe + guess while recording), ocr.js (read scans on the device), shrink.js (videos → audio only before upload), chat.js (no build step)
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

- **Kept awake for free.** Render stops free services after 15 minutes with no visitors, and the next visit then takes 30–60 seconds. The included GitHub Action (`.github/workflows/keep-awake.yml`) pings the site every 10 minutes so it stays awake. If you deploy under a different URL, change the URL in that file. GitHub pauses scheduled actions in repos with no activity for 60 days; re-enable it under the repo's **Actions** tab if that happens.
- **Long recordings are fine, and crash-safe.** The browser records locally and backs the audio up on the device every second (about 15 MB per hour). If the tab closes or the laptop dies mid-lecture, the next visit offers **Recover it**. The backup is deleted once the notes are made, when you discard it, or after 2 days.
- **Screen stays on while recording** in browsers that support it (Chrome, Edge, Safari 16.4+), since phones stop recording when the screen locks.
- **Phones and interruptions.** Websites can't record in the background on iPhone: locking the phone or switching apps pauses the recording. The site notices and shows how much is missing (e.g. "Recording was paused for 02:10"), and restarts the audio when you come back. iPhones also see a tip: for long lectures, record with **Voice Memos** (it keeps recording with the phone locked) and upload the file afterwards. On Android, recording usually continues in the background.
- **Recent notes** are kept on each device (the last 30). Reopen one to read it again or send it to Notion later.
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

While the lecture is recorded (or, for an upload, while the notes are written), the AI guesses **whose lecture it is and which class**: it compares what the lecture was about with everyone's classes (and, for people without a class list, the titles of their lecture pages). Both answers are pre-filled with a "🤖 Guessed from the lecture" note, and you can change either one. If two people take the same class, the name this device usually sends for breaks the tie.

The **Send to Notion** box asks two things:

1. **Who is it for?** A name from the top level of the shared Notion (one page per person). Each device remembers the last choice.
2. **Which class?** The site lists the classes it finds in that person's Notion, e.g. the entries of their *Courses*, *Classes* or *Domains* table. The AI pre-selects the class that matches the lecture. If no class list is found, pick **Something else** and type the class name (e.g. `csc108`).

The AI then suggests where the notes should go, preferring where that person's lectures for the class already live:

- a **lectures/topics table linked to the class**: a new entry, with the class link, a `lecture` type and today's date filled in when the table has those columns;
- the page where their **other lecture pages** for the class are;
- **inside the class page**, or adding to their **latest lecture**.

The Notion page gets the lecture's title (the one at the top of the notes, which you can edit). Check the suggestion, then click **Send to Notion**, or open **Put it somewhere else** to choose another of the suggested places. Nothing is written until you click Send.

### Study extras (optional)

Every lecture gets a summary, key points, **key terms** and action items. Above the Record/Upload tabs, **✨ Study extras** opens a list of optional extras, none ticked by default. Each device remembers its choice:

- **Explained simply**: the hardest ideas in plain language
- **Practice questions**: exam-style questions, answers hidden until clicked
- **Flashcards**: click to flip; **Copy for Quizlet / Anki** copies them in the format both apps import
- **Multiple-choice quiz**: 4 options per question, the answer and explanation hidden
- **Cheat sheet**: the must-know formulas, rules and facts

They're made in one extra AI step after the notes, using only the ones you tick. All of them go to Notion too: questions and quiz answers as toggles, flashcards as a Front/Back table. If the AI is too busy to make them, the notes still arrive and a message says the extras were skipped.

### Study helper chat

The **💬 Ask AI** button in the bottom-right corner opens a chat you can use any time:

- **During a lecture**, while recording: ask things like "explain the last few minutes simply". Each question transcribes only the audio recorded since the previous question, so the helper knows what was just said, and recording carries on.
- **After a lecture**: it uses the notes and the full transcript.
- **With no lecture open**: it's a general study tutor.

It answers from the lecture first and says when it adds something the lecture didn't cover. Opening a different lecture starts a fresh conversation.

### Lecture slides (optional)

Under the recording, and on the Upload tab, there's **📎 Add lecture slides**. It's optional. If you add the prof's slides (PDF or PowerPoint `.pptx`, up to 50 MB), the AI uses them to get names, terms and formulas right and to fix words the transcript misheard. Skip it and everything works the same. Slides that are only images (no selectable text) can't be read; the notes are then made from the recording alone.

### Upload / link

- **Files:** any audio or video format ffmpeg reads (mp3, m4a, wav, mp4, mov, webm, mkv, …), up to `MAX_UPLOAD_MB` (default 300 MB). For **videos** (`.mp4`, `.mov`, `.m4v`, over 15 MB), the browser first takes out just the sound and uploads that. The audio is copied, not re-encoded, so it loses nothing. That's usually 5-10x smaller (a 188 MB, 20-minute video became 25 MB in 4 seconds), so uploads are much faster on phone data or slow Wi-Fi. Other video types, or anything that goes wrong, upload unchanged.
- **Documents:** drop a PDF, Word (`.docx`), PowerPoint (`.pptx`) or text file into the same box and the app summarises it like a lecture, with the same notes, study extras and Send to Notion. It skips transcription, and the document's text goes in a collapsed **Full text** section. Scanned PDFs and photos of notes or handouts (`.jpg`, `.png`) work too: pages without selectable text are read with OCR (**Tesseract**, free and open source, so no key is needed). The OCR runs **on your own device** in the browser (Tesseract.js plus pdf.js, about 4 MB the first time, then cached), because a laptop or phone is many times faster than Render's free server. A 9-page scan takes about 10 seconds on a laptop. Only the text is sent to the server. If that doesn't work in your browser, the file is uploaded and the server reads it instead, which is slower: the first 40 scanned pages, with a warning if there are more. Documents over ~100 pages are cut off with a warning. Scanned slides work the same way, up to 20 pages.
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

## System check

Open **`/check`** on your site (there's a link in the footer, and it uses the site password). It shows ✅/❌ for every part, with a reason for anything that's wrong:
- the Groq key and the AI models (it makes one tiny real request to each);
- Whisper;
- the Notion connection, with the people it can see;
- ffmpeg, the OCR tools, the PDF/PowerPoint readers and yt-dlp;
- whether a password is set.

Run it before a lecture if anything seems off.

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
