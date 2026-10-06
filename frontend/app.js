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
  if (state.pendingBackup && !confirm("You have an unfinished recording saved on this device. Starting a new one will delete it. Continue?")) return;
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
  // 32 kbps Opus is plenty for speech (~15 MB per hour).
  const recorder = new MediaRecorder(dest.stream, mimeType ? { mimeType, audioBitsPerSecond: 32000 } : undefined);
  const rec = { recorder, streams: [sysStream, micStream], ctx, chunks: [], startedAt: Date.now(), mimeType: recorder.mimeType || mimeType };
  state.rec = rec;
  liveStart(rec); // transcribe as it goes, and guess where in Notion it belongs (live.js)

  // Back up every second of audio on this device, so a crash or closed tab doesn't lose it.
  hideRecoverCard();
  showMissed(0);
  rec.backupOk = true;
  rec.backup = store.backupStart({ startedAt: rec.startedAt, mimeType: rec.mimeType }).catch(() => backupFailed(rec));
  rec.chunkTimes = [];
  rec.missed = 0;          // seconds of the lecture lost to interruptions
  rec.pausedSince = null;  // when capturing stopped, if it has
  watchForInterruptions(rec, [micStream, sysStream]);
  recorder.ondataavailable = (e) => {
    if (!e.data || !e.data.size) return;
    rec.chunks.push(e.data);
    rec.chunkTimes.push(Date.now());
    if (!rec.backupOk) return;
    const seconds = (Date.now() - rec.startedAt) / 1000;
    rec.backup = rec.backup.then(() => rec.backupOk && store.backupChunk(e.data, seconds)).catch(() => backupFailed(rec));
  };
  keepScreenOn();
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

function backupFailed(rec) {
  if (!rec.backupOk) return;
  rec.backupOk = false;
  $("#rec-note").textContent = "⚠️ This browser won't let the site save a backup, so keep this tab open until you stop.";
}

// ----- keep the screen on while recording (phones stop recording when the screen locks) -----

async function keepScreenOn() {
  const note = $("#rec-note");
  if (!("wakeLock" in navigator)) {
    note.textContent = "Keep your screen on while recording. This browser can't keep it awake automatically.";
    return;
  }
  try {
    state.wakeLock = await navigator.wakeLock.request("screen");
    note.textContent = "Your screen will stay on while recording, and the audio is backed up on this device.";
  } catch {
    note.textContent = "Keep your screen on while recording. Phones can stop recording when the screen locks.";
  }
}

function releaseScreen() {
  if (state.wakeLock) state.wakeLock.release().catch(() => {});
  state.wakeLock = null;
}

// ----- noticing when a recording gets interrupted (phones pause it in the background) -----

function watchForInterruptions(rec, streams) {
  // iPhones suspend audio processing when the page is hidden or a call comes in.
  rec.ctx.addEventListener("statechange", () => {
    if (state.rec !== rec) return;
    if (rec.ctx.state === "running") markResumed(rec);
    else markPaused(rec);
  });
  for (const stream of streams) {
    stream?.getAudioTracks().forEach((track) => {
      track.addEventListener("mute", () => state.rec === rec && markPaused(rec));
      track.addEventListener("unmute", () => state.rec === rec && markResumed(rec));
    });
  }
}

function markPaused(rec) {
  if (rec.pausedSince === null) rec.pausedSince = Date.now();
}

function markResumed(rec) {
  if (rec.pausedSince === null) return;
  addMissed(rec, (Date.now() - rec.pausedSince) / 1000);
  rec.pausedSince = null;
}

function addMissed(rec, seconds) {
  if (seconds < 3) return; // ignore blips
  rec.missed += seconds;
  showMissed(rec.missed, true);
}

function showMissed(seconds, recording) {
  const warning = $("#rec-warning");
  warning.hidden = !seconds;
  if (!seconds) return;
  warning.textContent = recording
    ? `⚠️ Recording was paused for ${fmtTime(seconds)} while this page was in the background, so that part is missing. Keep this page open on screen while recording.`
    : `⚠️ This recording is missing about ${fmtTime(seconds)} from when the page was in the background.`;
}

