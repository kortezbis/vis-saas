"""Thread-safe Tkinter target reticle overlay (click-through, excluded from screenshots)."""

from __future__ import annotations

import ctypes
import logging
import queue
import sys
import threading
from typing import Any

log = logging.getLogger("overlay")

_TRANSPARENT = "#010101"
_GLOW = "#38bdf8"
_CORE = "#0ea5e9"
_FILL_GLOW = "#34d399"
_FILL_CORE = "#10b981"

_commands: queue.Queue[tuple[Any, ...]] = queue.Queue()
_ready = threading.Event()
_thread: threading.Thread | None = None
_lock = threading.Lock()


def _enable_dpi() -> None:
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _hwnd(root: Any) -> int:
    user32 = ctypes.windll.user32
    hwnd = int(root.winfo_id())
    ancestor = user32.GetAncestor(hwnd, 2)
    return ancestor or hwnd


def _exclude_from_capture(root: Any) -> None:
    if sys.platform != "win32":
        return
    ctypes.windll.user32.SetWindowDisplayAffinity(_hwnd(root), 0x00000011)


def _click_through(root: Any) -> None:
    if sys.platform != "win32":
        return
    hwnd = _hwnd(root)
    GWL_EXSTYLE = -20
    style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    ctypes.windll.user32.SetWindowLongW(
        hwnd,
        GWL_EXSTYLE,
        style | 0x00080000 | 0x00000020 | 0x00000080,
    )
    ctypes.windll.user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0002 | 0x0001 | 0x0040)


def _draw_reticle(canvas: Any, x: int, y: int, kind: str = "click") -> None:
    glow = _FILL_GLOW if kind == "fill" else _GLOW
    core = _FILL_CORE if kind == "fill" else _CORE
    canvas.create_oval(x - 48, y - 48, x + 48, y + 48, outline=glow, width=2)
    canvas.create_oval(x - 30, y - 30, x + 30, y + 30, outline=core, width=3)
    canvas.create_oval(x - 7, y - 7, x + 7, y + 7, fill=core, outline="white", width=1)
    canvas.create_line(x - 56, y, x - 14, y, fill=glow, width=2)
    canvas.create_line(x + 14, y, x + 56, y, fill=glow, width=2)
    canvas.create_line(x, y - 56, x, y - 14, fill=glow, width=2)
    canvas.create_line(x, y + 14, x, y + 56, fill=glow, width=2)
    if kind == "fill":
        canvas.create_text(
            x,
            y + 68,
            text="TYPING",
            fill=core,
            font=("Segoe UI", 11, "bold"),
        )


def _run() -> None:
    _enable_dpi()
    try:
        import tkinter as tk
    except Exception as exc:
        log.warning("Overlay unavailable (tkinter): %s", exc)
        return

    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    try:
        root.attributes("-transparentcolor", _TRANSPARENT)
    except tk.TclError:
        root.attributes("-alpha", 0.4)
    root.configure(bg=_TRANSPARENT)
    sw = root.winfo_screenwidth()
    sh = root.winfo_screenheight()
    root.geometry(f"{sw}x{sh}+0+0")
    canvas = tk.Canvas(root, bg=_TRANSPARENT, highlightthickness=0, bd=0, width=sw, height=sh)
    canvas.pack(fill="both", expand=True)
    root.update_idletasks()
    try:
        _click_through(root)
        _exclude_from_capture(root)
    except Exception as exc:
        log.warning("Could not configure overlay window: %s", exc)

    hide_job: str | None = None

    def paint(x: int, y: int, kind: str) -> None:
        canvas.delete("all")
        _draw_reticle(canvas, x, y, kind=kind)

    def clear() -> None:
        canvas.delete("all")

    def hide_later(ms: int = 300) -> None:
        nonlocal hide_job
        if hide_job is not None:
            try:
                root.after_cancel(hide_job)
            except Exception:
                pass
        hide_job = root.after(ms, clear)

    def poll() -> None:
        try:
            while True:
                item = _commands.get_nowait()
                op = item[0]
                if op == "show":
                    _, x, y, kind = item
                    kind = str(kind or "click")
                    paint(int(x), int(y), kind)
                    hide_later(900 if kind == "fill" else 350)
                elif op == "hide":
                    clear()
                elif op == "quit":
                    root.destroy()
                    return
        except queue.Empty:
            pass
        root.after(16, poll)

    _ready.set()
    root.after(16, poll)
    root.mainloop()
    _ready.clear()


def ensure_started() -> None:
    global _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return
        _ready.clear()
        _thread = threading.Thread(target=_run, name="viszmo-overlay", daemon=True)
        _thread.start()
    _ready.wait(timeout=2.5)


def show_target(x: int, y: int, kind: str = "click") -> None:
    try:
        ensure_started()
        if not _ready.is_set():
            return
        _commands.put(("show", int(x), int(y), kind))
    except Exception as exc:
        log.warning("Overlay show failed: %s", exc)


def hide_target() -> None:
    try:
        if _ready.is_set():
            _commands.put(("hide",))
    except Exception:
        pass


def show_frame(*_args: Any, **_kwargs: Any) -> None:
    return


def show_browser_frame() -> None:
    return


def hide_frame() -> None:
    return
