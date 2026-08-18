"""Position the copilot: floating overlay vs stuck to the browser window."""

from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("layout")

SIDEBAR_WIDTH = 380
_pinned_hwnd: int | None = None
_last_seen_hwnd: int | None = None

BROWSER_MARKERS = (
    "google chrome",
    "microsoft edge",
    "brave",
    "mozilla firefox",
    "opera",
    "vivaldi",
    "chromium",
)

SKIP_EXES = {
    "cursor.exe",
    "code.exe",
    "devenv.exe",
    "antigravity.exe",
}

COPILOT_TITLES = ("viszmo", "web copilot")


@dataclass(frozen=True)
class Rect:
    left: int
    top: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height

    @property
    def cx(self) -> int:
        return self.left + self.width // 2

    @property
    def cy(self) -> int:
        return self.top + self.height // 2


@dataclass
class NativeWindow:
    hwnd: int
    title: str
    left: int
    top: int
    width: int
    height: int
    visible: bool = True

    @property
    def _hWnd(self) -> int:
        return self.hwnd

    def activate(self) -> None:
        user32 = ctypes.windll.user32
        if user32.IsIconic(self.hwnd):
            user32.ShowWindow(self.hwnd, 9)
        user32.SetForegroundWindow(self.hwnd)

    def moveTo(self, x: int, y: int) -> None:
        ctypes.windll.user32.SetWindowPos(self.hwnd, 0, int(x), int(y), 0, 0, 0x0001 | 0x0040)

    def resizeTo(self, width: int, height: int) -> None:
        ctypes.windll.user32.SetWindowPos(self.hwnd, 0, 0, 0, int(width), int(height), 0x0002 | 0x0040)


def _windows() -> list[Any]:
    try:
        import pygetwindow as gw
    except ImportError:
        return []
    try:
        return list(gw.getAllWindows())
    except Exception:
        return []


def _title(win: Any) -> str:
    return str(getattr(win, "title", "") or "")


def _is_copilot(title: str) -> bool:
    t = title.lower().strip()
    return any(t == marker or t.startswith(marker) for marker in COPILOT_TITLES)


def _is_overlay(win: NativeWindow) -> bool:
    return win.title.lower().strip() == "tk" or _hwnd_class(win.hwnd) == "TkTopLevel"


def _has_browser_marker(title: str) -> bool:
    t = title.lower()
    return any(marker in t for marker in BROWSER_MARKERS)


def _is_browser(title: str) -> bool:
    return (not _is_copilot(title)) and _has_browser_marker(title)


def _hwnd(win: Any) -> int | None:
    for attr in ("_hWnd", "_hwnd", "hwnd"):
        value = getattr(win, attr, None)
        if value:
            return int(value)
    return None


def _user32() -> Any:
    return ctypes.windll.user32


def _hwnd_title(hwnd: int) -> str:
    user32 = _user32()
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value or ""


def _hwnd_class(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    _user32().GetClassNameW(hwnd, buf, 256)
    return buf.value or ""


def _hwnd_rect(hwnd: int) -> Rect | None:
    rect = wintypes.RECT()
    try:
        DWMWA_EXTENDED_FRAME_BOUNDS = 9
        ok = ctypes.windll.dwmapi.DwmGetWindowAttribute(
            hwnd,
            DWMWA_EXTENDED_FRAME_BOUNDS,
            ctypes.byref(rect),
            ctypes.sizeof(rect),
        )
        if ok == 0 and rect.right > rect.left and rect.bottom > rect.top:
            return Rect(rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)
    except Exception:
        pass
    if not _user32().GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    return Rect(rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)


def _exe_name(hwnd: int) -> str:
    try:
        pid = wintypes.DWORD()
        _user32().GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return ""
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not handle:
            return ""
        try:
            size = wintypes.DWORD(32768)
            buf = ctypes.create_unicode_buffer(size.value)
            if ctypes.windll.kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return buf.value.replace("/", "\\").rsplit("\\", 1)[-1].lower()
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    except Exception:
        return ""
    return ""


def _native_from_hwnd(hwnd: int | None) -> NativeWindow | None:
    if not hwnd:
        return None
    user32 = _user32()
    if not user32.IsWindow(hwnd) or not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
        return None
    title = _hwnd_title(hwnd)
    rect = _hwnd_rect(hwnd)
    if rect is None or rect.left <= -10000:
        return None
    return NativeWindow(
        hwnd=int(hwnd),
        title=title,
        left=rect.left,
        top=rect.top,
        width=rect.width,
        height=rect.height,
        visible=True,
    )


def _is_assignment_window(win: NativeWindow) -> bool:
    if win.width < 400 or win.height < 300:
        return False
    if _is_copilot(win.title) or _is_overlay(win):
        return False
    exe = _exe_name(win.hwnd)
    if exe in SKIP_EXES:
        return False
    return _has_browser_marker(win.title)


def _enum_top_windows() -> list[NativeWindow]:
    """Visible top-level windows in z-order (front to back)."""
    if sys.platform != "win32":
        return []
    found: list[NativeWindow] = []
    user32 = _user32()

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def _enum(hwnd, _lparam):
        win = _native_from_hwnd(int(hwnd))
        if win is not None:
            found.append(win)
        return True

    try:
        user32.EnumWindows(_enum, 0)
    except Exception:
        return []
    return found


def work_area() -> Rect:
    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    try:
        rect = RECT()
        SPI_GETWORKAREA = 0x0030
        ctypes.windll.user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0)
        return Rect(rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)
    except Exception:
        import pyautogui

        w, h = pyautogui.size()
        return Rect(0, 0, w, h)


