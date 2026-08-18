"""Launch and identify the Viszmo Chrome/Edge window."""

from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import layout

log = logging.getLogger("chrome_session")

PROFILE_DIR = Path(__file__).resolve().parent / ".viszmo-chrome"


def find_browser_exe() -> Path | None:
    env = os.environ
    candidates = [
        Path(env.get("PROGRAMFILES", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
        Path(env.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
        Path(env.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
        Path(env.get("PROGRAMFILES", r"C:\Program Files")) / "Microsoft/Edge/Application/msedge.exe",
        Path(env.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
        Path(env.get("LOCALAPPDATA", "")) / "Microsoft/Edge/Application/msedge.exe",
    ]
    for path in candidates:
        if path and path.is_file():
            return path
    return None


def snapshot_hwnds() -> set[int]:
    found: set[int] = set()
    for win in layout._windows():
        hwnd = layout._hwnd(win)
        if hwnd:
            found.add(hwnd)
    return found


def _wait_for_new_browser(before: set[int], timeout_s: float = 8.0) -> Any | None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        matches = []
        for win in layout._windows():
            hwnd = layout._hwnd(win)
            title = layout._title(win)
            if not hwnd or hwnd in before:
                continue
            if not layout._is_browser(title):
                continue
            width = int(getattr(win, "width", 0) or 0)
            height = int(getattr(win, "height", 0) or 0)
            if width < 400 or height < 300:
                continue
            matches.append(win)
        if matches:
            matches.sort(key=lambda w: int(w.width) * int(w.height), reverse=True)
            return matches[0]
        time.sleep(0.2)
    return None


def launch_viszmo_chrome(url: str = "about:blank") -> Any:
    """Open a dedicated Chrome/Edge profile and pin that window as the copilot target."""
    existing = layout.find_browser_window()
    if existing is not None and layout.is_pinned(existing):
        try:
            existing.activate()
        except Exception:
            pass
        return existing

    top = layout.find_top_browser()
    if top is not None:
        layout.pin_browser(top)
        try:
            top.activate()
        except Exception:
            pass
        log.info("Pinned top browser: %s", layout._title(top)[:80])
        return top

    exe = find_browser_exe()
    if exe is None:
        raise RuntimeError("Chrome or Edge was not found. Install Chrome, then press Launch again.")

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    before = snapshot_hwnds()
    cmd = [
        str(exe),
        f"--user-data-dir={PROFILE_DIR}",
        "--no-first-run",
        "--no-default-browser-check",
        "--new-window",
        url,
    ]
    log.info("Starting Viszmo browser: %s", exe.name)
    subprocess.Popen(cmd, close_fds=True)
    win = _wait_for_new_browser(before)
    if win is None:
        win = layout.find_browser_window()
    if win is None:
        raise RuntimeError("Browser started but no window was found. Click the Chrome window, then Launch again.")
    layout.pin_browser(win)
    try:
        win.activate()
    except Exception:
        pass
    log.info("Pinned launched browser: %s", layout._title(win)[:80])
    return win


def close_pinned_browser() -> str:
    win = layout.find_browser_window()
    layout.clear_pin()
    if win is None:
        return "No Viszmo browser window to close."
    title = layout._title(win)[:80]
    try:
        win.close()
    except Exception as exc:
        return f"Could not close browser: {exc}"
    return f"Closed {title}"