document.addEventListener("visibilitychange", () => {
  const rec = state.rec;
  if (!rec) return;
  if (document.visibilityState === "hidden") {
    rec.hiddenAt = Date.now();
    return;
  }
  // Back on the page: the wake lock was dropped, and the audio may need restarting.
  keepScreenOn();
  if (rec.ctx.state !== "running") rec.ctx.resume().catch(() => {});
  if (rec.hiddenAt && rec.pausedSince === null) {
    // No pause event fired, so check whether audio actually kept arriving while we were away.
    const away = (Date.now() - rec.hiddenAt) / 1000;
    const arrived = rec.chunkTimes.filter((t) => t > rec.hiddenAt).length; // ~1 chunk per second
    if (away > 5 && arrived < away * 0.5) addMissed(rec, away - arrived);
  }
  rec.hiddenAt = null;
});

// ----- iPhone tip: website recording pauses in the background there -----

// Only on iPhones/iPads (iPadOS reports itself as a Mac, but with a touch screen) — never on computers.
const IOS = (/iPhone|iPad|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1))
  && window.matchMedia("(pointer: coarse)").matches;
const IOS_TIP_KEY = "lecture-notes-ios-tip-hidden";

if (IOS && recall(IOS_TIP_KEY) !== "1") $("#ios-tip").hidden = false;
$("#ios-tip-hide").addEventListener("click", () => {
  $("#ios-tip").hidden = true;
  remember(IOS_TIP_KEY, "1");
});

function stopRecording() {
  const rec = state.rec;
  if (rec && rec.recorder.state !== "inactive") rec.recorder.stop();
}

function finishRecording(rec) {
  markResumed(rec);
  showMissed(rec.missed, false);
  clearInterval(rec.timer);
  cancelAnimationFrame(rec.raf);
  stopStreams(rec.streams);
  rec.ctx.close();
  state.rec = null;
  releaseScreen();
  liveFinish(rec); // transcribe the last few minutes now

  $("#rec-btn").textContent = "● Start recording";
  $("#rec-btn").classList.remove("recording");
  $("#meter-fill").style.width = "0";
  setBusy(state.busy);

  const type = rec.mimeType || "audio/webm";
  const blob = new Blob(rec.chunks, { type });
  if (!blob.size) {
    showError("Nothing was recorded. Check your audio source and try again.");
    return;
  }
  $("#rec-note").textContent = rec.backupOk ? "Saved on this device until the notes are made." : "";
  showRecording(blob, type, rec.startedAt);
}