def monitor_work_area_containing(x: int, y: int) -> Rect:
    """Work area of the monitor that contains (x, y). Falls back to the primary work area."""
    if sys.platform != "win32":
        return work_area()

    class MONITORINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("rcMonitor", wintypes.RECT),
            ("rcWork", wintypes.RECT),
            ("dwFlags", wintypes.DWORD),
        ]

    try:
        point = wintypes.POINT(int(x), int(y))
        handle = _user32().MonitorFromPoint(point, 2)
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if handle and _user32().GetMonitorInfoW(handle, ctypes.byref(info)):
            area = info.rcWork
            return Rect(area.left, area.top, area.right - area.left, area.bottom - area.top)
    except Exception:
        pass
    return work_area()


def set_always_on_top(win: Any, enabled: bool) -> None:
    hwnd = _hwnd(win)
    if not hwnd:
        return
    HWND_TOPMOST = -1
    HWND_NOTOPMOST = -2
    SWP_NOMOVE = 0x0002
    SWP_NOSIZE = 0x0001
    SWP_SHOWWINDOW = 0x0040
    ctypes.windll.user32.SetWindowPos(
        hwnd,
        HWND_TOPMOST if enabled else HWND_NOTOPMOST,
        0,
        0,
        0,
        0,
        SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW,
    )


def _safe_move_resize(win: Any, left: int, top: int, width: int, height: int) -> None:
    try:
        if getattr(win, "isMaximized", False):
            win.restore()
    except Exception:
        pass
    try:
        win.moveTo(int(left), int(top))
        win.resizeTo(max(int(width), 320), max(int(height), 200))
    except Exception as exc:
        log.warning("Could not move window %r: %s", _title(win), exc)


def pin_browser(win: Any) -> None:
    global _pinned_hwnd
    _pinned_hwnd = _hwnd(win)


def clear_pin() -> None:
    global _pinned_hwnd
    _pinned_hwnd = None


def is_pinned(win: Any) -> bool:
    hwnd = _hwnd(win)
    return bool(hwnd and _pinned_hwnd and hwnd == _pinned_hwnd)


def sys_platform() -> str:
    return sys.platform


def _window_by_hwnd(hwnd: int | None) -> Any | None:
    native = _native_from_hwnd(hwnd)
    if native is not None:
        return native
    if not hwnd:
        return None
    for win in _windows():
        if _hwnd(win) == hwnd and getattr(win, "visible", True):
            return win
    return None


def _foreground_hwnd() -> int | None:
    try:
        hwnd = int(ctypes.windll.user32.GetForegroundWindow())
        return hwnd or None
    except Exception:
        return None


def _window_under_cursor() -> NativeWindow | None:
    try:
        point = wintypes.POINT()
        user32 = _user32()
        if not user32.GetCursorPos(ctypes.byref(point)):
            return None
        hwnd = int(user32.WindowFromPoint(point) or 0)
        if not hwnd:
            return None
        root = int(user32.GetAncestor(hwnd, 2) or hwnd)
        return _native_from_hwnd(root)
    except Exception:
        return None


def find_top_browser() -> Any | None:
    """Frontmost real browser tab, skipping Viszmo, overlays, and IDEs."""
    for win in _enum_top_windows():
        if _is_assignment_window(win):
            return win
    return last_seen_browser()


def track_foreground_browser() -> None:
    """Follow the browser the user is on: focused window, then cursor, then z-order."""
    global _last_seen_hwnd
    hwnd = _foreground_hwnd()
    if hwnd:
        focused = _native_from_hwnd(hwnd)
        if focused is not None and _is_assignment_window(focused):
            _last_seen_hwnd = focused.hwnd
            return
    under = _window_under_cursor()
    if under is not None and _is_assignment_window(under):
        _last_seen_hwnd = under.hwnd
        return
    top = find_top_browser()
    if top is not None:
        _last_seen_hwnd = int(_hwnd(top) or 0) or _last_seen_hwnd


def last_seen_browser() -> Any | None:
    return _window_by_hwnd(_last_seen_hwnd) if _last_seen_hwnd else None


def find_copilot_window() -> Any | None:
    for win in _enum_top_windows():
        if _is_copilot(win.title):
            return win
    for win in _windows():
        title = _title(win).lower().strip()
        if title == "viszmo" or title.startswith("viszmo"):
            return win
        if title == "web copilot" or title.startswith("web copilot"):
            return win
    return None


