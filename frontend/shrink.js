"use strict";

// Before uploading a lecture video, keep only its sound: the notes only need the audio, and the
// video part is usually 90%+ of the file. A 300 MB video becomes about 15-60 MB, so it uploads
// many times faster (especially on phone data or slow Wi-Fi). The audio is copied as is, not
// re-encoded, so this is quick and loses nothing. Works for .mp4/.mov/.m4v (phones, Zoom, most
// screen recorders); other files, or anything that goes wrong, are uploaded unchanged.
// Uses mp4box.js (~150 KB, loaded only when needed) and loadScript from ocr.js.

const MP4BOX = "https://cdn.jsdelivr.net/npm/mp4box@0.5.2/dist/mp4box.all.min.js";
const SHRINKABLE = /\.(mp4|mov|m4v)$/i;
const SHRINK_MIN_BYTES = 15 * 1024 * 1024; // smaller files upload quickly anyway
const READ_BYTES = 8 * 1024 * 1024;         // read the file 8 MB at a time

// Returns a smaller audio-only file (.m4a), or null to upload the original.
async function audioOnly(file, progress) {
  if (!SHRINKABLE.test(file.name || "") || file.size < SHRINK_MIN_BYTES) return null;
  await loadScript(MP4BOX);
  const mp4 = MP4Box.createFile(false); // don't keep the video data around
  const pieces = [];
  let ready = null, done = false, failed = null;

  mp4.onError = (e) => { failed = new Error(String(e)); };
  mp4.onReady = (info) => {
    const audio = info.audioTracks[0];
    // Anything besides the sound (video, usually) is what gets dropped.
    ready = { audio, hasVideo: !!audio && info.tracks.some((t) => t.id !== audio.id) };
    if (!audio || !ready.hasVideo) return;
    mp4.setSegmentOptions(audio.id, null, { nbSamples: 1000 });
    pieces.push(mp4.initializeSegmentation()[0].buffer); // the audio file's header
    mp4.start();
  };
  mp4.onSegment = (id, user, buffer, sampleNumber, last) => {
    pieces.push(buffer);
    mp4.releaseUsedSamples(id, sampleNumber); // free the copied audio
    if (last) done = true;
    const total = ready.audio.nb_samples || 1;
    progress(`Taking the sound out of the video… ${Math.min(99, Math.round((sampleNumber / total) * 100))}%`);
  };

  // Read where mp4box asks next: it skips over video it doesn't need (and, when the file's index is
  // at the end, jumps there first).
  let position = 0;
  while (position < file.size && !done && !failed) {
    const buffer = await file.slice(position, position + READ_BYTES).arrayBuffer();
    buffer.fileStart = position;
    const next = mp4.appendBuffer(buffer, position + buffer.byteLength >= file.size);
    if (ready && (!ready.audio || !ready.hasVideo)) return null; // no sound, or no video to drop
    // Usually the next place to read; past the end means "nothing more needed".
    position = typeof next === "number" && next !== position ? next : position + buffer.byteLength;
  }
  mp4.flush();
  if (failed || !done || !ready?.audio || pieces.length < 2) return null; // incomplete: upload the original

  const audio = new Blob(pieces, { type: "audio/mp4" });
  // Not worth it if the video part was small (e.g. a mostly still screen).
  if (audio.size > file.size * 0.7) return null;
  return new File([audio], file.name.replace(/\.[^.]+$/, "") + ".m4a", { type: "audio/mp4" });
}