function showRecording(blob, type, startedAt) {
  state.blob = blob;
  state.blobStartedAt = startedAt;
  const ext = type.includes("mp4") ? "m4a" : type.includes("ogg") ? "ogg" : "webm";
  const url = URL.createObjectURL(blob);
  const d = new Date(startedAt), pad = (n) => String(n).padStart(2, "0");
  const stamp = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}_${pad(d.getHours())}-${pad(d.getMinutes())}`;
  $("#rec-preview").src = url;
  $("#rec-download").href = url;
  $("#rec-download").download = `recording_${stamp}.${ext}`;
  $("#recording-ready").hidden = false;
}

// ----- recovering a recording after a crash / closed tab -----

const BACKUP_MAX_AGE = 2 * 24 * 3600 * 1000; // older backups are deleted automatically

async function checkForBackup() {
  let saved = null;
  try { saved = await store.backupLoad(); } catch { return; }
  if (!saved) return;
  if (Date.now() - saved.info.startedAt > BACKUP_MAX_AGE) {
    store.backupClear().catch(() => {});
    return;
  }
  state.pendingBackup = saved;
  const when = new Date(saved.info.startedAt).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" });
  $("#recover-text").textContent = `Started ${when}, about ${fmtTime(saved.info.seconds || 0)} long. It was saved on this device in case the tab closed.`;
  $("#recover-card").hidden = false;
}

function hideRecoverCard() {
  state.pendingBackup = null;
  $("#recover-card").hidden = true;
}

$("#recover-yes").addEventListener("click", () => {
  const { info, blob } = state.pendingBackup;
  hideRecoverCard();
  $$(".tab").find((t) => t.dataset.tab === "record").click();
  $("#rec-timer").textContent = fmtTime(info.seconds || 0);
  $("#rec-note").textContent = "Recovered. Click Transcribe & write notes.";
  showRecording(blob, info.mimeType || "audio/webm", info.startedAt);
  $("#recording-ready").scrollIntoView({ behavior: "smooth", block: "center" });
});

$("#recover-no").addEventListener("click", () => {
  if (!confirm("Delete the unfinished recording? This can't be undone.")) return;
  hideRecoverCard();
  store.backupClear().catch(() => {});
});

$("#rec-btn").addEventListener("click", () => (state.rec ? stopRecording() : startRecording()));

$("#rec-discard").addEventListener("click", () => {
  if (!confirm("Discard this recording?")) return;
  state.blob = null;
  store.backupClear().catch(() => {});
  $("#rec-note").textContent = "";
  $("#recording-ready").hidden = true;
  $("#rec-timer").textContent = "00:00";
});

$("#rec-process").addEventListener("click", async () => {
  if (!state.blob || state.busy) return;
  if (await processLiveTranscript()) return;
  const ext = $("#rec-download").download.split(".").pop();
  processUpload(state.blob, `recording.${ext}`, true);
});

// Most of the recording was transcribed while it was being made (live.js), so only the notes are
// left to write. Returns false if that didn't work out; the recording is then uploaded as usual.
async function processLiveTranscript() {
  const startedAt = state.blobStartedAt;
  if (live.recId !== startedAt) return false;
  clearError();
  startWorking("Finishing the transcript…");
  const transcript = await liveTranscriptFor(startedAt);
  if (!transcript) return false;
  const placement = livePlacementFor(startedAt);
  try {
    const form = new FormData();
    form.append("transcript", transcript);
    form.append("label", "Recording");
    form.append("extras", chosenExtras().join(","));
    form.append("usual_person_id", recall(WHO_KEY));
    form.append("place", placement ? "false" : "true"); // already worked out during the lecture
    if (state.slides) form.append("slides_file", state.slides);
    const job = await api("/api/jobs/text", { method: "POST", body: form });
    state.fromRecording = true;
    state.earlyPlacement = placement;
    await pollJob(job.id);
  } catch (err) {
    stopWorking();
    showError(err.message);
  }
  return true;
}

window.addEventListener("beforeunload", (e) => {
  if (state.rec || state.busy) { e.preventDefault(); e.returnValue = ""; }
});

// ---------- optional study extras (chosen before recording) ----------

const EXTRAS_KEY = "lecture-notes-extras-v2"; // v2: nothing ticked by default
const extrasBoxes = () => $$('#extras input[type="checkbox"]');

function chosenExtras() {
  return extrasBoxes().filter((b) => b.checked).map((b) => b.value);
}

function updateExtrasSummary() {
  const names = extrasBoxes().filter((b) => b.checked).map((b) => b.parentElement.querySelector("strong").textContent);
  $("#extras-summary").textContent = names.length ? `· ${names.join(", ")}` : "· none";
}

(function restoreExtras() {
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(EXTRAS_KEY)); } catch { /* none saved */ }
  if (Array.isArray(saved)) extrasBoxes().forEach((b) => (b.checked = saved.includes(b.value)));
  updateExtrasSummary();
})();

extrasBoxes().forEach((b) => b.addEventListener("change", () => {
  try { localStorage.setItem(EXTRAS_KEY, JSON.stringify(chosenExtras())); } catch { /* storage blocked */ }
  updateExtrasSummary();
}));

// ---------- optional lecture slides ----------

const SLIDES_MAX_MB = 50;

function setSlides(file) {
  if (file) {
    if (!/\.(pdf|pptx)$/i.test(file.name)) {
      showError("Slides must be a PDF or PowerPoint (.pptx) file.");
      file = null;
    } else if (file.size > SLIDES_MAX_MB * 1024 * 1024) {
      showError(`The slides file is larger than ${SLIDES_MAX_MB} MB.`);
      file = null;
    }
  }
  state.slides = file || null;
  $$(".slides-pick").forEach((pick) => {
    pick.querySelector(".slides-label").hidden = !!state.slides;
    pick.querySelector(".slides-chosen").hidden = !state.slides;
    pick.querySelector(".slides-name").textContent = state.slides ? state.slides.name : "";
    pick.querySelector(".slides-input").value = "";
  });
}

$$(".slides-input").forEach((input) => input.addEventListener("change", (e) => setSlides(e.target.files[0])));
$$(".slides-remove").forEach((btn) => btn.addEventListener("click", () => setSlides(null)));

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
  // A document is summarised as it is: no audio to transcribe, and slides wouldn't add anything.
  const doc = isDocument(file.name);
  $("#file-process").textContent = doc ? "Write notes from document" : "Transcribe & write notes";
  $("#upload-slides-pick").hidden = doc;
  if (doc) setSlides(null);
  setBusy(state.busy);
}

const DOCUMENT_TYPES = /\.(pdf|docx|pptx|txt|md|png|jpe?g)$/i;
const isDocument = (name) => DOCUMENT_TYPES.test(name || "");

$("#file-input").addEventListener("change", (e) => setFile(e.target.files[0]));
const dz = $("#dropzone");
["dragenter", "dragover"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("drag"); }));
["dragleave", "drop"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("drag"); }));
dz.addEventListener("drop", (e) => setFile(e.dataTransfer.files[0]));

$("#file-process").addEventListener("click", () => state.file && processUpload(state.file, state.file.name, false));

$("#url-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  clearError();
  startWorking("Sending link…");
  try {
    const form = new FormData();
    form.append("url", $("#url-input").value.trim());
    form.append("extras", chosenExtras().join(","));
    form.append("usual_person_id", recall(WHO_KEY));
    if (state.slides) form.append("slides_file", state.slides);
    const job = await api("/api/jobs/url", { method: "POST", body: form });
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
    form.append("extras", chosenExtras().join(","));
    form.append("usual_person_id", recall(WHO_KEY));
    if (state.slides) form.append("slides_file", state.slides);
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

async function processUpload(blob, filename, fromRecording) {
  clearError();
  state.fromRecording = fromRecording;
  state.earlyPlacement = null;
  startWorking(isDocument(filename) ? "Uploading document…" : "Uploading…");
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
      if (job.transcript) {
        // The notes exist now, so the recording backup is no longer needed.
        if (state.fromRecording) {
          store.backupClear().catch(() => {});
          $("#rec-note").textContent = "";
        }
        // Where in Notion it goes, if that was worked out alongside the notes (or during the lecture).
        sendState.early = state.earlyPlacement || job.placement || null;
        state.earlyPlacement = null;
        showResults(job);
        saveToHistory(job);
        setSlides(null); // slides belong to this lecture; don't reuse them for the next one
      }
      if (job.warning) showError(job.warning);
      if (job.status === "error") {
        const what = job.source === "document" ? "The document was read" : "Transcript is ready";
        showError(job.transcript ? `${what}, but notes failed: ${job.error}` : job.error);
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

function fillQuestions(questions) {
  const list = $("#note-questions");
  list.replaceChildren();
  for (const item of questions) {
    const li = document.createElement("li");
    const q = document.createElement("p");
    q.className = "question";
    q.textContent = item.q;
    const answer = document.createElement("details");
    const summary = document.createElement("summary");
    summary.textContent = "Show answer";
    const a = document.createElement("p");
    a.textContent = item.a;
    answer.append(summary, a);
    li.append(q, answer);
    list.append(li);
  }
  $("#questions-block").hidden = !questions.length;
}

function fillTerms(terms) {
  const list = $("#note-terms");
  list.replaceChildren();
  for (const item of terms) {
    const dt = document.createElement("dt");
    dt.textContent = item.term;
    const dd = document.createElement("dd");
    dd.textContent = item.definition;
    list.append(dt, dd);
  }
  $("#terms-block").hidden = !terms.length;
}

function fillSimpleList(listSel, blockSel, items) {
  const list = $(listSel);
  list.replaceChildren(...items.map((text) => Object.assign(document.createElement("li"), { textContent: text })));
  $(blockSel).hidden = !items.length;
}

function fillPairs(listSel, blockSel, items, first, second) {
  const list = $(listSel);
  list.replaceChildren();
  for (const item of items) {
    list.append(Object.assign(document.createElement("dt"), { textContent: item[first] }),
                Object.assign(document.createElement("dd"), { textContent: item[second] }));
  }
  $(blockSel).hidden = !items.length;
}

function fillQuiz(quiz) {
  const list = $("#note-quiz");
  list.replaceChildren();
  for (const item of quiz) {
    const li = document.createElement("li");
    const q = Object.assign(document.createElement("p"), { className: "question", textContent: item.question });
    const options = document.createElement("ol");
    options.className = "options";
    item.options.forEach((opt) => options.append(Object.assign(document.createElement("li"), { textContent: opt })));
    const answer = document.createElement("details");
    const summary = Object.assign(document.createElement("summary"), { textContent: "Show answer" });
    const text = `${"ABCD"[item.answer]}) ${item.options[item.answer]}` + (item.explanation ? ` — ${item.explanation}` : "");
    answer.append(summary, Object.assign(document.createElement("p"), { textContent: "✅ " + text }));
    li.append(q, options, answer);
    list.append(li);
  }
  $("#quiz-block").hidden = !quiz.length;
}

function fillCards(cards) {
  const box = $("#note-cards");
  box.replaceChildren();
  for (const card of cards) {
    // Click a card to flip it.
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "card-flip";
    btn.textContent = card.front;
    btn.title = "Click to flip";
    btn.addEventListener("click", () => {
      const flipped = btn.classList.toggle("flipped");
      btn.textContent = flipped ? card.back : card.front;
    });
    box.append(btn);
  }
  $("#cards-block").hidden = !cards.length;
}

$("#copy-cards").addEventListener("click", async () => {
  const cards = state.result?.notes?.flashcards || [];
  // Quizlet and Anki both import "front<TAB>back" lines.
  const text = cards.map((c) => `${c.front.replace(/\s+/g, " ")}\t${c.back.replace(/\s+/g, " ")}`).join("\n");
  try {
    await navigator.clipboard.writeText(text);
    $("#copy-cards").textContent = "Copied ✓ (paste into Quizlet's or Anki's import)";
    setTimeout(() => ($("#copy-cards").textContent = "Copy for Quizlet / Anki"), 2500);
  } catch {
    showError("Couldn't access the clipboard.");
  }
});

function showResults(job) {
  const notes = job.notes || { title: job.label || "Notes", summary: "", key_points: [], action_items: [] };
  state.result = { transcript: job.transcript, notes };
  state.historyId = job.id;
  $("#note-title").value = notes.title || "Notes";
  $("#note-summary").textContent = notes.summary || "—";
  fillList($("#note-points"), notes.key_points || [], "—");
  fillList($("#note-actions"), notes.action_items || [], "None mentioned.");
  fillQuestions(notes.practice_questions || []);
  fillTerms(notes.key_terms || []);
  fillSimpleList("#note-cheat", "#cheat-block", notes.cheat_sheet || []);
  fillPairs("#note-explained", "#explained-block", notes.explanations || [], "topic", "explanation");
  fillQuiz(notes.quiz || []);
  fillCards(notes.flashcards || []);
  $("#notes-body").hidden = !job.notes;
  $("#note-transcript").textContent = job.transcript;
  $("#transcript-label").textContent = notes.source === "document" ? "Full text" : "Full transcript";
  $("#word-count").textContent = `(${job.transcript.split(/\s+/).filter(Boolean).length.toLocaleString()} words)`;
  $("#results").hidden = false;
  $("#notion-card").hidden = false;
  $("#notion-result").textContent = "";
  if (state.config?.notion_configured) startNotionFlow();
  $("#results").scrollIntoView({ behavior: "smooth", block: "start" });
}

function toMarkdown() {
  const { notes, transcript } = state.result;
  const lines = [`# ${$("#note-title").value}`, "", "## Summary", notes.summary || "—", "", "## Key points"];
  (notes.key_points.length ? notes.key_points : ["—"]).forEach((p) => lines.push(`- ${p}`));
  if (notes.key_terms?.length) {
    lines.push("", "## Key terms");
    notes.key_terms.forEach((x) => lines.push(`- **${x.term}**: ${x.definition}`));
  }
  lines.push("", "## Action items");
  (notes.action_items.length ? notes.action_items.map((a) => `- [ ] ${a}`) : ["None mentioned."]).forEach((a) => lines.push(a));
  if (notes.practice_questions?.length) {
    lines.push("", "## Practice questions");
    notes.practice_questions.forEach((x, i) => lines.push(`${i + 1}. ${x.q}`, `   - Answer: ${x.a}`));
  }
  if (notes.cheat_sheet?.length) {
    lines.push("", "## Cheat sheet");
    notes.cheat_sheet.forEach((x) => lines.push(`- ${x}`));
  }
  if (notes.explanations?.length) {
    lines.push("", "## Explained simply");
    notes.explanations.forEach((x) => lines.push(`**${x.topic}**: ${x.explanation}`, ""));
  }
  if (notes.quiz?.length) {
    lines.push("", "## Quiz");
    notes.quiz.forEach((x, i) => {
      lines.push(`${i + 1}. ${x.question}`);
      x.options.forEach((o, j) => lines.push(`   ${"ABCD"[j]}) ${o}`));
      lines.push(`   - Answer: ${"ABCD"[x.answer]}${x.explanation ? ` (${x.explanation})` : ""}`);
    });
  }
  if (notes.flashcards?.length) {
    lines.push("", "## Flashcards", "| Front | Back |", "| --- | --- |");
    notes.flashcards.forEach((x) => lines.push(`| ${x.front.replace(/\|/g, "/")} | ${x.back.replace(/\|/g, "/")} |`));
  }
  lines.push("", notes.source === "document" ? "## Full text" : "## Full transcript", transcript);
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
// Who is it for → which class (the AI guesses from the lecture) → the AI suggests where it
// goes in that person's Notion → Send.