def find_browser_window() -> Any | None:
    """Pinned, last-seen, or frontmost browser — never the largest random Chrome window."""
    track_foreground_browser()
    for hwnd in (_pinned_hwnd, _last_seen_hwnd):
        win = _native_from_hwnd(hwnd)
        if win is not None and _is_assignment_window(win):
            return win
    return find_top_browser()


def window_rect(win: Any) -> Rect | None:
    hwnd = _hwnd(win)
    if hwnd:
        rect = _hwnd_rect(hwnd)
        if rect is not None:
            return rect
    try:
        return Rect(int(win.left), int(win.top), int(win.width), int(win.height))
    except Exception:
        return None


def focus_browser() -> bool:
    """Bring Chrome/Edge to the front so clicks actually select page controls."""
    browser = find_browser_window()
    if browser is None:
        return False
    try:
        browser.activate()
        return True
    except Exception as exc:
        log.warning("Could not focus browser: %s", exc)
        return False


def _rects_overlap(a: Rect, b: Rect) -> bool:
    return a.left < b.right and b.left < a.right and a.top < b.bottom and b.top < a.bottom


def assignment_workspace_rect(sidebar_width: int = SIDEBAR_WIDTH) -> Rect | None:
    """Visible work area of the monitor that holds the connected tab."""
    browser = find_browser_window()
    if browser is None:
        return None
    rect = window_rect(browser)
    if rect is None:
        return None
    area = monitor_work_area_containing(rect.cx, rect.cy)
    copilot = find_copilot_window()
    sidebar = window_rect(copilot) if copilot is not None else None
    if sidebar is not None and area.left <= sidebar.cx <= area.right:
        width = max(min(sidebar.left, area.right) - area.left, 1)
        return Rect(area.left, area.top, width, area.height)
    crop = max(int(sidebar_width), 0)
    if crop and abs(area.left) < 40 and area.width > crop + 80:
        return Rect(area.left, area.top, area.width - crop, area.height)
    return area


def browser_workspace_rect(sidebar_width: int, layout_mode: str) -> Rect | None:
    """Screen region the agent should see (the page, not the copilot)."""
    return assignment_workspace_rect(sidebar_width)


def apply_float_layout(sidebar_width: int = SIDEBAR_WIDTH) -> str:
    copilot = find_copilot_window()
    area = work_area()
    if copilot is None:
        return "Floating overlay is on. If you started in a normal browser tab, use Stick to browser or the Chrome side panel."
    _safe_move_resize(
        copilot,
        area.left + area.width - sidebar_width,
        area.top,
        sidebar_width,
        area.height,
    )
    set_always_on_top(copilot, True)
    return "Floating: always-on-top overlay on the right. Switch to Stick to browser to glue it to Chrome/Edge."


def apply_attach_layout(sidebar_width: int = SIDEBAR_WIDTH) -> str:
    browser = find_browser_window()
    copilot = find_copilot_window()
    area = work_area()
    if browser is None:
        return "No Chrome/Edge window found. Open the site first, then click Stick to browser again."
    if copilot is None:
        return "Copilot window not found. Start with: python agent_server.py --desktop"

    browser_w = max(area.width - sidebar_width, 480)
    _safe_move_resize(browser, area.left, area.top, browser_w, area.height)
    _safe_move_resize(
        copilot,
        area.left + browser_w,
        area.top,
        sidebar_width,
        area.height,
    )
    set_always_on_top(copilot, False)
    title = _title(browser)[:80]
    return f"Stuck to browser: {title}"


_BROWSER_SUFFIXES = (
    " - Google Chrome",
    " - Microsoft Edge",
    " - Brave",
    " - Mozilla Firefox",
    " - Opera",
    " - Vivaldi",
    " - Chromium",
)


def page_title(raw: str) -> str:
    title = (raw or "").strip()
    lowered = title.lower()
    for suffix in _BROWSER_SUFFIXES:
        if lowered.endswith(suffix.lower()):
            title = title[: -len(suffix)].strip()
            break
    return title


def connected_tab() -> dict[str, Any]:
    """The assignment tab Viszmo will screenshot — the window you are actually on."""
    win = find_browser_window()
    if win is None:
        return {"connected": False, "title": "", "label": "No tab connected"}
    title = page_title(_title(win)) or "Browser tab"
    return {"connected": True, "title": title, "label": title}


def follow_attach(sidebar_width: int = SIDEBAR_WIDTH) -> None:
    """Keep the copilot glued to the right edge of the browser if it moves."""
    browser = find_browser_window()
    copilot = find_copilot_window()
    if browser is None or copilot is None:
        return
    area = work_area()
    b = window_rect(browser)
    c = window_rect(copilot)
    if b is None or c is None:
        return
    target_x = min(max(b.right, area.left), area.left + area.width - sidebar_width)
    target_y = b.top
    if abs(c.left - target_x) > 12 or abs(c.top - target_y) > 12 or abs(c.height - b.height) > 12:
        _safe_move_resize(copilot, target_x, target_y, sidebar_width, b.height)
        set_always_on_top(copilot, False)
