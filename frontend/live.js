"use strict";

// While a lecture is being recorded:
// 1. Every few minutes, the new audio is transcribed, so when recording stops only the last
//    few minutes are left. The notes are then written from this transcript; the full
//    recording doesn't need uploading or transcribing again.
// 2. Each time ~15 minutes of transcript build up, the notes for that part are written straight
//    away, so at the end only the last part and combining them are left.
// 3. Once there's enough to go on, the AI guesses whose lecture it is, which class, and where it
//    goes in Notion, and checks again as the lecture goes on until it's sure. By the time the notes
//    are ready, Send to Notion is too.
// If anything goes wrong with this, the recording is simply uploaded and transcribed at the end
// as before. The study helper chat (chat.js) uses the same transcript.
// Relies on globals from app.js: state, api, postJson, recall, WHO_KEY, chosenExtras, $.

const LIVE_EVERY_MS = 3 * 60 * 1000;
const GUESS_AFTER_WORDS = 250; // about two minutes of talking
const MAX_GUESSES = 6;

const live = newLive(null);

function newLive(rec) {
  return {
    rec,               // the recording being transcribed ({ startedAt, chunks, mimeType })
    recId: rec ? rec.startedAt : null,
    transcript: "",
    synced: 0,         // how many recorded pieces are transcribed
    total: null,       // how many pieces the recording ended with
    failed: false,     // a piece couldn't be read: transcribe the whole recording at the end instead
    running: null,     // the transcription in progress (one at a time, in order)
    final: null,       // the last catch-up, started when recording stops
    timer: null,
    guesses: 0,
    guessing: false,
    lastGuess: "",
    sure: false,       // two guesses in a row agreed, so stop asking
    placement: null,   // { person_id, class_id, class_name, plan, plan_for }
    parts: [],         // notes written so far, one per part of the transcript...
    partsEnd: 0,       // ...covering transcript.slice(0, partsEnd)
    noting: null,      // the part notes in progress
  };
}

function liveStart(rec) {
  clearInterval(live.timer);
  Object.assign(live, newLive(rec));
  $("#live-guess").hidden = true;
  live.timer = setInterval(() => liveSync(rec).then(() => Promise.all([liveNotes(rec), liveGuess(rec)])).catch(() => {}),
                           LIVE_EVERY_MS);
}

// Recording stopped: transcribe what's left straight away, so it's ready when they click.
function liveFinish(rec) {
  clearInterval(live.timer);
  if (live.recId !== rec.startedAt) return;
  live.total = rec.chunks.length;
  live.final = liveSync(rec);
  live.final.catch(() => {});
}

function liveSync(rec) {
  const run = (live.running || Promise.resolve()).catch(() => {}).then(() => syncOnce(rec));
  live.running = run;
  return run;
}

async function syncOnce(rec) {
  if (live.recId !== rec.startedAt || live.failed) return;
  const chunks = rec.chunks;
  const upTo = chunks.length;
  if (upTo <= live.synced) return;
  // The first piece carries the audio file's header, so later pieces need it in front to be readable.
  const fresh = chunks.slice(live.synced, upTo);
  const parts = live.synced === 0 ? fresh : [chunks[0], ...fresh];
  const type = rec.mimeType || "audio/webm";
  const form = new FormData();
  form.append("audio_file", new Blob(parts, { type }), type.includes("mp4") ? "snippet.m4a" : "snippet.webm");
  // The header piece is the recording's first second; drop it again so it isn't heard twice.
  if (live.synced > 0) form.append("skip_seconds", "1");
  // A network error leaves `synced` as is, so the next try picks these pieces up again.
  const result = await api("/api/assistant/transcribe", { method: "POST", body: form });
  if (live.recId !== rec.startedAt) return; // a new recording started meanwhile
  if (result.ok === false) {
    live.failed = true;
    throw new Error(result.error || "Couldn't transcribe part of the recording.");
  }
  live.synced = upTo;
  if (result.text) live.transcript += (live.transcript ? " " : "") + result.text;
}