const WHO_KEY = "lecture-notes-who";
const LAST_CLASS_KEY = "lecture-notes-last-class"; // { personId: classId }
const OTHER = "__other";
const sendState = { people: null, plan: null, planSeq: 0, classSeq: 0, guessSeq: 0 };

function recall(key) {
  try { return localStorage.getItem(key) || ""; } catch { return ""; }
}
function remember(key, value) {
  try { localStorage.setItem(key, value); } catch { /* storage blocked */ }
}
function lastClasses() {
  try { return JSON.parse(recall(LAST_CLASS_KEY) || "{}"); } catch { return {}; }
}

function noteContext() {
  return { note_title: $("#note-title").value.trim(), summary: state.result?.notes?.summary || "" };
}

function postJson(path, body) {
  return api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
}

async function startNotionFlow() {
  $("#notion-result").textContent = "";
  if (!sendState.people) await loadPeople();
  await guessOwner();
}

// The AI compares what the lecture is about with everyone's classes (and lecture pages) to
// guess whose lecture it is and which class. Both stay editable.
async function guessOwner() {
  const seq = ++sendState.guessSeq;
  const hint = $("#who-hint");
  hint.textContent = "🤖 Figuring out whose lecture this is…";
  $("#class-step").hidden = true;
  $("#plan-step").hidden = true;
  let guess = {};
  const early = sendState.early; // already worked out while the notes were being written
  if (early) guess = early;
  else {
    try {
      guess = await postJson("/api/notion/guess", { ...noteContext(), usual_person_id: recall(WHO_KEY) });
    } catch { /* fall back to the saved name */ }
  }
  if (seq !== sendState.guessSeq) return; // the user picked a name themselves meanwhile

  const known = sendState.people?.some((p) => p.id === guess.person_id);
  if (known) {
    $("#who").value = guess.person_id;
    hint.textContent = "🤖 Guessed from the lecture. Change it if that's wrong.";
    await loadClasses({ preferClass: guess.class_id, preferText: guess.class_name });
  } else {
    hint.textContent = "";
    await loadClasses();
  }
}

