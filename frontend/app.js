"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => [...document.querySelectorAll(sel)];
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const PW_KEY = "lecture-notes-password";

const state = {
  config: null,
  source: "mic",
  rec: null,        // { recorder, streams, ctx, chunks, startedAt, timer, raf, mimeType }
  blob: null,
  file: null,
  busy: false,
  result: null,     // { transcript, notes }
};

// ---------- helpers ----------

function getPassword() {
  try { return localStorage.getItem(PW_KEY) || ""; } catch { return ""; }
}
function setPassword(value) {
  try { localStorage.setItem(PW_KEY, value); } catch { /* storage blocked — fine */ }
}

function authHeaders() {
  const pw = getPassword();
  return pw ? { "X-App-Password": pw } : {};
}

function errorText(data, status) {
  const d = data && data.detail;
  if (typeof d === "string") return d;
  if (Array.isArray(d) && d[0]?.msg) return d[0].msg;
  return `Request failed (${status})`;
}

async function api(path, opts = {}) {
  const res = await fetch(path, { ...opts, headers: { ...authHeaders(), ...(opts.headers || {}) } });
  let data = null;
  try { data = await res.json(); } catch { /* non-JSON */ }
  if (res.status === 401) {
    lock("Please enter the password.");
    throw new Error("Password required.");
  }
  if (!res.ok) throw new Error(errorText(data, res.status));
  return data;
}

function showError(msg) {
  $("#error-text").textContent = msg;
  $("#error").hidden = false;
  $("#error").scrollIntoView({ behavior: "smooth", block: "nearest" });
}
function clearError() { $("#error").hidden = true; }

function fmtTime(sec) {
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = Math.floor(sec % 60);
  const mm = String(m).padStart(2, "0"), ss = String(s).padStart(2, "0");
  return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

function setBusy(busy) {
  state.busy = busy;
  for (const id of ["#rec-process", "#file-process", "#rec-btn"]) $(id).disabled = busy || (id === "#file-process" && !state.file);
  $("#url-form button").disabled = busy;
  $$(".source").forEach((b) => { if (!b.dataset.unsupported) b.disabled = busy || !!state.rec; });
}

// ---------- password lock ----------

function lock(msg) {
  document.body.classList.add("locked");
  $("#unlock").hidden = false;
  $("#unlock-msg").textContent = msg || "";
  $("#unlock-input").focus();
}

$("#unlock-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  setPassword($("#unlock-input").value);
  try {
    const res = await fetch("/api/auth/check", { headers: authHeaders() });
    if (!res.ok) throw new Error();
    document.body.classList.remove("locked");
    $("#unlock").hidden = true;
    $("#unlock-input").value = "";
  } catch {
    $("#unlock-msg").textContent = "That password didn't work.";
  }
});

// ---------- tabs ----------

$$(".tab").forEach((tab) => tab.addEventListener("click", () => {
  $$(".tab").forEach((t) => t.setAttribute("aria-selected", String(t === tab)));
  $("#tab-record").hidden = tab.dataset.tab !== "record";
  $("#tab-upload").hidden = tab.dataset.tab !== "upload";
}));

// ---------- recording ----------

const HINTS = {
  mic: "Your browser will ask for microphone access.",
  system: "In the share dialog, pick a tab, window or screen and turn on “Share audio”. Sharing a browser tab is the most reliable option, especially on Mac.",
  both: "You'll be asked to share a tab/screen (turn on “Share audio”) and then for microphone access.",
};

function selectSource(source) {
  state.source = source;
  $$(".source").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.source === source)));
  $("#source-hint").textContent = HINTS[source];
}

$$(".source").forEach((b) => b.addEventListener("click", () => selectSource(b.dataset.source)));

function stopStreams(streams) {
  streams.forEach((s) => s && s.getTracks().forEach((t) => t.stop()));
}

function mediaErrorMessage(err) {
  if (err && err.name === "NotAllowedError") return "Permission was denied or the share dialog was cancelled.";
  if (err && err.name === "NotFoundError") return "No microphone was found.";
  if (err && err.name === "NotSupportedError") return "Your browser can't capture this audio source. Try Chrome or Edge on a computer.";
  return (err && err.message) || "Couldn't start recording.";
}

function pickMimeType() {
  const types = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg;codecs=opus"];
  return types.find((t) => window.MediaRecorder && MediaRecorder.isTypeSupported(t)) || "";
}