// Write the notes for each full part of the transcript as soon as it's there (same part size as the
// server uses), so at the end only the rest is left. A part that fails is tried again next time.
function liveNotes(rec) {
  if (live.noting) return live.noting;
  live.noting = (async () => {
    const size = state.config?.summary_chunk_chars || 12000;
    while (live.recId === rec.startedAt && live.transcript.length - live.partsEnd >= size) {
      const end = partEnd(live.transcript, live.partsEnd, size);
      const { notes } = await postJson("/api/live/part-notes",
                                       { text: live.transcript.slice(live.partsEnd, end), index: live.parts.length + 1 });
      if (live.recId !== rec.startedAt) return;
      live.parts.push(notes);
      live.partsEnd = end;
    }
  })().finally(() => { live.noting = null; });
  return live.noting;
}

// Where a part should end: at a sentence end (or at least a space) near the size limit.
function partEnd(text, start, size) {
  const limit = start + size;
  const window = text.slice(start + Math.floor(size * 0.7), limit);
  const sentence = Math.max(window.lastIndexOf(". "), window.lastIndexOf("? "), window.lastIndexOf("! "));
  const at = sentence >= 0 ? sentence + 2 : window.lastIndexOf(" ") + 1;
  return at > 0 ? start + Math.floor(size * 0.7) + at : limit;
}

// The notes written during the recording, to send with the transcript.
async function livePartsFor(startedAt) {
  if (live.recId !== startedAt) return { parts: [], partsEnd: 0 };
  try { await live.noting; } catch { /* use the parts that are done */ }
  return { parts: live.parts, partsEnd: live.partsEnd };
}

// The start and the latest part of what was said, like placement.excerpt on the server.
function liveExcerpt(text, limit = 1500) {
  if (text.length <= limit) return text;
  const head = Math.floor(limit / 3);
  return `${text.slice(0, head)} … ${text.slice(-(limit - head))}`;
}

async function liveGuess(rec) {
  if (!state.config?.notion_configured || live.sure || live.guessing || live.guesses >= MAX_GUESSES) return;
  if (live.recId !== rec.startedAt) return;
  if (live.transcript.split(/\s+/).filter(Boolean).length < GUESS_AFTER_WORDS) return;
  live.guessing = true;
  live.guesses++;
  try {
    const about = liveExcerpt(live.transcript);
    const guess = await postJson("/api/notion/guess", { note_title: "", summary: about, usual_person_id: recall(WHO_KEY) });
    if (live.recId !== rec.startedAt || !guess.person_id) return;
    const key = `${guess.person_id}|${guess.class_id || guess.class_name || ""}`;
    const agrees = key === live.lastGuess;
    live.lastGuess = key;
    live.placement = { ...guess, plan: null };
    showLiveGuess(guess, false);
    if (!agrees || !(guess.class_id || guess.class_name)) return;
    // Two guesses in a row agree: work out where in Notion it goes, then stop asking.
    const planFor = { person_id: guess.person_id, class_id: guess.class_id || null,
                      class_text: guess.class_id ? "" : guess.class_name };
    const plan = await postJson("/api/notion/plan", { ...planFor, note_title: "", summary: about });
    if (live.recId !== rec.startedAt || live.lastGuess !== key) return;
    Object.assign(live.placement, { plan, plan_for: planFor });
    live.sure = true;
    showLiveGuess(guess, true);
  } catch { /* only a head start; it's worked out after the notes otherwise */ } finally {
    live.guessing = false;
  }
}

function showLiveGuess(guess, sure) {
  const where = [guess.person_name, guess.class_title].filter(Boolean).join(" · ");
  if (!where) return;
  $("#live-guess").textContent = sure
    ? `🤖 Notion: ${where} ✓ (ready to send when the notes are done; you can still change it)`
    : `🤖 Notion: looks like ${where} (getting surer as the lecture goes on)`;
  $("#live-guess").hidden = false;
}

// When the notes are made: the transcript is ready if every piece of this recording was transcribed.
async function liveTranscriptFor(startedAt) {
  if (live.recId !== startedAt || !live.final) return null;
  try { await live.final; } catch { /* try once more below */ }
  if (!live.failed && live.synced < live.total) {
    try { await liveSync(live.rec); } catch { return null; }
  }
  if (live.failed || live.synced < live.total || !live.transcript.trim()) return null;
  return live.transcript;
}

// Where in Notion it goes, if it was worked out for sure during the recording.
function livePlacementFor(startedAt) {
  return live.recId === startedAt && live.sure ? live.placement : null;
}
