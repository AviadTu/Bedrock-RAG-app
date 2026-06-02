/**
 * static/js/chat.js
 * ─────────────────
 * Vanilla JavaScript chat client.
 *
 * Responsibilities
 *   • Poll /status on load to wait for the RAG engine to be ready.
 *   • Restore conversation history from /history on page load.
 *   • Send user messages to POST /chat and render responses.
 *   • Show a typing indicator while waiting for the response.
 *   • Render Knowledge Base source references in a collapsible section.
 *   • Support "New Chat" (POST /clear) to reset the session.
 *   • Auto-grow textarea; submit on Enter (Shift+Enter = newline).
 */

"use strict";

/* ═══════════════════════════════════════════════════════════════════
   DOM references
═══════════════════════════════════════════════════════════════════ */
const messagesEl    = document.getElementById("messages");
const userInput     = document.getElementById("user-input");
const btnSend       = document.getElementById("btn-send");
const btnNewChat    = document.getElementById("btn-new-chat");
const btnUpload     = document.getElementById("btn-upload");
const fileInput     = document.getElementById("file-input");
const uploadStatus  = document.getElementById("upload-status");
const btnSidebar    = document.getElementById("btn-toggle-sidebar");
const sidebar       = document.getElementById("sidebar");
const statusDot     = document.getElementById("status-dot");
const statusLabel   = document.getElementById("status-label");
const progressWrap  = document.getElementById("progress-wrap");
const progressBar   = document.getElementById("progress-bar");
const msgCount      = document.getElementById("msg-count");
const welcomeCard   = document.getElementById("welcome");

/* ═══════════════════════════════════════════════════════════════════
   Templates
═══════════════════════════════════════════════════════════════════ */
const tplUser       = document.getElementById("tpl-user");
const tplAssistant  = document.getElementById("tpl-assistant");
const tplThinking   = document.getElementById("tpl-thinking");

/* ═══════════════════════════════════════════════════════════════════
   State
═══════════════════════════════════════════════════════════════════ */
let isEngineReady   = false;
let isBusy          = false;
let historyCount    = 0;
let statusPollTimer = null;

/* ═══════════════════════════════════════════════════════════════════
   Session identity (per-tab)
   ───────────────────────────────────────────────────────────────────
   The session_id is stored in sessionStorage, which the browser clears
   automatically when the tab is closed.  Reloads in the same tab keep
   the ID, so conversation memory survives a refresh.  Closing the tab
   and reopening the site yields a fresh, empty session.
═══════════════════════════════════════════════════════════════════ */
const SESSION_STORAGE_KEY = "rag_session_id";

function generateUuid() {
  // Prefer the secure native generator when available.
  if (window.crypto && typeof window.crypto.randomUUID === "function") {
    return window.crypto.randomUUID();
  }
  // RFC4122 v4 fallback for older browsers.
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    const v = c === "x" ? r : (r & 0x3) | 0x8;
    return v.toString(16);
  });
}

function getSessionId() {
  let sid = sessionStorage.getItem(SESSION_STORAGE_KEY);
  if (!sid) {
    sid = generateUuid();
    sessionStorage.setItem(SESSION_STORAGE_KEY, sid);
  }
  return sid;
}

/**
 * Drop-in replacement for `fetch()` that adds the X-Session-Id header
 * to every request.  All app traffic must go through this helper so the
 * backend can isolate per-tab sessions.
 */
function fetchWithSession(url, opts = {}) {
  const headers = new Headers(opts.headers || {});
  headers.set("X-Session-Id", getSessionId());
  return fetch(url, { ...opts, headers });
}

/* ═══════════════════════════════════════════════════════════════════
   Utility helpers
═══════════════════════════════════════════════════════════════════ */