async function loadPeople(refresh = false) {
  const who = $("#who");
  who.replaceChildren(new Option("Loading names…", ""));
  try {
    const { people } = await api(`/api/notion/people${refresh ? "?refresh=true" : ""}`);
    sendState.people = people;
    who.replaceChildren(new Option("Choose a name…", ""),
      ...people.map((p) => new Option(`${p.icon ? p.icon + " " : ""}${p.title}`, p.id)));
    const saved = recall(WHO_KEY);
    if (people.some((p) => p.id === saved)) who.value = saved;
  } catch (err) {
    who.replaceChildren(new Option("Couldn't load names", ""));
    showError(err.message);
  }
}

// `prefer` comes from the AI's guess: a class to select, or a class name to type in.
async function loadClasses(prefer = {}) {
  const guessed = !!(prefer.preferClass || prefer.preferText);
  const personId = $("#who").value;
  const seq = ++sendState.classSeq;
  $("#class-step").hidden = !personId;
  $("#plan-step").hidden = true;
  if (!personId) return;

  const select = $("#which-class");
  const hint = $("#class-hint");
  select.disabled = true;
  select.replaceChildren(new Option("Looking through their Notion…", ""));
  hint.textContent = "";
  let classes = [], guess = null;
  try {
    // When the class was already guessed together with the person, don't guess it again.
    const context = guessed ? {} : noteContext();
    ({ classes, guess } = await postJson("/api/notion/classes", { person_id: personId, ...context }));
  } catch (err) {
    showError(err.message);
  }
  if (seq !== sendState.classSeq) return; // the person changed while we were looking

  select.replaceChildren(new Option("Choose a class…", ""));
  const groups = new Map();
  for (const c of classes) {
    if (!groups.has(c.group)) groups.set(c.group, []);
    groups.get(c.group).push(c);
  }
  for (const [name, items] of groups) {
    const group = document.createElement("optgroup");
    group.label = name;
    items.forEach((c) => group.append(new Option(`${c.icon ? c.icon + " " : ""}${c.title}`, c.id)));
    select.append(group);
  }
  select.append(new Option("Something else (type it)…", OTHER));
  select.disabled = false;

  const last = lastClasses()[personId];
  if (prefer.preferClass && classes.some((c) => c.id === prefer.preferClass)) {
    select.value = prefer.preferClass;
    hint.textContent = "🤖 Guessed from the lecture. Change it if that's wrong.";
  } else if (prefer.preferText) {
    select.value = OTHER;
    $("#class-other").value = prefer.preferText;
    hint.textContent = "🤖 Guessed from their lecture pages. Change it if that's wrong.";
  } else if (guess) {
    select.value = guess;
    hint.textContent = "🤖 Guessed from the lecture. Change it if that's wrong.";
  } else if (classes.some((c) => c.id === last)) {
    select.value = last;
    hint.textContent = "Same class as last time.";
  } else if (!classes.length) {
    select.value = OTHER;
    hint.textContent = "No class list found in their Notion. Type the class name and press Enter.";
  }
  onClassChange();
}