async function startRecording() {
  clearError();
  const source = state.source;
  // Create the AudioContext inside the click so browsers let it start.
  const ctx = new (window.AudioContext || window.webkitAudioContext)();
  let sysStream = null, micStream = null;

  try {
    if (source !== "mic") {
      // Chrome only offers audio sharing when video is requested too; we ignore the video.
      sysStream = await navigator.mediaDevices.getDisplayMedia({
        video: true,
        audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false },
        systemAudio: "include",
        selfBrowserSurface: "exclude",
      });
      if (!sysStream.getAudioTracks().length) {
        throw new Error("No audio was shared. Start again and turn on “Share audio” in the share dialog (Chrome/Edge only).");
      }
    }
    if (source !== "system") {
      micStream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true },
      });
    }
  } catch (err) {
    stopStreams([sysStream, micStream]);
    ctx.close();
    showError(mediaErrorMessage(err));
    return;
  }

  // Mix every input into one stream (and feed the level meter).
  await ctx.resume();
  const dest = ctx.createMediaStreamDestination();
  const analyser = ctx.createAnalyser();
  analyser.fftSize = 512;
  for (const s of [sysStream, micStream]) {
    if (!s) continue;
    const node = ctx.createMediaStreamSource(new MediaStream(s.getAudioTracks()));
    node.connect(dest);
    node.connect(analyser);
  }

  const mimeType = pickMimeType();
  const recorder = new MediaRecorder(dest.stream, mimeType ? { mimeType, audioBitsPerSecond: 64000 } : undefined);
  const rec = { recorder, streams: [sysStream, micStream], ctx, chunks: [], startedAt: Date.now(), mimeType: recorder.mimeType || mimeType };
  state.rec = rec;

  recorder.ondataavailable = (e) => { if (e.data && e.data.size) rec.chunks.push(e.data); };
  recorder.onstop = () => finishRecording(rec);
  // If the user clicks the browser's own "Stop sharing" button, stop recording too.
  [sysStream, micStream].forEach((s) => s && s.getTracks().forEach((t) => t.addEventListener("ended", stopRecording)));
  recorder.start(1000);

  // UI: timer + level meter
  $("#recording-ready").hidden = true;
  $("#rec-btn").textContent = "■ Stop recording";
  $("#rec-btn").classList.add("recording");
  $$(".source").forEach((b) => (b.disabled = true));
  rec.timer = setInterval(() => { $("#rec-timer").textContent = fmtTime((Date.now() - rec.startedAt) / 1000); }, 500);
  const buf = new Uint8Array(analyser.fftSize);
  const draw = () => {
    analyser.getByteTimeDomainData(buf);
    let sum = 0;
    for (const v of buf) sum += ((v - 128) / 128) ** 2;
    const level = Math.min(1, Math.sqrt(sum / buf.length) * 4);
    $("#meter-fill").style.width = `${Math.round(level * 100)}%`;
    rec.raf = requestAnimationFrame(draw);
  };
  draw();
}

function stopRecording() {
  const rec = state.rec;
  if (rec && rec.recorder.state !== "inactive") rec.recorder.stop();
}

function finishRecording(rec) {
  clearInterval(rec.timer);
  cancelAnimationFrame(rec.raf);
  stopStreams(rec.streams);
  rec.ctx.close();
  state.rec = null;

  $("#rec-btn").textContent = "● Start recording";
  $("#rec-btn").classList.remove("recording");
  $("#meter-fill").style.width = "0";
  setBusy(state.busy);

  const type = rec.mimeType || "audio/webm";
  state.blob = new Blob(rec.chunks, { type });
  if (!state.blob.size) {
    showError("Nothing was recorded. Check your audio source and try again.");
    return;
  }
  const ext = type.includes("mp4") ? "m4a" : type.includes("ogg") ? "ogg" : "webm";
  const url = URL.createObjectURL(state.blob);
  const stamp = new Date().toISOString().slice(0, 16).replace("T", "_").replace(":", "-");
  $("#rec-preview").src = url;
  $("#rec-download").href = url;
  $("#rec-download").download = `recording_${stamp}.${ext}`;
  $("#recording-ready").hidden = false;
}

$("#rec-btn").addEventListener("click", () => (state.rec ? stopRecording() : startRecording()));

$("#rec-discard").addEventListener("click", () => {
  if (!confirm("Discard this recording?")) return;
  state.blob = null;
  $("#recording-ready").hidden = true;
  $("#rec-timer").textContent = "00:00";
});

$("#rec-process").addEventListener("click", () => {
  if (!state.blob) return;
  const ext = $("#rec-download").download.split(".").pop();
  processUpload(state.blob, `recording.${ext}`);
});

window.addEventListener("beforeunload", (e) => {
  if (state.rec || state.busy) { e.preventDefault(); e.returnValue = ""; }
});

// ---------- upload / link ----------