/** Escape text to safe HTML so XSS is not possible. */
function escapeHtml(text) {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/** Scroll the messages panel to the very bottom. */
function scrollBottom() {
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

/** Show / hide the welcome card depending on message count. */
function syncWelcome() {
  if (welcomeCard) {
    welcomeCard.style.display = historyCount === 0 ? "block" : "none";
  }
}

/** Update the sidebar message counter. */
function updateMsgCount(n) {
  historyCount = n;
  msgCount.textContent = n;
  syncWelcome();
}

/* ═══════════════════════════════════════════════════════════════════
   Engine status polling
═══════════════════════════════════════════════════════════════════ */

async function pollStatus() {
  try {
    const res  = await fetchWithSession("/status");
    const data = await res.json();

    const status = data.status || "ready";

    // The Knowledge Base is always queryable, so chat input is always on.
    isEngineReady = true;
    setInputEnabled(true);

    if (status === "ingesting") {
      // Documents are still being processed by Bedrock – keep polling.
      setStatusLoading(status);
      if (!statusPollTimer) {
        statusPollTimer = setInterval(pollStatus, 3000);
      }
      return;
    }

    if (status === "ingestion_failed") {
      setStatusError("עיבוד המסמכים נכשל");
    } else {
      setStatusReady();
    }

    if (statusPollTimer) {
      clearInterval(statusPollTimer);
      statusPollTimer = null;
    }
  } catch (_) {
    setStatusError("לא ניתן להתחבר לשרת");
  }
}

function setStatusReady() {
  statusDot.className = "status-dot ready";
  statusLabel.textContent = "מוכן";
  progressWrap.style.display = "none";
}

function setStatusLoading(status) {
  statusDot.className = "status-dot loading";

  const labels = {
    ingesting: "מעבד מסמכים…",
    ready:     "מוכן",
  };

  statusLabel.textContent = labels[status] || "מעבד…";
  // Bedrock does not report granular progress; show an indeterminate state.
  progressWrap.style.display = "none";
}

function setStatusError(msg) {
  statusDot.className = "status-dot error";
  statusLabel.textContent = msg || "שגיאה";
  progressWrap.style.display = "none";
}

/* ═══════════════════════════════════════════════════════════════════
   Message rendering
═══════════════════════════════════════════════════════════════════ */

function appendUserBubble(text) {
  const node = tplUser.content.cloneNode(true);
  node.querySelector(".msg-text").textContent = text;
  messagesEl.appendChild(node);
  scrollBottom();
}

function appendAssistantBubble(text, retrieved) {
  const node     = tplAssistant.content.cloneNode(true);
  const msgEl    = node.querySelector(".message");
  const textEl   = node.querySelector(".msg-text");
  const sources  = node.querySelector(".sources");
  const toggle   = node.querySelector(".sources-toggle");
  const labelEl  = node.querySelector(".sources-label");
  const listEl   = node.querySelector(".sources-list");

  textEl.textContent = text;

  if (retrieved && retrieved.length > 0) {
    sources.style.display = "block";
    labelEl.textContent =
      retrieved.length === 1 ? "מקור 1" : `${retrieved.length} מקורות`;

    retrieved.forEach((r) => {
      const li = document.createElement("li");
      li.className = "source-item";

      const filename = r.source ? escapeHtml(r.source) : "לא ידוע";
      li.innerHTML = `
        <div class="source-meta">
          <span class="source-filename">${filename}</span>
        </div>
      `;
      listEl.appendChild(li);
    });

    toggle.addEventListener("click", () => {
      const expanded = toggle.getAttribute("aria-expanded") === "true";
      toggle.setAttribute("aria-expanded", String(!expanded));
      listEl.classList.toggle("open", !expanded);
    });
  }

  messagesEl.appendChild(node);
  scrollBottom();
  return msgEl;
}

function showThinking() {
  const node = tplThinking.content.cloneNode(true);
  messagesEl.appendChild(node);
  scrollBottom();
  return document.getElementById("msg-thinking");
}

function removeThinking(el) {
  if (el && el.parentNode) el.parentNode.removeChild(el);
}

/* ═══════════════════════════════════════════════════════════════════
   History restore
═══════════════════════════════════════════════════════════════════ */

async function loadHistory() {
  try {
    const res  = await fetchWithSession("/history");
    const data = await res.json();
    const msgs = data.messages || [];

    msgs.forEach((msg) => {
      if (msg.role === "user") {
        appendUserBubble(msg.content);
      } else {
        appendAssistantBubble(msg.content, msg.retrieved || []);
      }
    });

    updateMsgCount(msgs.length);
  } catch (_) {
    /* History load failure is non-critical; just start fresh */
  }
}

/* ═══════════════════════════════════════════════════════════════════
   Send message
═══════════════════════════════════════════════════════════════════ */

async function sendMessage() {
  const text = userInput.value.trim();
  if (isBusy || !isEngineReady) return;

  isBusy = true;
  setInputEnabled(false);

  appendUserBubble(text);
  userInput.value = "";
  autoGrow(userInput);

  updateMsgCount(historyCount + 1);

  const thinkingEl = showThinking();

  try {
    const res = await fetchWithSession("/chat", {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify({ message: text }),
    });

    const data = await res.json();
    removeThinking(thinkingEl);

    if (!res.ok) {
      const errMsg = data.error || `שגיאת שרת ${res.status}`;
      appendAssistantBubble(`⚠️ ${errMsg}`, []);
    } else {
      appendAssistantBubble(data.answer || "(אין תשובה)", data.context || []);
      updateMsgCount(historyCount + 1);
    }
  } catch (err) {
    removeThinking(thinkingEl);
    appendAssistantBubble("⚠️ שגיאת רשת. בדקו את החיבור שלכם.", []);
  } finally {
    isBusy = false;
    setInputEnabled(true);
    userInput.focus();
  }
}

/* ═══════════════════════════════════════════════════════════════════
   New Chat
═══════════════════════════════════════════════════════════════════ */

async function newChat() {
  if (isBusy) return;
  try {
    await fetchWithSession("/clear", { method: "POST" });
  } catch (_) { /* best-effort */ }

  /* Clear the message area (keep the welcome card) */
  messagesEl.innerHTML = "";
  if (welcomeCard) {
    welcomeCard.style.display = "block";
    messagesEl.appendChild(welcomeCard);
  }

  updateMsgCount(0);
  userInput.value = "";
  autoGrow(userInput);
  userInput.focus();
}

/* ═══════════════════════════════════════════════════════════════════
   Document upload
   ───────────────────────────────────────────────────────────────────
   Sends one or more files to POST /upload. On success the backend uploads
   them to S3 and starts a Bedrock ingestion job; we engage /status polling
   so the status card shows "processing documents" until ingestion finishes.
   Chat stays available throughout (it queries the existing Knowledge Base).
═══════════════════════════════════════════════════════════════════ */

function setUploadStatus(text, isError) {
  if (!uploadStatus) return;
  if (!text) {
    uploadStatus.style.display = "none";
    uploadStatus.textContent = "";
    return;
  }
  uploadStatus.style.display = "block";
  uploadStatus.textContent = text;
  uploadStatus.classList.toggle("error", !!isError);
}

async function uploadFiles(fileList) {
  if (!fileList || fileList.length === 0) return;

  const form = new FormData();
  for (const file of fileList) {
    form.append("files", file);
  }

  if (btnUpload) btnUpload.disabled = true;
  setUploadStatus(`מעלה ${fileList.length} קבצים…`, false);

  try {
    const res  = await fetchWithSession("/upload", { method: "POST", body: form });
    const data = await res.json();

    if (!res.ok) {
      setUploadStatus(data.error || `ההעלאה נכשלה (${res.status})`, true);
      return;
    }

    const okCount  = (data.uploaded || []).length;
    const errCount = (data.errors || []).length;
    let msg = `הועלו ${okCount} קבצים.`;
    if (errCount > 0) msg += ` ${errCount} נכשלו.`;
    setUploadStatus(msg, errCount > 0);

    /* Bedrock is now ingesting the new documents → poll /status so the
       status card reflects "processing documents" until it completes. */
    if (data.ingesting) {
      setStatusLoading("ingesting");
      if (!statusPollTimer) {
        statusPollTimer = setInterval(pollStatus, 3000);
      }
    }
  } catch (err) {
    setUploadStatus("שגיאת רשת במהלך ההעלאה.", true);
  } finally {
    if (btnUpload) btnUpload.disabled = false;
    if (fileInput) fileInput.value = "";   // allow re-selecting the same file
  }
}

/* ═══════════════════════════════════════════════════════════════════
   UI helpers
═══════════════════════════════════════════════════════════════════ */

function setInputEnabled(on) {
  userInput.disabled = !on;
  btnSend.disabled   = !on;
}

function autoGrow(el) {
  el.style.height = "auto";
  el.style.height = Math.min(el.scrollHeight, 180) + "px";
}

/* ═══════════════════════════════════════════════════════════════════
   Event listeners
═══════════════════════════════════════════════════════════════════ */

userInput.addEventListener("input", () => autoGrow(userInput));

userInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
});

btnSend.addEventListener("click", sendMessage);

btnNewChat.addEventListener("click", newChat);

if (btnUpload && fileInput) {
  btnUpload.addEventListener("click", () => fileInput.click());
  fileInput.addEventListener("change", () => uploadFiles(fileInput.files));
}

btnSidebar.addEventListener("click", () => {
  sidebar.classList.toggle("collapsed");
});

/* Suggestion chips in the welcome card */
document.addEventListener("click", (e) => {
  const chip = e.target.closest(".chip");
  if (!chip) return;
  const q = chip.dataset.q;
  if (q) {
    userInput.value = q;
    autoGrow(userInput);
    sendMessage();
  }
});

/* ═══════════════════════════════════════════════════════════════════
   Initialisation
═══════════════════════════════════════════════════════════════════ */

(async function init() {
  setInputEnabled(false);   // disabled until engine is ready

  /* Restore previous messages first */
  await loadHistory();

  /* Start polling for engine readiness */
  await pollStatus();       // immediate check
  if (!isEngineReady) {
    statusPollTimer = setInterval(pollStatus, 2000);
  }
})();