function onClassChange() {
  const value = $("#which-class").value;
  const other = value === OTHER;
  $("#class-other").hidden = !other;
  if (other) {
    if ($("#class-other").value.trim()) makePlan();
    else {
      $("#plan-step").hidden = true;
      $("#class-other").focus();
    }
    return;
  }
  if (!value) {
    $("#plan-step").hidden = true;
    return;
  }
  remember(LAST_CLASS_KEY, JSON.stringify({ ...lastClasses(), [$("#who").value]: value }));
  makePlan();
}

async function makePlan() {
  const value = $("#which-class").value;
  const body = {
    person_id: $("#who").value,
    class_id: value && value !== OTHER ? value : null,
    class_text: value === OTHER ? $("#class-other").value.trim() : "",
    ...noteContext(),
  };
  if (!body.person_id || (!body.class_id && !body.class_text)) return;

  // Worked out already (during the lecture, or while the notes were written) for this same choice?
  const early = sendState.early;
  sendState.early = null;
  const ready = early?.plan && early.plan_for && early.plan_for.person_id === body.person_id
    && (early.plan_for.class_id || null) === body.class_id && (early.plan_for.class_text || "") === body.class_text;

  const seq = ++sendState.planSeq;
  $("#notion-result").textContent = "";
  $("#plan-step").hidden = false;
  $("#plan-where").textContent = "Finding the best spot…";
  $("#plan-path").textContent = "";
  $("#plan-why").textContent = "";
  $("#plan-alt").replaceChildren();
  $("#notion-send").disabled = true;
  try {
    const plan = ready ? early.plan : await postJson("/api/notion/plan", body);
    if (seq !== sendState.planSeq) return; // a newer choice replaced this one
    sendState.plan = plan;
    $("#plan-alt").replaceChildren(...plan.candidates.map((c, i) => new Option(c.label, String(i))));
    $("#plan-alt").value = String(plan.best);
    showPlace(plan.best, plan.reason);
    $("#notion-send").disabled = false;
  } catch (err) {
    if (seq !== sendState.planSeq) return;
    $("#plan-where").textContent = "Couldn't work out where it goes.";
    showError(err.message);
  }
}