function setFile(file) {
  if (!file) return;
  const limit = state.config?.max_upload_mb || 300;
  if (file.size > limit * 1024 * 1024) {
    showError(`That file is larger than ${limit} MB.`);
    return;
  }
  state.file = file;
  $("#file-name").textContent = `${file.name} (${(file.size / 1024 / 1024).toFixed(1)} MB)`;
  setBusy(state.busy);
}

$("#file-input").addEventListener("change", (e) => setFile(e.target.files[0]));
const dz = $("#dropzone");
["dragenter", "dragover"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("drag"); }));
["dragleave", "drop"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("drag"); }));
dz.addEventListener("drop", (e) => setFile(e.dataTransfer.files[0]));

$("#file-process").addEventListener("click", () => state.file && processUpload(state.file, state.file.name));

$("#url-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  clearError();
  startWorking("Sending link…");
  try {
    const job = await api("/api/jobs/url", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: $("#url-input").value.trim() }),
    });
    await pollJob(job.id);
  } catch (err) {
    stopWorking();
    showError(err.message);
  }
});

// ---------- processing ----------

function startWorking(text) {
  setBusy(true);
  $("#results").hidden = true;
  $("#notion-card").hidden = true;
  $("#status-card").hidden = false;
  $("#status-text").textContent = text;
  $("#status-card").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function stopWorking() {
  setBusy(false);
  $("#status-card").hidden = true;
  $("#upload-progress").hidden = true;
}

function uploadWithProgress(blob, filename) {
  return new Promise((resolve, reject) => {
    const form = new FormData();
    form.append("file", blob, filename);
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/jobs/upload");
    Object.entries(authHeaders()).forEach(([k, v]) => xhr.setRequestHeader(k, v));
    xhr.upload.onprogress = (e) => {
      if (!e.lengthComputable) return;
      const pct = Math.round((e.loaded / e.total) * 100);
      $("#upload-fill").style.width = `${pct}%`;
      $("#status-text").textContent = `Uploading… ${pct}%`;
    };
    xhr.onload = () => {
      let data = null;
      try { data = JSON.parse(xhr.responseText); } catch { /* ignore */ }
      if (xhr.status === 401) { lock("Please enter the password."); reject(new Error("Password required.")); }
      else if (xhr.status >= 400) reject(new Error(errorText(data, xhr.status)));
      else resolve(data);
    };
    xhr.onerror = () => reject(new Error("Upload failed. Check your connection and try again."));
    xhr.send(form);
  });
}

async function processUpload(blob, filename) {
  clearError();
  startWorking("Uploading…");
  $("#upload-progress").hidden = false;
  $("#upload-fill").style.width = "0";
  try {
    const job = await uploadWithProgress(blob, filename);
    $("#upload-progress").hidden = true;
    await pollJob(job.id);
  } catch (err) {
    stopWorking();
    showError(err.message);
  }
}

async function pollJob(id) {
  let failures = 0;
  while (true) {
    let job;
    try {
      job = await api(`/api/jobs/${id}`);
      failures = 0;
    } catch (err) {
      // Tolerate brief network blips, but not "job not found".
      if (/not found/i.test(err.message) || ++failures > 5) throw err;
      await sleep(3000);
      continue;
    }
    $("#status-text").textContent = job.message;
    if (job.status === "done" || job.status === "error") {
      stopWorking();
      if (job.transcript) showResults(job);
      if (job.status === "error") {
        showError(job.transcript ? `Transcript is ready, but notes failed: ${job.error}` : job.error);
      }
      return;
    }
    await sleep(2000);
  }
}

// ---------- results ----------

function fillList(el, items, empty) {
  el.replaceChildren();
  if (!items.length) {
    const li = document.createElement("li");
    li.className = "muted";
    li.textContent = empty;
    el.append(li);
    return;
  }
  for (const item of items) {
    const li = document.createElement("li");
    li.textContent = item;
    el.append(li);
  }
}

function showResults(job) {
  const notes = job.notes || { title: job.label || "Notes", summary: "", key_points: [], action_items: [] };
  state.result = { transcript: job.transcript, notes };
  $("#note-title").value = notes.title || "Notes";
  $("#note-summary").textContent = notes.summary || "—";
  fillList($("#note-points"), notes.key_points || [], "—");
  fillList($("#note-actions"), notes.action_items || [], "None mentioned.");
  $("#notes-body").hidden = !job.notes;
  $("#note-transcript").textContent = job.transcript;
  $("#word-count").textContent = `(${job.transcript.split(/\s+/).filter(Boolean).length.toLocaleString()} words)`;
  $("#results").hidden = false;
  $("#notion-card").hidden = false;
  $("#notion-result").textContent = "";
  if (state.config?.notion_configured && notionMode() === "existing" && !$("#page-select").options.length) loadPages();
  $("#results").scrollIntoView({ behavior: "smooth", block: "start" });
}

function toMarkdown() {
  const { notes, transcript } = state.result;
  const lines = [`# ${$("#note-title").value}`, "", "## Summary", notes.summary || "—", "", "## Key points"];
  (notes.key_points.length ? notes.key_points : ["—"]).forEach((p) => lines.push(`- ${p}`));
  lines.push("", "## Action items");
  (notes.action_items.length ? notes.action_items.map((a) => `- [ ] ${a}`) : ["None mentioned."]).forEach((a) => lines.push(a));
  lines.push("", "## Full transcript", transcript);
  return lines.join("\n");
}

$("#copy-md").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(toMarkdown());
    $("#copy-md").textContent = "Copied ✓";
    setTimeout(() => ($("#copy-md").textContent = "Copy as Markdown"), 1500);
  } catch {
    showError("Couldn't access the clipboard.");
  }
});

