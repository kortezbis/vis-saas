"""Own and identify Viszmo's dedicated assignment browser.

The desktop app launches a separate Chromium profile with a private DevTools
endpoint. The rest of the application talks to one CDP target by target id,
so changing focus, minimizing the window, or opening another browser cannot
retarget the task.
"""

from __future__ import annotations

import base64
import json
import logging
import multiprocessing as mp
import os
import socket
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from queue import Empty
from pathlib import Path
from typing import Any

import layout

log = logging.getLogger("chrome_session")

CDP_HOST = "127.0.0.1"
DEFAULT_CDP_PORT = 9222
DEFAULT_START_URL = "https://www.google.com"
# Chrome does not allow CDP-injected DOM on its protected built-in New Tab
# page. Leave that page alone; the new-document script will mount the panel
# after the user navigates the tab to a normal assignment page.
NEW_TAB_FALLBACK_URL = DEFAULT_START_URL
# Kept for compatibility with older imports. New desktop sessions use a free
# local port so another Chromium instance cannot collide with Viszmo.
CDP_PORT = DEFAULT_CDP_PORT


def _data_root() -> Path:
    override = os.getenv("VISZMO_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        base = os.getenv("LOCALAPPDATA", "").strip()
        if base:
            return Path(base) / "Viszmo"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Viszmo"
    return Path(os.getenv("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))) / "Viszmo"


PROFILE_DIR = _data_root() / "BrowserProfile"


@dataclass
class ManagedBrowser:
    executable: Path
    profile_dir: Path
    port: int
    process: subprocess.Popen[Any] | None
    target_id: str
    target_ws_url: str
    title: str = ""
    url: str = ""
    hwnd: int | None = None
    panel_script_id: str = ""
    panel_script_ids: dict[str, str] = field(default_factory=dict)


_managed: ManagedBrowser | None = None
_orphaned_processes: list[subprocess.Popen[Any]] = []
_panel_lock = threading.Lock()
_panel_watcher: threading.Thread | None = None
_panel_watcher_stop: threading.Event | None = None
_panel_clients: dict[str, Any] = {}
_panel_task_lock = threading.Lock()
_panel_task_process: mp.Process | None = None
_panel_task_stop_event: Any | None = None
_panel_task_queue: Any | None = None
_panel_task_pump: threading.Thread | None = None
_panel_task_target_id = ""
_browser_launch_lock = threading.Lock()
_desktop_access_token = ""
_desktop_access_token_lock = threading.Lock()
PANEL_VERSION = "original-v10"
PANEL_STOP_GRACE_SECONDS = 1.5


def set_desktop_access_token(token: str | None) -> None:
    global _desktop_access_token
    with _desktop_access_token_lock:
        _desktop_access_token = str(token or "").strip()


def desktop_access_token() -> str:
    with _desktop_access_token_lock:
        return _desktop_access_token


_PANEL_SCRIPT_TEMPLATE = r"""
(() => {
  // Page.addScriptToEvaluateOnNewDocument also runs in child frames. Viszmo
  // belongs to the top-level assignment page, never to each embedded frame.
  if (window.top !== window) return;
  const hostId = "__viszmo_panel__";
  const panelVersion = __VISZMO_PANEL_VERSION__;
  const panelWidth = 380;
  const panelPositionKey = "viszmoPanelPosition";
  const panelMargin = 8;
  const panelBase = __VISZMO_PANEL_URL__;
  const panelTargetId = __VISZMO_TARGET_ID__;
  const logoData = __VISZMO_LOGO_DATA__;

  function mount() {
    const panels = Array.from(document.querySelectorAll("#" + hostId));
    const previous = panels.shift() || null;
    panels.forEach((panel) => panel.remove());
    if (previous && window.__viszmoPanelVersion === panelVersion) return;
    if (previous) previous.remove();
    if (!document.documentElement || !document.body) {
      document.addEventListener("DOMContentLoaded", mount, { once: true });
      return;
    }
    window.__viszmoPanelVersion = panelVersion;
    window.__viszmoPanelWidth = panelWidth;

    const host = document.createElement("div");
    host.id = hostId;
    host.setAttribute("aria-label", "Viszmo browser panel");
    // Keep the in-page panel compact while preserving the full-height layout
    // on smaller screens.
    host.style.cssText = "position:fixed;top:12px;right:12px;width:" + panelWidth + "px;height:min(680px,calc(100vh - 24px));z-index:2147483647;pointer-events:none;";
    const shadow = host.attachShadow({ mode: "open" });
    shadow.innerHTML = `
      <style>
        @import url('https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@500;600;700;800&display=swap');
        :host { all: initial; }
        .panel, .panel * { box-sizing:border-box; font-family:"Plus Jakarta Sans","Segoe UI",system-ui,sans-serif; }
        .panel { position:relative; width:100%; height:100%; display:flex; flex-direction:column; overflow:hidden; border:1px solid #2C2C2C; border-radius:16px; background:#1A1A1A; color:#E2E8F0; box-shadow:0 18px 46px rgba(0,0,0,.38); pointer-events:auto; }
        .top { padding:12px 14px 8px; display:flex; justify-content:space-between; align-items:center; user-select:none; cursor:grab; touch-action:none; }
        .top.dragging { cursor:grabbing; }
        .top button { cursor:pointer; }
        .brand { display:flex; align-items:center; gap:10px; min-width:0; }
        .brand-logo { height:28px; width:auto; max-width:140px; object-fit:contain; display:block; }
        .brand-fallback { display:none; color:#fff; font-size:16px; font-weight:800; }
        .top-right { display:flex; align-items:center; gap:4px; }
        .icon-btn { width:28px; height:28px; border:none; border-radius:8px; cursor:pointer; background:transparent; color:#94A3B8; padding:0; display:grid; place-items:center; flex-shrink:0; }
        .icon-btn svg { width:15px; height:15px; stroke:currentColor; stroke-width:2; stroke-linecap:round; stroke-linejoin:round; }
        .icon-btn:hover { background:#1E1E1E; color:#E2E8F0; }
        .icon-btn.copied { color:#4ade80; background:rgba(74,222,128,.12); }
        .mode-row { display:flex; gap:6px; padding:0 14px 8px; }
        .mode-btn { flex:1; border:1px solid #2C2C2C; border-radius:10px; padding:8px 10px; background:#1E1E1E; color:#94A3B8; font-size:12px; font-weight:700; cursor:pointer; }
        .mode-btn:hover { color:#E2E8F0; border-color:#3F3F46; }
        .mode-btn.on { background:rgba(14,165,233,.16); color:#E2E8F0; border-color:#0ea5e9; }
        .mode-hint { padding:0 14px 8px; font-size:11px; color:#94A3B8; line-height:1.4; }
        .mode-hint.fast { color:#94A3B8; }
        .logs { flex:1; margin:0 14px 8px; background:#1E1E1E; border:1px solid #2C2C2C; border-radius:12px; padding:10px; overflow-y:auto; min-height:80px; user-select:text; cursor:text; }
        .msg { font-size:12px; line-height:1.45; color:#d5def0; margin:0 0 8px; white-space:pre-wrap; }
        .msg.status { color:#94A3B8; }
        .msg.error { color:#fca5a5; background:rgba(239,68,68,.12); padding:8px; border-radius:8px; }
        .msg.answer { padding:9px 10px; border:1px solid #2C2C2C; border-radius:10px; background:#131314; }
        .answer-title { color:#4ade80; font-size:11px; font-weight:800; margin-bottom:3px; }
        .answer-question { color:#94A3B8; font-size:11px; margin-bottom:5px; }
        .answer-value { color:#E2E8F0; font-size:13px; font-weight:700; }
        .composer { margin:0 14px 8px; background:#1E1E1E; border:1px solid #2C2C2C; border-radius:14px; padding:8px 8px 6px; min-height:118px; display:flex; flex-direction:column; }
        .tab-chip { display:flex; align-items:center; gap:8px; background:#141414; border:1px solid #2C2C2C; border-radius:10px; padding:6px 8px; margin-bottom:6px; cursor:pointer; }
        .tab-chip.off { opacity:.55; }
        .tab-icon { width:18px; height:18px; border-radius:50%; flex-shrink:0; background:#0ea5e9; color:#fff; font-size:10px; font-weight:800; display:grid; place-items:center; }
        .tab-text { flex:1; min-width:0; font-size:11px; color:#CBD5E1; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
        .tab-x { width:18px; height:18px; border:none; border-radius:6px; cursor:pointer; background:transparent; color:#94A3B8; font-size:13px; line-height:1; flex-shrink:0; }
        .tab-x:hover { background:#1E1E1E; color:#E2E8F0; }
        .composer textarea { flex:1; width:100%; min-height:52px; resize:none; border:none; outline:none; background:transparent; color:#E2E8F0; font:13px/1.4 "Plus Jakarta Sans","Segoe UI",sans-serif; padding:4px 6px 0; }
        .composer textarea::placeholder { color:#475569; }
        .composer-bar { display:flex; align-items:center; justify-content:space-between; padding:4px 2px 2px; gap:8px; }
        .composer-hint { font-size:11px; color:#94A3B8; min-width:0; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
        .composer-actions { display:flex; align-items:center; gap:6px; flex-shrink:0; }
        .snip-btn, .run-btn { display:inline-flex; align-items:center; justify-content:center; border:none; border-radius:8px; cursor:pointer; color:#fff; font-weight:700; font-size:13px; flex-shrink:0; }
        .snip-btn { padding:6px 10px; background:#2C2C2C; color:#CBD5E1; }
        .snip-btn:hover { background:#3F3F46; color:#fff; }
        .run-btn { padding:6px 14px; background:#0ea5e9; }
        .run-btn:hover { background:#0284c7; }
        .run-btn.running { background:#dc2626; }
        .spinner { display:inline-block; width:12px; height:12px; border:2px solid rgba(255,255,255,.3); border-radius:50%; border-top-color:#fff; animation:spin .8s linear infinite; margin-right:6px; }
        @keyframes spin { to { transform:rotate(360deg); } }
        .copy-area { position:fixed; left:-10000px; top:-10000px; }
        .bridge-frame { position:absolute; left:-2px; top:-2px; width:1px; height:1px; border:0; opacity:0; pointer-events:none; }
        :host(.collapsed) { width:44px !important; height:44px !important; }
        :host(.collapsed) .panel { border-radius:12px; }
        :host(.collapsed) .top { height:44px; padding:8px; justify-content:center; }
        :host(.collapsed) .brand-logo { display:none; }
        :host(.collapsed) .brand::after { content:"V"; width:28px; height:28px; border-radius:8px; display:grid; place-items:center; background:#0ea5e9; color:#fff; font-size:15px; font-weight:800; }
        :host(.collapsed) .top-right { position:absolute; inset:0; }
        :host(.collapsed) .top-right .icon-btn { width:44px; height:44px; color:transparent; }
        :host(.collapsed) .top-right .icon-btn:hover { background:transparent; }
        :host(.collapsed) .top-right .icon-btn::after { content:"+"; color:#94A3B8; font-size:18px; }
        :host(.collapsed) .top-right .icon-btn svg { display:none; }
        :host(.collapsed) .top-right #copyBtn { display:none; }
        :host(.collapsed) .mode-row, :host(.collapsed) .mode-hint, :host(.collapsed) .logs, :host(.collapsed) .composer { display:none; }
      </style>
      <section class="panel">
        <header class="top">
          <div class="brand"><img id="brandLogo" src="${logoData}" alt="Viszmo" class="brand-logo"><span class="brand-fallback">Viszmo</span></div>
          <div class="top-right">
            <button class="icon-btn" id="copyBtn" type="button" title="Copy log" aria-label="Copy log">
              <svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
            </button>
            <button class="icon-btn" id="hideBtn" type="button" title="Hide panel" aria-label="Hide panel">
              <svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg>
            </button>
          </div>
        </header>
        <div class="mode-row">
          <button class="mode-btn on" id="modeMath" type="button">Math</button>
          <button class="mode-btn" id="modeGeneral" type="button">General</button>
        </div>
        <div class="mode-row">
          <button class="mode-btn" id="modeCopilot" type="button">Copilot</button>
          <button class="mode-btn on" id="modeAutopilot" type="button">Autopilot</button>
          <button class="mode-btn" id="modeDry" type="button">Dry Run</button>
        </div>
        <div class="mode-hint" id="modeHint">STEM: algebra, calc, stats, physics, chemistry, graphs.</div>
        <div class="logs" id="chat"><div class="msg">Viszmo runs inside its assignment browser. Your mouse, keyboard, and other apps stay independent.</div></div>
        <div class="composer" id="composer">
          <div class="tab-chip" id="tabChip"><div class="tab-icon" id="tabIcon">T</div><div class="tab-text" id="tabText">Looking for a tabâ€¦</div><button class="tab-x" id="tabClear" type="button" title="Hide tab" aria-label="Hide connected tab">Ã—</button></div>
          <textarea id="notes" rows="3" placeholder="Add a note for this tabâ€¦"></textarea>
          <div class="composer-bar"><span class="composer-hint" id="composerHint">Connected to this Chrome tab</span><div class="composer-actions"><button class="snip-btn" id="snipBtn" type="button" title="Snip one question" aria-label="Snip one question">Snip</button><button class="run-btn" id="sendBtn" type="button" title="Run" aria-label="Run">Run</button></div></div>
        </div>
      </section>`;
    document.documentElement.appendChild(host);

    const dragHandle = shadow.querySelector(".top");
    const chat = shadow.getElementById("chat");
    const notes = shadow.getElementById("notes");
    const sendBtn = shadow.getElementById("sendBtn");
    const snipBtn = shadow.getElementById("snipBtn");
    const modeHint = shadow.getElementById("modeHint");
    const composerHint = shadow.getElementById("composerHint");
    const tabChip = shadow.getElementById("tabChip");
    const tabText = shadow.getElementById("tabText");
    const tabIcon = shadow.getElementById("tabIcon");
    const copyBtn = shadow.getElementById("copyBtn");
    const hideBtn = shadow.getElementById("hideBtn");
    const logLines = [];
    const answerLines = [];
    let tabTimer = null;
    let running = false;
    let stopping = false;
    let tabHidden = false;
    let lastTabTitle = "";
    let selectedMode = "math";
    let selectedAutonomy = "autopilot";
    let panelDrag = null;
    let snipActive = false;
    window.__viszmoPanelCommands = [];
    window.__viszmoPanelEvents = [];
    try {
      if (localStorage.getItem("viszmoMode") === "general") selectedMode = "general";
      const savedAutonomy = localStorage.getItem("viszmoAutonomy");
      if (savedAutonomy === "copilot" || savedAutonomy === "dry_run") selectedAutonomy = savedAutonomy;
    } catch (_error) {}

    function setPanelPosition(left, topPos) {
      const rect = host.getBoundingClientRect();
      const maxLeft = Math.max(panelMargin, window.innerWidth - rect.width - panelMargin);
      const maxTop = Math.max(panelMargin, window.innerHeight - rect.height - panelMargin);
      const nextLeft = Number(left);
      const nextTop = Number(topPos);
      const clampedLeft = Math.min(Math.max(Number.isFinite(nextLeft) ? nextLeft : panelMargin, panelMargin), maxLeft);
      const clampedTop = Math.min(Math.max(Number.isFinite(nextTop) ? nextTop : panelMargin, panelMargin), maxTop);
      host.style.left = Math.round(clampedLeft) + "px";
      host.style.top = Math.round(clampedTop) + "px";
      host.style.right = "auto";
    }

    function savePanelPosition() {
      const rect = host.getBoundingClientRect();
      try {
        localStorage.setItem(panelPositionKey, JSON.stringify({
          left: Math.round(rect.left),
          top: Math.round(rect.top),
        }));
      } catch (_error) {}
    }

    function restorePanelPosition() {
      try {
        const saved = JSON.parse(localStorage.getItem(panelPositionKey) || "null");
        if (saved && Number.isFinite(Number(saved.left)) && Number.isFinite(Number(saved.top))) {
          setPanelPosition(saved.left, saved.top);
        }
      } catch (_error) {}
    }

    function keepPanelInViewport() {
      if (host.style.right !== "auto") return;
      const rect = host.getBoundingClientRect();
      setPanelPosition(rect.left, rect.top);
    }

    function startPanelDrag(event) {
      const target = event.target;
      if ((event.button !== undefined && event.button !== 0) || (target && target.closest && target.closest("button, a, input, textarea, select"))) return;
      const rect = host.getBoundingClientRect();
      panelDrag = {
        pointerId: event.pointerId,
        startX: event.clientX,
        startY: event.clientY,
        startLeft: rect.left,
        startTop: rect.top,
      };
      dragHandle.classList.add("dragging");
      if (Number.isInteger(event.pointerId)) dragHandle.setPointerCapture(event.pointerId);
      event.preventDefault();
    }

    function movePanelDrag(event) {
      if (!panelDrag || event.pointerId !== panelDrag.pointerId) return;
      setPanelPosition(
        panelDrag.startLeft + event.clientX - panelDrag.startX,
        panelDrag.startTop + event.clientY - panelDrag.startY,
      );
      event.preventDefault();
    }

    function finishPanelDrag(event) {
      if (!panelDrag || (event.pointerId !== undefined && event.pointerId !== panelDrag.pointerId)) return;
      const pointerId = panelDrag.pointerId;
      panelDrag = null;
      dragHandle.classList.remove("dragging");
      if (Number.isInteger(pointerId) && dragHandle.hasPointerCapture(pointerId)) dragHandle.releasePointerCapture(pointerId);
      savePanelPosition();
    }

    const refreshModeHint = () => {
      modeHint.className = "mode-hint" + (selectedMode === "general" ? " fast" : "");
      if (selectedAutonomy === "copilot") {
        modeHint.textContent = "Copilot: collect every answer in chat; you review, enter, and submit them.";
      } else if (selectedAutonomy === "dry_run") {
        modeHint.textContent = selectedMode === "math"
          ? "Dry run: STEM answers filled in, but Submit is never clicked."
          : "Dry run: answers filled fast, but Submit is never clicked.";
      } else {
        modeHint.textContent = selectedMode === "math"
          ? "Autopilot: STEM answers, submission, and navigation."
          : "Autopilot: fast reading, vocab, history, and civics.";
      }
    };

    const setMode = (mode) => {
      selectedMode = mode === "general" ? "general" : "math";
      try { localStorage.setItem("viszmoMode", selectedMode); } catch (_error) {}
      shadow.getElementById("modeMath").className = "mode-btn" + (selectedMode === "math" ? " on" : "");
      shadow.getElementById("modeGeneral").className = "mode-btn" + (selectedMode === "general" ? " on" : "");
      refreshModeHint();
    };

    const setAutonomy = (autonomy) => {
      selectedAutonomy = (autonomy === "copilot" || autonomy === "dry_run") ? autonomy : "autopilot";
      try { localStorage.setItem("viszmoAutonomy", selectedAutonomy); } catch (_error) {}
      shadow.getElementById("modeCopilot").className = "mode-btn" + (selectedAutonomy === "copilot" ? " on" : "");
      shadow.getElementById("modeAutopilot").className = "mode-btn" + (selectedAutonomy === "autopilot" ? " on" : "");
      shadow.getElementById("modeDry").className = "mode-btn" + (selectedAutonomy === "dry_run" ? " on" : "");
      refreshModeHint();
    };

    function setRunning(value) {
      running = Boolean(value);
      notes.disabled = running || stopping;
      snipBtn.disabled = running || stopping;
      if (stopping) {
        sendBtn.innerHTML = '<span class="spinner"></span>Stopping';
        sendBtn.classList.add("running");
        sendBtn.title = "Stopping";
        sendBtn.disabled = true;
      } else if (running) {
        sendBtn.innerHTML = '<span class="spinner"></span>Stop';
        sendBtn.classList.add("running");
        sendBtn.title = "Stop";
        sendBtn.disabled = false;
      } else {
        sendBtn.textContent = "Run";
        sendBtn.classList.remove("running");
        sendBtn.title = "Run";
        sendBtn.disabled = false;
      }
    }

    function appendMessage(text, kind) {
      if (!text) return;
      const line = document.createElement("div");
      line.className = "msg" + (kind === "error" ? " error" : kind === "status" ? " status" : "");
      line.textContent = text;
      chat.appendChild(line);
      chat.scrollTop = chat.scrollHeight;
      logLines.push((kind === "error" ? "ERROR: " : "") + text);
    }

    function appendAnswer(message) {
      const card = document.createElement("div");
      card.className = "msg answer";
      const title = document.createElement("div");
      title.className = "answer-title";
      title.textContent = "Question " + (message.number || answerLines.length + 1);
      const question = document.createElement("div");
      question.className = "answer-question";
      question.textContent = message.question || "Selected question";
      const value = document.createElement("div");
      value.className = "answer-value";
      value.textContent = "Answer: " + (message.answer || "No answer identified");
      card.append(title, question, value);
      chat.appendChild(card);
      chat.scrollTop = chat.scrollHeight;
      answerLines.push(title.textContent + "\n" + question.textContent + "\n" + value.textContent);
    }

    function copyLog() {
      const text = (selectedAutonomy === "copilot" || snipActive) && answerLines.length
        ? answerLines.join("\n\n")
        : logLines.join("\n");
      const done = () => {
        copyBtn.classList.add("copied");
        copyBtn.title = "Copied!";
        setTimeout(() => { copyBtn.classList.remove("copied"); copyBtn.title = selectedAutonomy === "copilot" || snipActive ? "Copy answers" : "Copy log"; }, 1200);
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done).catch(() => fallbackCopy(text, done));
      } else fallbackCopy(text, done);
    }

    function fallbackCopy(text, done) {
      const area = document.createElement("textarea");
      area.className = "copy-area";
      area.value = text;
      shadow.appendChild(area);
      area.select();
      try { document.execCommand("copy"); done(); } catch (_error) {}
      area.remove();
    }

    function handleControllerMessage(raw) {
      let message = raw;
      if (typeof raw === "string") {
        try { message = JSON.parse(raw); } catch (_error) { return; }
      }
      if (!message || typeof message !== "object") return;
      if (message.type === "copilot_answer") {
        appendAnswer(message);
      } else if (message.type === "copilot_complete") {
        stopping = false;
        snipActive = false;
        setRunning(false);
        appendMessage("âœ” " + (message.text || "Answers ready."), "status");
      } else if (message.type === "status" || message.type === "progress") {
        if (message.type === "progress" && !stopping) setRunning(true);
        if (selectedAutonomy !== "copilot" && !snipActive) appendMessage(message.text || "", "status");
      } else if (message.type === "thought" || message.type === "log") {
        if (selectedAutonomy !== "copilot" && !snipActive) appendMessage(message.text || "", "agent");
      } else if (message.type === "usage" || message.type === "usage_summary") {
        if (selectedAutonomy !== "copilot" && !snipActive) appendMessage(message.text || "", "status");
      } else if (message.type === "done") {
        stopping = false;
        setRunning(false);
        appendMessage("âœ” " + (message.text || "Finished"), "status");
      } else if (message.type === "paywall") {
        stopping = false;
        snipActive = false;
        setRunning(false);
        appendMessage(message.text || "Your free desktop questions are used. Open Viszmo Plans to subscribe.", "error");
      } else if (message.type === "aborted") {
        appendMessage(message.text || "Stoppingâ€¦", stopping ? "status" : "error");
        if (!stopping) setRunning(false);
      } else if (message.type === "stopped") {
        stopping = false;
        setRunning(false);
        appendMessage(message.text || "Stopped", "status");
      } else if (message.type === "copilot") {
        stopping = false;
        setRunning(false);
        appendMessage(message.text || "Copilot entered the answer and paused for your review.", "status");
      } else if (message.type === "error") {
        stopping = false;
        snipActive = false;
        setRunning(false);
        appendMessage(message.text || "The task failed.", "error");
      }
    }

    function send(message) {
      const commands = Array.isArray(window.__viszmoPanelCommands)
        ? window.__viszmoPanelCommands
        : (window.__viszmoPanelCommands = []);
      commands.push({ ...message, target_id: panelTargetId });
      return true;
    }

    function connect() {
      appendMessage("Connected to this Chrome tab.", "status");
    }

    function launch() {
      if (stopping) return;
      if (running) {
        abortTask();
        return;
      }
      setRunning(true);
      const label = selectedMode === "math" ? "Math" : "General";
      const behavior = selectedAutonomy === "copilot" ? "Copilot" : (selectedAutonomy === "dry_run" ? "Dry run" : "Autopilot");
      if (selectedAutonomy === "copilot") {
        chat.innerHTML = "";
        logLines.length = 0;
        answerLines.length = 0;
        appendMessage("Copilot is collecting answersâ€¦", "status");
      } else {
        appendMessage("Launching (" + label + " / " + behavior + ")â€¦", "status");
      }
      send({ type: "launch", mode: selectedMode, autonomy: selectedAutonomy, target_id: panelTargetId, sidebar_width: panelWidth, notes: (notes.value || "").trim() });
    }

    function startSnip() {
      if (running || stopping || snipActive) return;
      const overlay = document.createElement("div");
      const selection = document.createElement("div");
      overlay.style.cssText = "position:fixed;inset:0;z-index:2147483646;cursor:crosshair;background:rgba(7,10,18,.20);";
      selection.style.cssText = "position:fixed;border:2px solid #0ea5e9;background:rgba(29,127,255,.16);pointer-events:none;";
      overlay.appendChild(selection);
      document.documentElement.appendChild(overlay);
      host.style.visibility = "hidden";
      let start = null;
      let cleaned = false;
      const cleanup = () => {
        if (cleaned) return;
        cleaned = true;
        document.removeEventListener("keydown", cancel, true);
        overlay.remove();
        host.style.visibility = "visible";
      };
      const cancel = (event) => {
        if (event.key === "Escape") cleanup();
      };
      const boxFor = (event) => ({
        x: Math.min(start.x, event.clientX), y: Math.min(start.y, event.clientY),
        width: Math.abs(event.clientX - start.x), height: Math.abs(event.clientY - start.y),
      });
      document.addEventListener("keydown", cancel, true);
      overlay.addEventListener("pointerdown", (event) => {
        start = { x: event.clientX, y: event.clientY };
        overlay.setPointerCapture(event.pointerId);
      });
      overlay.addEventListener("pointermove", (event) => {
        if (!start) return;
        const box = boxFor(event);
        selection.style.left = box.x + "px";
        selection.style.top = box.y + "px";
        selection.style.width = box.width + "px";
        selection.style.height = box.height + "px";
      });
      overlay.addEventListener("pointerup", (event) => {
        if (!start) { cleanup(); return; }
        const crop = boxFor(event);
        cleanup();
        if (crop.width < 24 || crop.height < 24) {
          appendMessage("Snip cancelled â€” drag around one full question.", "error");
          return;
        }
        snipActive = true;
        chat.innerHTML = "";
        logLines.length = 0;
        answerLines.length = 0;
        appendMessage("Reading selected questionâ€¦", "status");
        setRunning(true);
        send({ type: "snip", mode: selectedMode, crop, notes: (notes.value || "").trim() });
      });
      overlay.addEventListener("pointercancel", cleanup);
    }

    function requestStop() {
      if (stopping) return;
      stopping = true;
      send({ type: "abort" });
      setRunning(false);
      setTimeout(() => {
        if (!stopping) return;
        stopping = false;
        setRunning(false);
        appendMessage("Stop is taking longer than expected; the next Run will retry cleanup.", "error");
      }, 6000);
    }

    function abortTask() {
      requestStop();
    }

    function hideSidebar() {
      abortTask();
      const wasPositioned = host.style.right === "auto";
      host.classList.toggle("collapsed");
      if (wasPositioned) {
        keepPanelInViewport();
        savePanelPosition();
      }
      hideBtn.title = host.classList.contains("collapsed") ? "Show panel" : "Hide panel";
    }

    function tabLetter(title) {
      const clean = (title || "").replace(/^(learn|quiz|study|watch):\s*/i, "").trim();
      return (clean.charAt(0) || "T").toUpperCase();
    }

    function showTab(data) {
      if (tabHidden) {
        tabChip.className = "tab-chip off";
        tabText.textContent = "Tab hidden â€” click to reconnect";
        tabIcon.textContent = "+";
        composerHint.textContent = "Reconnect to send this page";
        return;
      }
      const connected = Boolean(data && data.connected);
      const title = (data && (data.label || data.title)) || "";
      tabChip.className = "tab-chip";
      if (connected && title) {
        lastTabTitle = title;
        const thisTab = !data.target_id || data.target_id === panelTargetId;
        tabText.textContent = (thisTab ? "This tab: \"" : "Chrome tab: \"") + title + "\"";
        tabIcon.textContent = tabLetter(title);
        composerHint.textContent = thisTab ? "Notes go with this tab" : "Press Run to use this tab";
      } else {
        tabText.textContent = "Open an assignment tab in Chrome";
        tabIcon.textContent = "?";
        composerHint.textContent = "Viszmo will connect to this page";
      }
    }

    function clearTab(event) {
      if (event) event.stopPropagation();
      tabHidden = !tabHidden;
      showTab({ connected: Boolean(lastTabTitle), title: lastTabTitle, label: lastTabTitle });
    }

    function drainControllerEvents() {
      const events = Array.isArray(window.__viszmoPanelEvents)
        ? window.__viszmoPanelEvents.splice(0)
        : [];
      events.forEach(handleControllerMessage);
    }

    function pollTab() {
      if (tabHidden) { showTab({}); return; }
      showTab({
        connected: true,
        title: document.title || location.hostname || "Chrome tab",
        label: document.title || location.href,
        target_id: panelTargetId,
      });
    }

    shadow.getElementById("modeMath").addEventListener("click", () => setMode("math"));
    shadow.getElementById("modeGeneral").addEventListener("click", () => setMode("general"));
    shadow.getElementById("modeCopilot").addEventListener("click", () => setAutonomy("copilot"));
    shadow.getElementById("modeAutopilot").addEventListener("click", () => setAutonomy("autopilot"));
    shadow.getElementById("modeDry").addEventListener("click", () => setAutonomy("dry_run"));
    sendBtn.addEventListener("click", launch);
    snipBtn.addEventListener("click", startSnip);
    copyBtn.addEventListener("click", copyLog);
    hideBtn.addEventListener("click", hideSidebar);
    shadow.getElementById("tabClear").addEventListener("click", clearTab);
    dragHandle.addEventListener("pointerdown", startPanelDrag);
    dragHandle.addEventListener("pointermove", movePanelDrag);
    dragHandle.addEventListener("pointerup", finishPanelDrag);
    dragHandle.addEventListener("pointercancel", finishPanelDrag);
    dragHandle.addEventListener("lostpointercapture", finishPanelDrag);
    window.addEventListener("resize", keepPanelInViewport);
    tabChip.addEventListener("click", (event) => {
      if (event.target && event.target.id === "tabClear") return;
      if (tabHidden) { tabHidden = false; pollTab(); }
    });
    notes.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        launch();
      }
    });
    shadow.addEventListener("keydown", (event) => {
      if (event.key === "Escape") abortTask();
    });
    const logo = shadow.getElementById("brandLogo");
    logo.addEventListener("error", () => { logo.style.display = "none"; shadow.querySelector(".brand-fallback").style.display = "inline"; });
    restorePanelPosition();
    pollTab();
    tabTimer = setInterval(() => { drainControllerEvents(); pollTab(); }, 350);
    setMode(selectedMode);
    setAutonomy(selectedAutonomy);
    setTimeout(connect, 0);
  }

  function boot() {
    if (document.readyState === "loading" || !document.body) {
      setTimeout(boot, 50);
      return;
    }
    mount();
  }
  boot();
})();
"""


def cdp_tabs(host: str = CDP_HOST, port: int = DEFAULT_CDP_PORT) -> list[dict[str, Any]]:
    """Return pages exposed by a local Chrome/Edge DevTools endpoint."""
    for path in ("/json/list", "/json"):
        try:
            with urllib.request.urlopen(f"http://{host}:{port}{path}", timeout=1.5) as response:
                payload = json.loads(response.read())
            if isinstance(payload, list):
                return [item for item in payload if isinstance(item, dict)]
        except (OSError, ValueError, urllib.error.URLError):
            continue
    return []


def cdp_available(host: str = CDP_HOST, port: int = DEFAULT_CDP_PORT) -> bool:
    return bool(cdp_tabs(host, port))


def _free_port() -> int:
    """Reserve a currently unused loopback port for the managed browser."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((CDP_HOST, 0))
        return int(sock.getsockname()[1])


def _browser_candidates(browser: str = "auto") -> list[Path]:
    env = os.environ
    if sys.platform == "darwin":
        chrome = [
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        ]
        edge = [
            Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
            Path.home() / "Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        ]
        brave = [
            Path("/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"),
            Path.home() / "Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        ]
    elif sys.platform == "win32":
        chrome = [
            Path(env.get("PROGRAMFILES", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
            Path(env.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
            Path(env.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
        ]
        edge = [
            Path(env.get("PROGRAMFILES", r"C:\Program Files")) / "Microsoft/Edge/Application/msedge.exe",
            Path(env.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
            Path(env.get("LOCALAPPDATA", "")) / "Microsoft/Edge/Application/msedge.exe",
        ]
        brave = [
            Path(env.get("PROGRAMFILES", r"C:\Program Files")) / "BraveSoftware/Brave-Browser/Application/brave.exe",
            Path(env.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "BraveSoftware/Brave-Browser/Application/brave.exe",
            Path(env.get("LOCALAPPDATA", "")) / "BraveSoftware/Brave-Browser/Application/brave.exe",
        ]
    else:
        chrome = [Path("/usr/bin/google-chrome"), Path("/usr/bin/chromium"), Path("/usr/bin/chromium-browser")]
        edge = [Path("/usr/bin/microsoft-edge"), Path("/usr/bin/microsoft-edge-stable")]
        brave = [Path("/usr/bin/brave-browser"), Path("/usr/bin/brave")]
        chrome.extend(Path(found) for found in (shutil.which("google-chrome"), shutil.which("chromium")) if found)
        edge.extend(Path(found) for found in (shutil.which("microsoft-edge"),) if found)
        brave.extend(Path(found) for found in (shutil.which("brave-browser"),) if found)
    normalized = (browser or "auto").strip().lower()
    if normalized in {"chrome", "google", "google chrome"}:
        return chrome
    if normalized in {"edge", "microsoft edge", "msedge"}:
        return edge
    if normalized in {"brave", "brave browser"}:
        return brave
    return [*chrome, *edge, *brave]


def find_browser_exe(browser: str = "auto") -> Path | None:
    """Find the requested installed Chromium browser executable."""
    for path in _browser_candidates(browser):
        if path and path.is_file():
            return path
    return None


def _page_targets(tabs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        tab
        for tab in tabs
        if tab.get("type") == "page" and tab.get("webSocketDebuggerUrl")
    ]


def _target_matches_url(tab: dict[str, Any], requested_url: str) -> bool:
    if not requested_url or requested_url == "about:blank":
        return False
    current = str(tab.get("url") or "").strip()
    return current == requested_url or requested_url in current


def _choose_target(
    tabs: list[dict[str, Any]],
    before_ids: set[str],
    requested_url: str,
) -> dict[str, Any] | None:
    pages = _page_targets(tabs)
    new_pages = [tab for tab in pages if str(tab.get("id") or "") not in before_ids]
    if new_pages:
        matching = [tab for tab in new_pages if _target_matches_url(tab, requested_url)]
        return (matching or new_pages)[0]
    matching = [tab for tab in pages if _target_matches_url(tab, requested_url)]
    return (matching or pages)[0] if pages else None


def _wait_for_target(
    port: int,
    before_ids: set[str],
    requested_url: str,
    timeout_s: float = 12.0,
) -> dict[str, Any] | None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        target = _choose_target(cdp_tabs(port=port), before_ids, requested_url)
        if target is not None:
            return target
        time.sleep(0.2)
    return None


def _window_for_target(target: dict[str, Any]) -> Any | None:
    """Best-effort native window lookup for the sidebar's status display."""
    target_title = str(target.get("title") or "").strip().lower()
    if not target_title:
        return None
    matches = []
    for win in layout._windows():
        title = layout.page_title(layout._title(win)).strip().lower()
        if title and (title in target_title or target_title in title):
            matches.append(win)
    if not matches:
        return None
    matches.sort(
        key=lambda item: int(getattr(item, "width", 0) or 0) * int(getattr(item, "height", 0) or 0),
        reverse=True,
    )
    return matches[0]


def _refresh_managed() -> dict[str, Any] | None:
    global _managed
    if _managed is None:
        return None
    target_id = _managed.target_id
    target = next(
        (item for item in cdp_tabs(port=_managed.port) if str(item.get("id") or "") == target_id),
        None,
    )
    if target is None or not target.get("webSocketDebuggerUrl"):
        replacement = next(iter(_page_targets(cdp_tabs(port=_managed.port))), None)
        if replacement is None:
            log.warning("Managed assignment tab is no longer available.")
            if _managed.process is not None:
                _remember_orphaned_process(_managed.process)
            _managed = None
            layout.clear_pin()
            return None
        _managed.target_id = str(replacement.get("id") or "")
        _managed.target_ws_url = str(replacement.get("webSocketDebuggerUrl") or "")
        _managed.title = str(replacement.get("title") or "")
        _managed.url = str(replacement.get("url") or "")
        _managed.panel_script_id = _managed.panel_script_ids.get(_managed.target_id, "")
        target = replacement
        log.info("Switched to the remaining Chrome tab %s", _managed.target_id)
    _managed.target_ws_url = str(target["webSocketDebuggerUrl"])
    _managed.title = str(target.get("title") or "")
    _managed.url = str(target.get("url") or "")
    return target


def _remember_orphaned_process(process: subprocess.Popen[Any] | None) -> None:
    """Keep a browser process handle after its last tab disappears.

    Closing Chrome's window first can make the CDP target disappear before the
    dashboard's Close button is pressed. Retaining the handle lets that button
    still terminate the managed process instead of reporting a false clean
    shutdown.
    """
    if process is None:
        return
    if all(existing is not process for existing in _orphaned_processes):
        _orphaned_processes.append(process)


def has_closeable_session() -> bool:
    """Whether Close Chrome should remain available for cleanup."""
    return bool(
        (_managed is not None and _managed.process is not None)
        or _orphaned_processes
    )


def _terminate_process_tree(process: subprocess.Popen[Any]) -> bool:
    """Terminate the managed browser and its children, even after window close."""
    pid = getattr(process, "pid", None)
    if not pid:
        return False

    if sys.platform == "win32":
        try:
            result = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
            if result.returncode == 0:
                return True
        except (OSError, subprocess.SubprocessError) as exc:
            log.debug("taskkill could not terminate browser PID %s: %s", pid, exc)

    try:
        if process.poll() is None:
            if sys.platform != "win32":
                try:
                    os.killpg(os.getpgid(pid), signal.SIGTERM)
                except (OSError, ProcessLookupError):
                    process.terminate()
            else:
                process.terminate()
    except (OSError, ProcessLookupError) as exc:
        log.debug("Browser process %s was already gone: %s", pid, exc)

    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
            process.wait(timeout=2)
        except (OSError, ProcessLookupError, subprocess.TimeoutExpired):
            return False
    return process.poll() is not None


def _panel_url() -> str:
    explicit = os.getenv("VISZMO_PANEL_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    host = os.getenv("VISZMO_HOST", CDP_HOST).strip() or CDP_HOST
    try:
        port = int(os.getenv("VISZMO_PORT", "8000"))
    except ValueError:
        port = 8000
    return f"http://{host}:{port}"


def _panel_logo_data_url() -> str:
    logo_path = Path(__file__).resolve().parent / "assets" / "viszmofull.png"
    try:
        encoded = base64.b64encode(logo_path.read_bytes()).decode("ascii")
    except (OSError, ValueError):
        return ""
    return f"data:image/png;base64,{encoded}"


def _panel_source(panel_url: str | None = None, target_id: str = "") -> str:
    value = panel_url or _panel_url()
    return (
        _PANEL_SCRIPT_TEMPLATE
        .replace("__VISZMO_PANEL_URL__", json.dumps(value.rstrip("/")))
        .replace("__VISZMO_TARGET_ID__", json.dumps(str(target_id or "")))
        .replace("__VISZMO_PANEL_VERSION__", json.dumps(PANEL_VERSION))
        .replace("__VISZMO_LOGO_DATA__", json.dumps(_panel_logo_data_url()))
    )


def _redirect_builtin_new_tab(target: dict[str, Any]) -> bool:
    """Skip protected New Tab pages without redirecting or opening anything."""
    url = str(target.get("url") or "").strip().lower()
    if url not in {"chrome://newtab/", "chrome://new-tab-page/"}:
        return True
    log.debug("Leaving protected Chrome New Tab untouched until it navigates.")
    return False


def _install_panel_on_target(
    target: dict[str, Any],
    source: str,
    script_ids: dict[str, str],
    required: bool,
) -> bool:
    target_id = str(target.get("id") or "")
    ws_url = str(target.get("webSocketDebuggerUrl") or "")
    if not target_id or not ws_url:
        return False
    client = _panel_clients.get(target_id)
    script_id = script_ids.get(target_id, "") if client is not None else ""
    deadline = time.time() + 8
    try:
        from virtual_mouse import CdpMouse

        while True:
            if client is None:
                client = CdpMouse(ws_url)
            try:
                client._send("Page.enable", {})
                if not script_id:
                    response = client._send(
                        "Page.addScriptToEvaluateOnNewDocument",
                        {"source": source},
                    ) or {}
                    script_id = str(response.get("result", {}).get("identifier") or "")
                panel_check = client._send(
                    "Runtime.evaluate",
                    {
                        "expression": (
                            "(() => {"
                            "const panel = document.getElementById('__viszmo_panel__');"
                            f"return Boolean(panel && window.__viszmoPanelVersion === {json.dumps(PANEL_VERSION)});"
                            "})()"
                        ),
                        "returnByValue": True,
                    },
                ) or {}
                has_panel = bool(panel_check.get("result", {}).get("result", {}).get("value"))
                if not has_panel:
                    client._send("Runtime.evaluate", {"expression": source, "returnByValue": False})
                script_ids[target_id] = script_id or "installed"
                _panel_clients[target_id] = client
                client = None
                return True
            except RuntimeError as exc:
                if _panel_clients.get(target_id) is client:
                    _panel_clients.pop(target_id, None)
                try:
                    client.close()
                except Exception:
                    pass
                client = None
                script_id = ""
                if time.time() >= deadline:
                    raise
                if "execution context" not in str(exc).lower() and "runtime.evaluate" not in str(exc).lower():
                    raise
                time.sleep(0.2)
    except Exception as exc:
        _panel_clients.pop(target_id, None)
        if required:
            raise
        log.debug("Could not install the Viszmo panel on Chrome tab %s: %s", target_id, exc)
        return False
    finally:
        if client is not None:
            client.close()


def _start_panel_watcher() -> None:
    global _panel_watcher, _panel_watcher_stop
    if _panel_watcher is not None and _panel_watcher.is_alive():
        return
    stop = threading.Event()
    _panel_watcher_stop = stop

    def watch() -> None:
        while not stop.wait(0.75):
            if _managed is None:
                return
            try:
                install_browser_panel()
            except Exception as exc:
                log.debug("Browser panel watcher retry: %s", exc)

    _panel_watcher = threading.Thread(target=watch, name="viszmo-panel-watcher", daemon=True)
    _panel_watcher.start()


def _stop_panel_watcher() -> None:
    global _panel_watcher, _panel_watcher_stop, _panel_clients
    stop = _panel_watcher_stop
    _panel_watcher_stop = None
    _panel_watcher = None
    if stop is not None:
        stop.set()
    for client in list(_panel_clients.values()):
        try:
            client.close()
        except Exception:
            pass
    _panel_clients = {}
    _abort_panel_task()


def _send_panel_event(target_id: str, event: dict[str, Any]) -> None:
    """Push a controller event into the panel without page networking."""
    payload = json.dumps(event, separators=(",", ":"))
    expression = (
        "(() => {"
        "const events = window.__viszmoPanelEvents || (window.__viszmoPanelEvents = []);"
        f"events.push({payload});"
        "if (events.length > 120) events.splice(0, events.length - 120);"
        "})()"
    )
    with _panel_lock:
        client = _panel_clients.get(str(target_id or ""))
        if client is None:
            return
        try:
            client._send("Runtime.evaluate", {"expression": expression, "returnByValue": False})
        except Exception as exc:
            log.debug("Could not send an event to the Viszmo panel on %s: %s", target_id, exc)


def _dismiss_target_popup(target_id: str) -> bool:
    """Dismiss a blocking dialog before the task worker is stopped.

    The worker may be inside a long model request when the user presses Stop,
    so its CDP connection cannot be relied on to clean up the page first. Use
    a short-lived connection for JavaScript dialogs, then send Escape through
    the panel connection for ordinary site modals/dropdowns.
    """
    normalized_id = str(target_id or "")
    if not normalized_id:
        return False

    dismissed = False
    managed = _managed
    ws_url = (
        str(managed.target_ws_url or "")
        if managed is not None and str(managed.target_id or "") == normalized_id
        else ""
    )
    if ws_url:
        try:
            from virtual_mouse import CdpMouse

            dialog_client = CdpMouse(ws_url)
            try:
                dialog_client._send("Page.handleJavaScriptDialog", {"accept": False})
                dismissed = True
            finally:
                dialog_client.close()
        except Exception as exc:
            # No JavaScript dialog is a normal result. The temporary client
            # may close itself after Chrome reports that there is no dialog.
            log.debug("No JavaScript dialog to dismiss on %s: %s", normalized_id, exc)

    with _panel_lock:
        client = _panel_clients.get(normalized_id)
    if client is None:
        return dismissed

    try:
        escape_payload = {
            "key": "Escape",
            "code": "Escape",
            "windowsVirtualKeyCode": 27,
            "nativeVirtualKeyCode": 27,
            "modifiers": 0,
        }
        client._send("Input.dispatchKeyEvent", {"type": "keyDown", **escape_payload})
        client._send("Input.dispatchKeyEvent", {"type": "keyUp", **escape_payload})
        return True
    except Exception as exc:
        log.debug("Could not send Escape to the assignment tab %s: %s", normalized_id, exc)
        return dismissed


def _panel_worker_entry(
    target_ws_url: str,
    mode: str,
    autonomy: str,
    notes: str,
    snip_box: tuple[float, float, float, float] | None,
    usage_token: str,
    stop_event: Any,
    event_queue: Any,
) -> None:
    """Run one embedded-panel task in a killable worker process."""

    def emit(event: dict[str, Any]) -> None:
        try:
            event_queue.put(event)
        except (BrokenPipeError, EOFError, OSError):
            pass

    try:
        import agent_engine
        import virtual_mouse

        virtual_mouse.configure_cdp_target(target_ws_url)
        label = "Math" if mode == "math" else "General"
        emit({"type": "status", "text": f"Evaluating workspace ({label})..."})
        status = agent_engine.run_oneshot(
            goal=notes or agent_engine.goal_for(mode),
            sidebar_width=380,
            mode=mode,
            autonomy=autonomy,
            snip_box=snip_box,
            usage_token=usage_token,
            should_abort=stop_event.is_set,
            on_event=emit,
        )
        emit({"type": "_worker_finished", "status": status})
    except BaseException as exc:
        emit({"type": "error", "text": str(exc)})
        emit({"type": "_worker_finished", "status": "error"})
    finally:
        try:
            import virtual_mouse

            virtual_mouse.clear_cdp_target()
        except (ImportError, RuntimeError):
            pass
        try:
            event_queue.cancel_join_thread()
        except (AttributeError, OSError):
            pass


_live_status: dict[str, Any] = {
    "state": "idle",
    "question": "",
    "text": "",
    "updated": 0.0,
}
_live_status_lock = threading.Lock()

_QUESTION_RE = None


def _publish_live_status(event: dict[str, Any]) -> None:
    """Maintain a tiny snapshot of task progress for desktop widgets."""
    global _QUESTION_RE
    try:
        import re

        if _QUESTION_RE is None:
            _QUESTION_RE = re.compile(r"(?:^|[^a-zA-Z])(?:q|question)\s*(\d{1,3})(?:[^0-9]|$)", re.IGNORECASE)
        event_type = str(event.get("type") or "")
        text = str(event.get("text") or "")
        with _live_status_lock:
            if event_type in {"done", "error", "aborted", "stopped", "copilot_complete", "copilot"}:
                _live_status["state"] = "finished" if event_type in {"done", "copilot_complete"} else "stopped"
                _live_status["text"] = text[:160]
            elif event_type in {"progress", "status", "log", "thought"}:
                _live_status["state"] = "running"
                match = _QUESTION_RE.search(text)
                if match:
                    _live_status["question"] = f"Q{match.group(1)}"
                if text:
                    _live_status["text"] = text[:160]
            _live_status["updated"] = time.time()
    except Exception:
        pass


def _managed_browser_alive() -> bool:
    """True when the managed assignment browser still answers its debug port."""
    managed = _managed
    if managed is None:
        return False
    try:
        with urllib.request.urlopen(
            f"http://{CDP_HOST}:{managed.port}/json/version",
            timeout=1,
        ) as response:
            return getattr(response, "status", 200) == 200
    except Exception:
        return False


def get_live_status() -> dict[str, Any]:
    with _live_status_lock:
        snapshot = dict(_live_status)
    snapshot["attached"] = _managed_browser_alive()
    return snapshot


def _panel_task_event_pump(process: mp.Process, event_queue: Any, target_id: str) -> None:
    """Forward child-process events to the panel without running agent code here."""
    finished = False
    while True:
        try:
            event = event_queue.get(timeout=0.1)
        except Empty:
            if finished and not process.is_alive():
                return
            if not process.is_alive():
                try:
                    event = event_queue.get(timeout=0.25)
                except Empty:
                    return
                except (EOFError, OSError, ValueError):
                    return
            else:
                continue
        except (EOFError, OSError, ValueError):
            return

        if not isinstance(event, dict):
            continue
        if event.get("type") == "_worker_finished":
            finished = True
            continue
        _send_panel_event(target_id, event)
        _publish_live_status(event)
        try:
            import notifications

            notifications.notify_task_event(str(event.get("type") or ""), str(event.get("text") or ""))
        except Exception:
            pass


def _close_panel_task_resources(
    process: mp.Process | None,
    event_queue: Any | None,
    event_pump: threading.Thread | None,
) -> None:
    if event_pump is not None and event_pump is not threading.current_thread():
        event_pump.join(timeout=1.0)
    pump_done = event_pump is None or not event_pump.is_alive()
    if event_queue is not None and pump_done:
        try:
            event_queue.close()
            event_queue.cancel_join_thread()
        except (AttributeError, OSError, ValueError):
            pass
    if process is not None:
        try:
            process.close()
        except (ValueError, OSError):
            pass


def _discard_finished_panel_task() -> None:
    global _panel_task_process, _panel_task_stop_event, _panel_task_queue, _panel_task_pump, _panel_task_target_id
    with _panel_task_lock:
        process = _panel_task_process
        if process is None or process.is_alive():
            return
        event_queue = _panel_task_queue
        event_pump = _panel_task_pump
        _panel_task_process = None
        _panel_task_stop_event = None
        _panel_task_queue = None
        _panel_task_pump = None
        _panel_task_target_id = ""
    _close_panel_task_resources(process, event_queue, event_pump)


def _start_panel_task(
    target_id: str,
    mode: str,
    autonomy: str,
    notes: str,
    snip_box: tuple[float, float, float, float] | None = None,
) -> bool:
    """Run a panel-launched assignment in a killable worker process."""
    global _panel_task_process, _panel_task_stop_event, _panel_task_queue, _panel_task_pump, _panel_task_target_id
    _discard_finished_panel_task()
    target = assigned_target()
    target_ws_url = str((target or {}).get("webSocketDebuggerUrl") or "")
    if not target_ws_url:
        raise RuntimeError("The assignment browser tab is no longer connected.")
    usage_token = desktop_access_token()
    if not usage_token:
        raise RuntimeError("Sign in to Viszmo before answering questions.")

    context = mp.get_context("spawn")
    stop_event = context.Event()
    event_queue = context.Queue()
    process = context.Process(
        target=_panel_worker_entry,
        args=(target_ws_url, mode, autonomy, notes, snip_box, usage_token, stop_event, event_queue),
        name="viszmo-panel-worker",
    )
    with _panel_task_lock:
        if _panel_task_process is not None and _panel_task_process.is_alive():
            event_queue.close()
            event_queue.cancel_join_thread()
            return False
        try:
            process.start()
        except Exception:
            event_queue.close()
            event_queue.cancel_join_thread()
            raise
        event_pump = threading.Thread(
            target=_panel_task_event_pump,
            args=(process, event_queue, str(target_id or "")),
            name="viszmo-panel-events",
            daemon=True,
        )
        _panel_task_process = process
        _panel_task_stop_event = stop_event
        _panel_task_queue = event_queue
        _panel_task_pump = event_pump
        _panel_task_target_id = str(target_id or "")
        event_pump.start()
    return True


def _abort_panel_task(target_id: str = "") -> bool:
    global _panel_task_process, _panel_task_stop_event, _panel_task_queue, _panel_task_pump, _panel_task_target_id
    with _panel_task_lock:
        process = _panel_task_process
        active_target = _panel_task_target_id or str(target_id or "")
        if process is None:
            if active_target:
                _send_panel_event(active_target, {"type": "stopped", "text": "Stopped by user."})
            return True
        if not process.is_alive():
            # The worker may have finished between the command poll and this
            # call. Clear its resources and acknowledge Stop instead of
            # leaving the panel believing a task is still active.
            pass
        stop_event = _panel_task_stop_event
        event_queue = _panel_task_queue
        event_pump = _panel_task_pump
    if not process.is_alive():
        _discard_finished_panel_task()
        if active_target:
            _send_panel_event(active_target, {"type": "stopped", "text": "Stopped by user."})
        return True

    with _panel_task_lock:
        if _panel_task_process is not process:
            if active_target:
                _send_panel_event(active_target, {"type": "stopped", "text": "Stopped by user."})
            return True
        stop_event = _panel_task_stop_event
        if stop_event is not None:
            stop_event.set()
        _dismiss_target_popup(active_target)

    deadline = time.monotonic() + PANEL_STOP_GRACE_SECONDS
    while process.is_alive() and time.monotonic() < deadline:
        time.sleep(0.05)
    if process.is_alive():
        log.warning("Force-terminating the Viszmo panel worker process %s.", process.pid)
        process.terminate()
        process.join(timeout=2)
    if process.is_alive() and hasattr(process, "kill"):
        process.kill()
        process.join(timeout=2)

    if process.is_alive():
        log.error("Viszmo panel worker process %s could not be stopped.", process.pid)
        return False

    with _panel_task_lock:
        if _panel_task_process is process:
            _panel_task_process = None
            _panel_task_stop_event = None
            _panel_task_queue = None
            _panel_task_pump = None
            _panel_task_target_id = ""
    _close_panel_task_resources(process, event_queue, event_pump)
    if active_target:
        _send_panel_event(active_target, {"type": "stopped", "text": "Stopped by user."})
    return True


def _read_panel_commands(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    commands: list[dict[str, Any]] = []
    expression = (
        "(() => {"
        "const commands = window.__viszmoPanelCommands || [];"
        "window.__viszmoPanelCommands = [];"
        "return commands;"
        "})()"
    )
    for target in pages:
        target_id = str(target.get("id") or "")
        client = _panel_clients.get(target_id)
        if client is None:
            continue
        try:
            response = client._send("Runtime.evaluate", {"expression": expression, "returnByValue": True}) or {}
            value = response.get("result", {}).get("result", {}).get("value")
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        item.setdefault("target_id", target_id)
                        commands.append(item)
        except Exception as exc:
            log.debug("Could not read commands from the Viszmo panel on %s: %s", target_id, exc)
    return commands


def _handle_panel_command(command: dict[str, Any]) -> None:
    kind = str(command.get("type") or "").strip().lower()
    target_id = str(command.get("target_id") or "").strip()
    if kind == "abort":
        if not _abort_panel_task(target_id):
            _send_panel_event(target_id, {"type": "error", "text": "The Viszmo task could not be stopped."})
        return
    if kind not in {"launch", "snip"}:
        return
    try:
        import agent_engine

        selected_mode = agent_engine.normalize_mode(command.get("mode"))
        selected_autonomy = agent_engine.normalize_autonomy(command.get("autonomy"))
        snip_box: tuple[float, float, float, float] | None = None
        if kind == "snip":
            crop = command.get("crop")
            if not isinstance(crop, dict):
                raise ValueError("Draw a box around one question, then try Snip again.")
            try:
                x = float(crop.get("x"))
                y = float(crop.get("y"))
                width = float(crop.get("width"))
                height = float(crop.get("height"))
            except (TypeError, ValueError) as exc:
                raise ValueError("The selected snip was invalid. Please try again.") from exc
            if not all(value >= 0 for value in (x, y)) or width < 24 or height < 24:
                raise ValueError("Draw a larger box around one full question, then try Snip again.")
            snip_box = (x, y, width, height)
            # Snips are always review-only: they never interact with the page.
            selected_autonomy = "copilot"
        if target_id:
            select_target(target_id)
        notes = str(command.get("notes") or "").strip()
        if not _start_panel_task(target_id, selected_mode, selected_autonomy, notes, snip_box):
            # A stale panel can send Launch before it has received the Stop
            # acknowledgement. Treat that as replace-the-old-task intent so
            # the user does not get trapped behind a false "already running"
            # state.
            _send_panel_event(target_id, {"type": "status", "text": "Stopping the previous Viszmo taskâ€¦"})
            if not _abort_panel_task(target_id) or not _start_panel_task(
                target_id,
                selected_mode,
                selected_autonomy,
                notes,
                snip_box,
            ):
                _send_panel_event(target_id, {"type": "error", "text": "The previous Viszmo task could not be stopped."})
    except Exception as exc:
        log.exception("Panel command failed")
        _send_panel_event(target_id, {"type": "error", "text": str(exc)})


def install_browser_panel(panel_url: str | None = None) -> None:
    """Install the original Viszmo panel on every page in managed Chrome."""
    global _managed
    commands: list[dict[str, Any]] = []
    with _panel_lock:
        if _managed is None or _refresh_managed() is None:
            raise RuntimeError("No managed assignment browser is connected.")
        pages = _page_targets(cdp_tabs(port=_managed.port))
        if not pages:
            raise RuntimeError("The managed Chrome browser has no open page tabs.")
        primary_id = _managed.target_id
        installed_primary = False
        installed_new_panel = False
        for target in pages:
            target_id = str(target.get("id") or "")
            if not _redirect_builtin_new_tab(target):
                continue
            was_installed = target_id in _managed.panel_script_ids
            source = _panel_source(panel_url, target_id)
            installed = _install_panel_on_target(
                target,
                source,
                _managed.panel_script_ids,
                required=target_id == primary_id,
            )
            if installed and not was_installed:
                installed_new_panel = True
            if target_id == primary_id:
                installed_primary = installed
        _managed.panel_script_id = _managed.panel_script_ids.get(primary_id, "")
        if not installed_primary:
            raise RuntimeError("The Viszmo panel could not connect to the managed Chrome tab yet.")
        commands = _read_panel_commands(pages)
        if installed_new_panel:
            log.info("Installed the original Viszmo panel across %s managed Chrome tab(s).", len(pages))
    for command in commands:
        _handle_panel_command(command)


def select_target(target_id: str) -> dict[str, Any]:
    """Make the tab whose in-page panel was used the active assignment target."""
    global _managed
    normalized = str(target_id or "").strip()
    if not normalized or _managed is None:
        raise RuntimeError("No managed assignment browser is connected.")
    target = next(
        (item for item in _page_targets(cdp_tabs(port=_managed.port)) if str(item.get("id") or "") == normalized),
        None,
    )
    if target is None:
        raise RuntimeError("That Chrome tab is no longer available.")
    if normalized != _managed.target_id:
        try:
            from virtual_mouse import reset_mouse

            reset_mouse()
        except Exception:
            pass
        _managed.target_id = normalized
        _managed.target_ws_url = str(target.get("webSocketDebuggerUrl") or "")
        _managed.panel_script_id = _managed.panel_script_ids.get(normalized, "")
        _managed.title = str(target.get("title") or "")
        _managed.url = str(target.get("url") or "")
        log.info("Selected Chrome tab %s (%s)", normalized, _managed.title or _managed.url)
    return target


def assigned_target() -> dict[str, Any] | None:
    """Return the exact page target owned by the current desktop session."""
    return _refresh_managed()


def assigned_websocket_url() -> str | None:
    target = assigned_target()
    if target is None:
        return None
    return str(target.get("webSocketDebuggerUrl") or "") or None


def assigned_cdp_port() -> int | None:
    return _managed.port if _managed is not None else None


def attach_electron_target(target_id: str, port: int) -> dict[str, Any]:
    """Bind the agent to an assignment BrowserWindow owned by Electron."""
    global _managed
    normalized_id = str(target_id or "").strip()
    try:
        normalized_port = int(port)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Electron did not provide a valid browser debug port.") from exc
    if not normalized_id or normalized_port < 1 or normalized_port > 65535:
        raise RuntimeError("Electron did not provide a valid assignment target.")

    expected_port = os.getenv("VISZMO_ELECTRON_CDP_PORT", "").strip()
    if expected_port and expected_port.isdigit() and int(expected_port) != normalized_port:
        raise RuntimeError("The assignment browser connection does not belong to this Viszmo app.")

    target = next(
        (item for item in cdp_tabs(port=normalized_port) if str(item.get("id") or "") == normalized_id),
        None,
    )
    if target is None or target.get("type") != "page" or not target.get("webSocketDebuggerUrl"):
        raise RuntimeError("Electron's assignment browser tab is not ready yet.")

    # Repeated attach notifications are normal while the browser window is
    # loading. Reuse the existing managed session instead of resetting panel
    # state and registering another new-document script.
    if _managed is not None and _managed.port == normalized_port:
        selected = select_target(normalized_id)
        _start_panel_watcher()
        return selected

    _managed = ManagedBrowser(
        executable=Path("Electron"),
        profile_dir=_data_root() / "ElectronAssignmentProfile",
        port=normalized_port,
        process=None,
        target_id=normalized_id,
        target_ws_url=str(target["webSocketDebuggerUrl"]),
        title=str(target.get("title") or ""),
        url=str(target.get("url") or ""),
        hwnd=None,
    )
    layout.clear_pin()
    _start_panel_watcher()
    log.info("Attached to Electron assignment target %s (%s)", normalized_id, _managed.title or _managed.url)
    return target


def connected_tab() -> dict[str, Any]:
    """Return status for the managed assignment page, independent of focus."""
    target = assigned_target()
    if target is None:
        return {
            "connected": False,
            "managed": False,
            "cleanup_available": has_closeable_session(),
            "title": "",
            "url": "",
            "label": "No assignment browser connected",
        }
    try:
        # This catches a newly opened tab even before the background watcher
        # gets its next pass. Existing targets are skipped after first install.
        install_browser_panel()
    except Exception as exc:
        log.debug("Panel refresh while reading tab status: %s", exc)
    target = assigned_target()
    if target is None:
        return {
            "connected": False,
            "managed": False,
            "cleanup_available": has_closeable_session(),
            "title": "",
            "url": "",
            "label": "No assignment browser connected",
        }
    title = str(target.get("title") or "").strip()
    url = str(target.get("url") or "").strip()
    return {
        "connected": True,
        "managed": True,
        "cleanup_available": has_closeable_session(),
        "title": title or url or "Assignment browser",
        "url": url,
        "label": title or url or "Assignment browser",
        "browser": _managed.executable.stem if _managed else "chromium",
        "port": _managed.port if _managed else None,
        "target_id": _managed.target_id if _managed else "",
    }


def _reclaim_orphaned_profile_browser(profile_dir: Path) -> int:
    """Terminate leftover managed-browser processes holding this profile.

    A backend crash or hard Ctrl+C can orphan the assignment browser. The
    next launch would otherwise lose to the zombie: Chrome hands off to the
    process that already owns the profile directory and exits immediately,
    so the fresh debug port never opens.
    """
    if sys.platform != "win32":
        return 0
    marker = str(profile_dir).replace("'", "''")
    script = (
        "$ErrorActionPreference='SilentlyContinue';"
        "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe' OR Name='msedge.exe' OR Name='brave.exe'\" | "
        "Where-Object { $_.CommandLine -like '*" + marker + "*' } | "
        "Select-Object -ExpandProperty ProcessId"
    )
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except Exception as exc:
        log.debug("Orphan scan skipped: %s", exc)
        return 0
    killed = 0
    own_pid = os.getpid()
    for line in (completed.stdout or "").split():
        token = line.strip()
        if not token.isdigit():
            continue
        pid = int(token)
        if pid == own_pid:
            continue
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                timeout=15,
            )
            killed += 1
        except Exception:
            pass
    if killed:
        # Give the profile lock a moment to release before relaunching.
        time.sleep(0.8)
    return killed


def broadcast_panel_event(event: dict[str, Any]) -> int:
    """Send one event to every installed panel; returns delivery count."""
    with _panel_lock:
        target_ids = list(_panel_clients.keys())
    sent = 0
    for target_id in target_ids:
        try:
            _send_panel_event(target_id, event)
            sent += 1
        except Exception:
            continue
    return sent


def launch_viszmo_chrome(url: str = DEFAULT_START_URL, browser: str = "auto") -> Any | None:
    """Serialize launch requests so duplicate UI events reuse one browser."""
    with _browser_launch_lock:
        return _launch_viszmo_chrome(url, browser)


def _launch_viszmo_chrome(url: str = DEFAULT_START_URL, browser: str = "auto") -> Any | None:
    """Launch or reuse Viszmo's isolated, CDP-enabled assignment browser.

    The returned native window is only a convenience for the status UI. Input
    and screenshots use the assigned CDP target, so the window never needs to
    be focused and the user's system cursor is never involved.
    """
    global _managed
    requested_url = (url or DEFAULT_START_URL).strip() or DEFAULT_START_URL

    if _refresh_managed() is not None:
        _start_panel_watcher()
        log.info("Reusing managed assignment browser on CDP port %s", _managed.port if _managed else "?")
        return _window_for_target(assigned_target() or {})

    exe = find_browser_exe(browser)
    if exe is None:
        wanted = "Chrome or Edge" if browser in {"", "auto"} else browser
        raise RuntimeError(f"{wanted} was not found. Install it, then launch Viszmo again.")

    profile_override = os.getenv("VISZMO_BROWSER_PROFILE", "").strip()
    default_profile = _data_root() / f"BrowserProfile-{exe.stem.lower()}"
    profile_dir = Path(profile_override).expanduser() if profile_override else default_profile
    profile_dir.mkdir(parents=True, exist_ok=True)
    reclaimed = _reclaim_orphaned_profile_browser(profile_dir)
    if reclaimed:
        log.warning(
            "Reclaimed %s orphaned assignment browser process(es) left over from a previous session.",
            reclaimed,
        )
    port = _free_port()
    before_ids = {str(item.get("id") or "") for item in cdp_tabs(port=port)}
    cmd = [
        str(exe),
        f"--remote-debugging-port={port}",
        f"--remote-debugging-address={CDP_HOST}",
        "--remote-allow-origins=*",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--new-window",
        # Keep the assignment page active while the user works elsewhere.
        "--disable-background-timer-throttling",
        "--disable-renderer-backgrounding",
        "--disable-backgrounding-occluded-windows",
        "--disable-features=CalculateNativeWinOcclusion",
        requested_url,
    ]
    log.info("Starting managed %s assignment browser on port %s", exe.stem, port)
    try:
        process_options: dict[str, Any] = {
            "close_fds": True,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if sys.platform == "win32":
            process_options["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            process_options["start_new_session"] = True
        process = subprocess.Popen(
            cmd,
            **process_options,
        )
    except OSError as exc:
        raise RuntimeError(f"Could not start {exe.name}: {exc}") from exc

    target = _wait_for_target(port, before_ids, requested_url)
    if target is None:
        try:
            process.terminate()
        except Exception:
            pass
        raise RuntimeError("The assignment browser started, but its private automation connection was not ready.")

    win = _window_for_target(target)
    hwnd = layout._hwnd(win) if win is not None else None
    _managed = ManagedBrowser(
        executable=exe,
        profile_dir=profile_dir,
        port=port,
        process=process,
        target_id=str(target.get("id") or ""),
        target_ws_url=str(target.get("webSocketDebuggerUrl") or ""),
        title=str(target.get("title") or ""),
        url=str(target.get("url") or ""),
        hwnd=hwnd,
    )
    if win is not None:
        layout.pin_browser(win)
    _start_panel_watcher()
    log.info("Pinned assignment target %s (%s)", _managed.target_id, _managed.title or _managed.url)
    return win


def close_pinned_browser() -> str:
    """Close the managed browser process and forget its target completely."""
    global _managed, _orphaned_processes
    managed_before_refresh = _managed
    target = assigned_target() if managed_before_refresh is not None else None
    processes: list[subprocess.Popen[Any]] = []

    def add_process(process: subprocess.Popen[Any] | None) -> None:
        if process is not None and all(existing is not process for existing in processes):
            processes.append(process)

    add_process(managed_before_refresh.process if managed_before_refresh is not None else None)
    add_process(_managed.process if _managed is not None else None)
    for process in _orphaned_processes:
        add_process(process)

    if managed_before_refresh is None and not processes:
        return "No Viszmo assignment browser is running."
    title = str(
        (target or {}).get("title")
        or (managed_before_refresh.url if managed_before_refresh is not None else "")
        or "assignment browser"
    )[:80]
    _stop_panel_watcher()
    _managed = None
    _orphaned_processes = []
    layout.clear_pin()
    try:
        from virtual_mouse import reset_mouse

        reset_mouse()
    except Exception:
        pass

    failures = 0
    for process in processes:
        if not _terminate_process_tree(process):
            failures += 1
    if failures:
        return f"Closed {title}, but {failures} browser process could not be confirmed stopped."
    return f"Closed {title} and terminated its browser process."


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Launch a managed Viszmo assignment browser")
    parser.add_argument("url", nargs="?", default=DEFAULT_START_URL)
    parser.add_argument("--browser", choices=("auto", "chrome", "edge", "brave"), default="auto")
    args = parser.parse_args()
    launch_viszmo_chrome(args.url, browser=args.browser)