function showPlace(index, reason) {
  const place = sendState.plan.candidates[index];
  $("#plan-where").textContent = place.label;
  $("#plan-path").textContent = place.where;
  $("#plan-why").textContent = reason ? `🤖 ${reason}` : "";
}

$("#who").addEventListener("change", () => {
  sendState.guessSeq++; // the user chose; ignore any guess still on its way
  sendState.early = null;
  $("#who-hint").textContent = "";
  remember(WHO_KEY, $("#who").value);
  loadClasses();
});
$("#which-class").addEventListener("change", onClassChange);
$("#class-other").addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    e.preventDefault();
    makePlan();
  }
});
$("#class-other").addEventListener("change", makePlan);
$("#plan-alt").addEventListener("change", () => showPlace(Number($("#plan-alt").value), ""));
$("#notion-refresh").addEventListener("click", async () => {
  await loadPeople(true);
  await loadClasses();
});

$("#notion-send").addEventListener("click", async () => {
  if (!state.result || !sendState.plan) return;
  clearError();
  const place = sendState.plan.candidates[Number($("#plan-alt").value)];
  const btn = $("#notion-send");
  btn.disabled = true;
  btn.textContent = "Sending…";
  $("#notion-result").textContent = "";
  try {
    const { notes, transcript } = state.result;
    const { url } = await postJson("/api/notion/export", {
      place: { kind: place.kind, target_id: place.target_id, link_to: place.link_to },
      title: $("#note-title").value.trim() || "Lecture notes", // the lecture's title, from its content
      notes, // summary, key points, action items and whichever study extras were made
      transcript,
      local_date: new Date().toLocaleDateString("en-CA"), // YYYY-MM-DD in the user's timezone
    });
    const a = document.createElement("a");
    a.href = url;
    a.target = "_blank";
    a.rel = "noopener";
    a.textContent = "Open in Notion ↗";
    $("#notion-result").replaceChildren("✅ Saved to Notion. ", a);
    if (state.historyId) {
      store.historyUpdate(state.historyId, { notion: { url, where: place.label } }).then(renderHistory).catch(() => {});
    }
  } catch (err) {
    showError(err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "Send to Notion";
  }
});

// ---------- recent notes (saved on this device) ----------

async function saveToHistory(job) {
  const notes = state.result.notes;
  try {
    await store.historySave({ id: job.id, date: Date.now(), title: notes.title || job.label || "Notes",
                              notes, transcript: job.transcript, notion: null });
  } catch { /* storage blocked: no history, everything else still works */ }
  renderHistory();
}

async function renderHistory() {
  let items = [];
  try { items = await store.historyList(); } catch { /* no storage */ }
  const list = $("#history-list");
  list.replaceChildren();
  for (const item of items) {
    const li = document.createElement("li");
    const open = document.createElement("button");
    open.type = "button";
    open.className = "history-open";
    const title = document.createElement("span");
    title.className = "history-title";
    title.textContent = item.title;
    const meta = document.createElement("span");
    meta.className = "muted small";
    const when = new Date(item.date).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
    meta.textContent = item.notion ? `${when} · ✓ Sent to Notion` : when;
    open.append(title, meta);
    open.addEventListener("click", () => {
      clearError();
      sendState.early = null;
      showResults({ id: item.id, label: item.title, notes: item.notes, transcript: item.transcript });
    });
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "icon-btn";
    remove.setAttribute("aria-label", `Delete ${item.title}`);
    remove.textContent = "×";
    remove.addEventListener("click", async () => {
      if (!confirm(`Delete “${item.title}” from this device? (Anything already in Notion stays there.)`)) return;
      await store.historyDelete(item.id).catch(() => {});
      renderHistory();
    });
    li.append(open, remove);
    list.append(li);
  }
  $("#history-card").hidden = !items.length;
}

// Keep the saved copy's title in sync when the user edits it.
$("#note-title").addEventListener("change", () => {
  if (!state.historyId || !state.result) return;
  const title = $("#note-title").value.trim() || "Notes";
  state.result.notes = { ...state.result.notes, title };
  store.historyUpdate(state.historyId, { title, notes: state.result.notes }).then(renderHistory).catch(() => {});
});

// ---------- startup ----------

$("#error-close").addEventListener("click", clearError);

async function init() {
  selectSource("mic");
  checkForBackup();
  renderHistory();
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
  if (cfg.password_required && !getPassword()) lock();
}

init();