$("#start-over").addEventListener("click", () => {
  $("#results").hidden = true;
  $("#notion-card").hidden = true;
  state.result = null;
  window.scrollTo({ top: 0, behavior: "smooth" });
});

// ---------- notion ----------

function notionMode() {
  return $('input[name="notion-mode"]:checked').value;
}

let searchTimer = null;
async function loadPages(query = "") {
  const select = $("#page-select");
  select.replaceChildren(new Option("Loading…", ""));
  try {
    const { pages } = await api(`/api/notion/pages?q=${encodeURIComponent(query)}`);
    select.replaceChildren();
    if (!pages.length) select.append(new Option("No pages found. Share pages with your integration in Notion.", ""));
    pages.forEach((p) => select.append(new Option(`${p.icon ? p.icon + " " : ""}${p.title}`, p.id)));
  } catch (err) {
    select.replaceChildren(new Option("Couldn't load pages", ""));
    showError(err.message);
  }
}

$$('input[name="notion-mode"]').forEach((r) => r.addEventListener("change", () => {
  const existing = notionMode() === "existing";
  $("#existing-picker").hidden = !existing;
  if (existing && !$("#page-select").options.length) loadPages();
}));

$("#page-search").addEventListener("input", (e) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => loadPages(e.target.value), 350);
});

$("#notion-send").addEventListener("click", async () => {
  if (!state.result) return;
  clearError();
  const mode = notionMode();
  const pageId = $("#page-select").value;
  if (mode === "existing" && !pageId) { showError("Pick a Notion page first."); return; }

  const btn = $("#notion-send");
  btn.disabled = true;
  btn.textContent = "Sending…";
  $("#notion-result").textContent = "";
  try {
    const { notes, transcript } = state.result;
    const { url } = await api("/api/notion/export", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        mode,
        page_id: pageId || null,
        title: $("#note-title").value.trim() || "Untitled notes",
        notes: { summary: notes.summary, key_points: notes.key_points, action_items: notes.action_items },
        transcript,
      }),
    });
    const a = document.createElement("a");
    a.href = url;
    a.target = "_blank";
    a.rel = "noopener";
    a.textContent = "Open in Notion ↗";
    $("#notion-result").replaceChildren("✅ Saved to Notion. ", a);
  } catch (err) {
    showError(err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "Send to Notion";
  }
});

// ---------- startup ----------

$("#error-close").addEventListener("click", clearError);

async function init() {
  selectSource("mic");
  if (!navigator.mediaDevices?.getDisplayMedia) {
    $$('.source[data-source="system"], .source[data-source="both"]').forEach((b) => {
      b.disabled = true;
      b.dataset.unsupported = "1";
      b.title = "Your browser can't capture system audio. Use Chrome or Edge on a computer.";
    });
  }
  if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
    $("#rec-btn").disabled = true;
    $("#source-hint").textContent = "Recording isn't supported in this browser. You can still upload a file.";
  }

  try {
    state.config = await (await fetch("/api/config")).json();
  } catch {
    showError("Couldn't reach the server. If it was asleep, wait ~1 minute and refresh.");
    return;
  }
  const cfg = state.config;
  $("#file-limit").textContent = `Audio or video, up to ${cfg.max_upload_mb} MB`;
  $("#notion-off").hidden = cfg.notion_configured;
  $("#notion-on").hidden = !cfg.notion_configured;
  if (cfg.notion_configured && !cfg.notion_can_create) {
    const newRadio = $('input[name="notion-mode"][value="new"]');
    newRadio.disabled = true;
    newRadio.parentElement.title = "Set NOTION_PARENT_PAGE_ID to enable creating new pages.";
    $('input[name="notion-mode"][value="existing"]').checked = true;
    $("#existing-picker").hidden = false;
  }
  if (cfg.password_required && !getPassword()) lock();
}

init();
