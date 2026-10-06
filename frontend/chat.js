"use strict";

// Study helper chat (bottom-right). Uses whatever lecture is in front of the user:
// - while recording: the audio recorded so far, transcribed a bit at a time as questions come in;
// - after: the notes and full transcript;
// - otherwise it's a general study tutor.
// Relies on globals from app.js: state, api, $, fmtTime.

const chat = {
  messages: [],      // {role: "user"|"assistant", content}
  busy: false,
  contextKey: null,  // which lecture the conversation is about
  live: { recId: null, transcript: "", synced: 0 },
};

const SUGGESTIONS = {
  live: ["Explain the last few minutes simply", "What's the main idea so far?", "Give me an example of what was just said"],
  after: ["Explain the hardest part simply", "Quiz me on this lecture", "What should I review first?"],
  none: ["How should I study for a test?", "Explain a concept I'm stuck on"],
};

function chatMode() {
  if (state.rec) return "live";
  if (state.result) return "after";
  if (chat.live.transcript) return "recorded"; // stopped, notes not made yet
  return "none";
}

function chatContextKey() {
  const mode = chatMode();
  if (mode === "live" || mode === "recorded") return `rec:${chat.live.recId || state.rec?.startedAt}`;
  if (mode === "after") return `notes:${state.historyId || state.result.notes.title}`;
  return "none";
}

function updateChatHeader() {
  const mode = chatMode();
  const label = {
    live: () => `🔴 Listening to this lecture (${fmtTime((Date.now() - state.rec.startedAt) / 1000)} so far)`,
    recorded: () => "Using what was recorded",
    after: () => `Using: ${$("#note-title").value || "this lecture"}`,
    none: () => "No lecture open, so ask anything",
  }[mode]();
  $("#chat-context").textContent = label;
  const chips = $("#chat-suggestions");
  const list = SUGGESTIONS[mode === "recorded" ? "after" : mode];
  if (chips.dataset.mode !== mode) {
    chips.dataset.mode = mode;
    chips.replaceChildren(...list.map((text) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "chip";
      b.textContent = text;
      b.addEventListener("click", () => askHelper(text));
      return b;
    }));
  }
  chips.hidden = chat.busy || chat.messages.length > 0;
}

// ----- catching up on a recording in progress -----

async function catchUpOnRecording() {
  const rec = state.rec;
  if (!rec) return;
  if (chat.live.recId !== rec.startedAt) chat.live = { recId: rec.startedAt, transcript: "", synced: 0 };
  const chunks = rec.chunks;
  if (chunks.length <= chat.live.synced) return;
  // Only send what's new. The first chunk carries the audio file's header, so later pieces
  // need it in front to be readable.
  const fresh = chunks.slice(chat.live.synced);
  const parts = chat.live.synced === 0 ? fresh : [chunks[0], ...fresh];
  const upTo = chunks.length;
  const type = rec.mimeType || "audio/webm";
  const form = new FormData();
  form.append("audio_file", new Blob(parts, { type }), type.includes("mp4") ? "snippet.m4a" : "snippet.webm");
  // The header piece is the recording's first second; drop it again so it isn't heard twice.
  if (chat.live.synced > 0) form.append("skip_seconds", "1");
  const { text } = await api("/api/assistant/transcribe", { method: "POST", body: form });
  if (chat.live.recId !== rec.startedAt) return; // a new recording started meanwhile
  chat.live.synced = upTo;
  if (text) chat.live.transcript += (chat.live.transcript ? " " : "") + text;
}

function chatContext() {
  const mode = chatMode();
  if (mode === "live") return { live: true, transcript: chat.live.transcript };
  if (mode === "recorded") return { transcript: chat.live.transcript, title: "Lecture just recorded" };
  if (mode === "after") {
    const n = state.result.notes;
    return {
      title: $("#note-title").value || n.title, summary: n.summary || "", key_points: n.key_points || [],
      key_terms: n.key_terms || [], transcript: state.result.transcript || "",
    };
  }
  return {};
}

// ----- conversation -----

function formatReply(text) {
  // Minimal, safe formatting: escape everything, then allow **bold**, `code` and line breaks.
  const escaped = text.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  return escaped
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\n/g, "<br>");
}

function addBubble(role, text, extraClass = "") {
  const div = document.createElement("div");
  div.className = `bubble ${role} ${extraClass}`.trim();
  if (role === "assistant") div.innerHTML = formatReply(text);
  else div.textContent = text;
  $("#chat-log").append(div);
  $("#chat-log").scrollTop = $("#chat-log").scrollHeight;
  return div;
}

async function askHelper(question) {
  question = question.trim();
  if (!question || chat.busy) return;

  // A different lecture than the conversation was about: start a fresh conversation.
  const key = chatContextKey();
  if (chat.contextKey !== key) {
    if (chat.messages.length) addBubble("note", `${$("#chat-context").textContent}. Starting a fresh conversation.`);
    chat.messages = [];
    chat.contextKey = key;
  }

  chat.messages.push({ role: "user", content: question });
  addBubble("user", question);
  $("#chat-input").value = "";
  chat.busy = true;
  updateChatHeader();
  const thinking = addBubble("assistant", state.rec ? "Catching up on the lecture…" : "Thinking…", "pending");
  try {
    if (state.rec) {
      try { await catchUpOnRecording(); } catch { /* answer with what we have */ }
      thinking.innerHTML = formatReply("Thinking…");
    }
    const { reply } = await api("/api/assistant/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages: chat.messages.slice(-12), context: chatContext() }),
    });
    chat.messages.push({ role: "assistant", content: reply });
    thinking.classList.remove("pending");
    thinking.innerHTML = formatReply(reply);
  } catch (err) {
    chat.messages.pop(); // let them ask again
    thinking.classList.remove("pending");
    thinking.classList.add("error");
    thinking.textContent = `Couldn't answer: ${err.message}`;
  } finally {
    chat.busy = false;
    updateChatHeader();
    $("#chat-log").scrollTop = $("#chat-log").scrollHeight;
  }
}

// ----- opening / closing -----

let chatTimer = null;

function openChat(open) {
  $("#chat-panel").hidden = !open;
  $("#chat-fab").hidden = open;
  clearInterval(chatTimer);
  if (open) {
    updateChatHeader();
    chatTimer = setInterval(updateChatHeader, 1000);
    $("#chat-input").focus();
  }
}

$("#chat-fab").addEventListener("click", () => openChat(true));
$("#chat-close").addEventListener("click", () => openChat(false));
$("#chat-form").addEventListener("submit", (e) => {
  e.preventDefault();
  askHelper($("#chat-input").value);
});
$("#chat-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    askHelper($("#chat-input").value);
  }
});
