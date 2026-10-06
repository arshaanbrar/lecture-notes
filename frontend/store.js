"use strict";

// On-device storage (IndexedDB): a backup of the recording in progress, and recent notes.
// Everything here stays in this browser only; nothing is uploaded. Every call fails soft:
// if storage is blocked (private mode, full disk…), the app still works without it.

const store = (() => {
  const DB_NAME = "lecture-notes";
  const MAX_HISTORY = 30;
  let dbPromise = null;

  function open() {
    if (!dbPromise) {
      dbPromise = new Promise((resolve, reject) => {
        if (!window.indexedDB) return reject(new Error("Storage isn't available."));
        const req = indexedDB.open(DB_NAME, 1);
        req.onupgradeneeded = () => {
          const db = req.result;
          db.createObjectStore("chunks", { autoIncrement: true }); // recording backup, in order
          db.createObjectStore("meta");                            // "recording" → backup info
          db.createObjectStore("history", { keyPath: "id" });     // recent notes
        };
        req.onsuccess = () => resolve(req.result);
        req.onerror = () => reject(req.error);
      }).catch((err) => {
        dbPromise = null;
        throw err;
      });
    }
    return dbPromise;
  }

  // Run `fn(store)` in a transaction; resolves with the value of the request it returns.
  async function tx(name, mode, fn) {
    const db = await open();
    return new Promise((resolve, reject) => {
      const t = db.transaction(name, mode);
      const req = fn(t.objectStore(name));
      t.oncomplete = () => resolve(req ? req.result : undefined);
      t.onerror = () => reject(t.error);
      t.onabort = () => reject(t.error);
    });
  }

  // ----- recording backup (one at a time) -----

  async function backupStart(info) {
    await tx("chunks", "readwrite", (s) => s.clear());
    await tx("meta", "readwrite", (s) => s.put({ ...info, seconds: 0 }, "recording"));
  }

  async function backupChunk(blob, seconds) {
    await tx("chunks", "readwrite", (s) => s.add(blob));
    const info = await tx("meta", "readonly", (s) => s.get("recording"));
    if (info) await tx("meta", "readwrite", (s) => s.put({ ...info, seconds }, "recording"));
  }

  async function backupLoad() {
    const info = await tx("meta", "readonly", (s) => s.get("recording"));
    if (!info) return null;
    const chunks = await tx("chunks", "readonly", (s) => s.getAll());
    if (!chunks.length) return null;
    return { info, blob: new Blob(chunks, { type: info.mimeType || "audio/webm" }) };
  }

  async function backupClear() {
    await tx("chunks", "readwrite", (s) => s.clear());
    await tx("meta", "readwrite", (s) => s.delete("recording"));
  }

  // ----- recent notes -----

  async function historyList() {
    const items = await tx("history", "readonly", (s) => s.getAll());
    return items.sort((a, b) => b.date - a.date);
  }

  async function historySave(item) {
    await tx("history", "readwrite", (s) => s.put(item));
    const items = await historyList();
    for (const old of items.slice(MAX_HISTORY)) await historyDelete(old.id);
  }

  async function historyUpdate(id, changes) {
    const item = await tx("history", "readonly", (s) => s.get(id));
    if (item) await tx("history", "readwrite", (s) => s.put({ ...item, ...changes }));
  }

  async function historyDelete(id) {
    await tx("history", "readwrite", (s) => s.delete(id));
  }

  return { backupStart, backupChunk, backupLoad, backupClear, historyList, historySave, historyUpdate, historyDelete };
})();
