"""Browser-scoped input for the managed assignment tab.

Every event in this module is sent through the Chrome DevTools Protocol to a
specific page target. There is intentionally no OS cursor movement, keyboard
control, global input lock, or foreground-window focus.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
from typing import Any

log = logging.getLogger("virtual_mouse")

DEFAULT_CDP_READ_TIMEOUT_SECONDS = 30.0
MIN_CDP_READ_TIMEOUT_SECONDS = 10.0
MAX_CDP_READ_TIMEOUT_SECONDS = 120.0

# A spawned agent worker cannot inherit chrome_session's in-memory target
# selection on Windows.  The parent passes the exact CDP WebSocket URL into
# the worker so it remains bound to the same assignment tab.
_configured_cdp_target: str | None = None


def _cdp_read_timeout_seconds() -> float:
    raw = os.getenv(
        "VISZMO_CDP_READ_TIMEOUT_SECONDS",
        str(DEFAULT_CDP_READ_TIMEOUT_SECONDS),
    )
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = DEFAULT_CDP_READ_TIMEOUT_SECONDS
    return max(MIN_CDP_READ_TIMEOUT_SECONDS, min(MAX_CDP_READ_TIMEOUT_SECONDS, value))


DEFAULT_SCREENSHOT_FORMAT = "jpeg"
DEFAULT_JPEG_QUALITY = 88
MIN_JPEG_QUALITY = 30


def _screenshot_format() -> str:
    """Return the wire format for page captures: jpeg by default for size."""
    raw = os.getenv("VISZMO_SCREENSHOT_FORMAT", DEFAULT_SCREENSHOT_FORMAT)
    normalized = str(raw).strip().lower()
    return "png" if normalized == "png" else "jpeg"


def _screenshot_jpeg_quality() -> int:
    raw = os.getenv("VISZMO_JPEG_QUALITY", str(DEFAULT_JPEG_QUALITY))
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        value = DEFAULT_JPEG_QUALITY
    return max(MIN_JPEG_QUALITY, min(100, value))

# Physical US-keyboard mappings for printable characters that appear often in
# math expressions.  The browser needs the physical key/code and Shift state,
# not only the character in ``text``; math editors commonly inspect both.
_PRINTABLE_KEY_MAP: dict[str, tuple[str, str, int, str, int]] = {
    " ": (" ", "Space", 32, " ", 0),
    "-": ("-", "Minus", 189, "-", 0),
    "_": ("_", "Minus", 189, "-", 8),
    "=": ("=", "Equal", 187, "=", 0),
    "+": ("+", "Equal", 187, "=", 8),
    "[": ("[", "BracketLeft", 219, "[", 0),
    "{": ("{", "BracketLeft", 219, "[", 8),
    "]": ("]", "BracketRight", 221, "]", 0),
    "}": ("}", "BracketRight", 221, "]", 8),
    "\\": ("\\", "Backslash", 220, "\\", 0),
    "|": ("|", "Backslash", 220, "\\", 8),
    ";": (";", "Semicolon", 186, ";", 0),
    ":": (":", "Semicolon", 186, ";", 8),
    "'": ("'", "Quote", 222, "'", 0),
    '"': ('"', "Quote", 222, "'", 8),
    ",": (",", "Comma", 188, ",", 0),
    "<": ("<", "Comma", 188, ",", 8),
    ".": (".", "Period", 190, ".", 0),
    ">": (">", "Period", 190, ".", 8),
    "/": ("/", "Slash", 191, "/", 0),
    "?": ("?", "Slash", 191, "/", 8),
    "`": ("`", "Backquote", 192, "`", 0),
    "~": ("~", "Backquote", 192, "`", 8),
    "!": ("!", "Digit1", 49, "1", 8),
    "@": ("@", "Digit2", 50, "2", 8),
    "#": ("#", "Digit3", 51, "3", 8),
    "$": ("$", "Digit4", 52, "4", 8),
    "%": ("%", "Digit5", 53, "5", 8),
    "^": ("^", "Digit6", 54, "6", 8),
    "&": ("&", "Digit7", 55, "7", 8),
    "*": ("*", "Digit8", 56, "8", 8),
    "(": ("(", "Digit9", 57, "9", 8),
    ")": (")", "Digit0", 48, "0", 8),
}


def _key_to_cdp(key: str) -> dict[str, Any]:
    """Map a browser key name or character to CDP key-event fields."""
    if key in _PRINTABLE_KEY_MAP:
        value, code, virtual_key, _, _ = _PRINTABLE_KEY_MAP[key]
        return {
            "key": value,
            "code": code,
            "windowsVirtualKeyCode": virtual_key,
            "nativeVirtualKeyCode": virtual_key,
        }

    key_map: dict[str, tuple[str, str, int]] = {
        "enter": ("Enter", "Enter", 13),
        "return": ("Enter", "Enter", 13),
        "tab": ("Tab", "Tab", 9),
        "escape": ("Escape", "Escape", 27),
        "esc": ("Escape", "Escape", 27),
        "backspace": ("Backspace", "Backspace", 8),
        "delete": ("Delete", "Delete", 46),
        "up": ("ArrowUp", "ArrowUp", 38),
        "down": ("ArrowDown", "ArrowDown", 40),
        "left": ("ArrowLeft", "ArrowLeft", 37),
        "right": ("ArrowRight", "ArrowRight", 39),
        "home": ("Home", "Home", 36),
        "end": ("End", "End", 35),
        "pageup": ("PageUp", "PageUp", 33),
        "pagedown": ("PageDown", "PageDown", 34),
        "space": (" ", "Space", 32),
        "ctrl": ("Control", "ControlLeft", 17),
        "control": ("Control", "ControlLeft", 17),
        "alt": ("Alt", "AltLeft", 18),
        "shift": ("Shift", "ShiftLeft", 16),
        "win": ("Meta", "MetaLeft", 91),
        "meta": ("Meta", "MetaLeft", 91),
        "cmd": ("Meta", "MetaLeft", 91),
    }
    normalized = key.strip().lower()
    if normalized in key_map:
        value, code, virtual_key = key_map[normalized]
        return {
            "key": value,
            "code": code,
            "windowsVirtualKeyCode": virtual_key,
            "nativeVirtualKeyCode": virtual_key,
        }
    if len(key) == 1 and key.isascii() and key.isalpha():
        upper = key.upper()
        return {
            "key": key,
            "code": f"Key{upper}",
            "windowsVirtualKeyCode": ord(upper),
            "nativeVirtualKeyCode": ord(upper),
        }
    if len(key) == 1 and key.isascii() and key.isdigit():
        return {
            "key": key,
            "code": f"Digit{key}",
            "windowsVirtualKeyCode": ord(key),
            "nativeVirtualKeyCode": ord(key),
        }
    return {"key": key, "code": key, "windowsVirtualKeyCode": 0, "nativeVirtualKeyCode": 0}


def _character_modifiers(char: str) -> int:
    if char in _PRINTABLE_KEY_MAP:
        return _PRINTABLE_KEY_MAP[char][4]
    if len(char) == 1 and char.isascii() and char.isalpha() and char.isupper():
        return 8  # Shift
    return 0


def _unmodified_text(char: str) -> str:
    if char in _PRINTABLE_KEY_MAP:
        return _PRINTABLE_KEY_MAP[char][3]
    if len(char) == 1 and char.isascii() and char.isalpha():
        return char.lower()
    return char


def _has_physical_key(char: str) -> bool:
    return char in _PRINTABLE_KEY_MAP or (
        len(char) == 1 and char.isascii() and (char.isalpha() or char.isdigit())
    )


_MODIFIER_BITS = {
    "ctrl": 2,
    "control": 2,
    "alt": 1,
    "shift": 8,
    "win": 4,
    "meta": 4,
    "cmd": 4,
}

# Probe the live page for the real interactive element under/near an intended
# click point. Coordinates are viewport CSS pixels, matching what the agent's
# screenshots show. The Viszmo panel subtree is ignored so it can never steal
# a snap, and frame boundaries are never crossed.
_SNAP_ELEMENT_JS_TEMPLATE = """
(() => {
  const px = __X__, py = __Y__;
  const inPanel = (el) => {
    for (let n = el; n; n = n.parentElement) {
      if (n.id === '__viszmo_panel__') return true;
    }
    return false;
  };
  const interactiveTags = new Set(['BUTTON','A','INPUT','SELECT','LABEL','SUMMARY','TEXTAREA','OPTION']);
  const interactiveRoles = new Set(['button','option','checkbox','radio','tab','menuitem','switch','link','combobox','textbox','searchbox']);
  const isInteractive = (el) => {
    if (!el) return false;
    const tag = el.tagName;
    if (tag === 'IFRAME' || tag === 'EMBED' || tag === 'OBJECT') return false;
    if (interactiveTags.has(tag)) return true;
    if (el.isContentEditable) return true;
    const role = (el.getAttribute('role') || '').toLowerCase();
    if (role && interactiveRoles.has(role)) return true;
    if (tag !== 'HTML' && typeof el.onclick === 'function') return true;
    try {
      if (getComputedStyle(el).cursor === 'pointer') return true;
    } catch (err) {}
    return false;
  };
  const vw = window.innerWidth || document.documentElement.clientWidth;
  const vh = window.innerHeight || document.documentElement.clientHeight;
  const probes = [[0,0],[5,0],[-5,0],[0,5],[0,-5],[11,0],[-11,0],[0,11],[0,-11]];
    for (const [dx, dy] of probes) {
    const qx = px + dx, qy = py + dy;
    if (qx < 0 || qy < 0 || qx >= vw || qy >= vh) continue;
    let stack = [];
    try {
      stack = document.elementsFromPoint(qx, qy) || [];
    } catch (err) {
      continue;
    }
    for (const el of stack) {
      if (inPanel(el)) continue;
      if (!isInteractive(el)) continue;
      const rect = el.getBoundingClientRect();
      if (rect.width <= 0 || rect.height <= 0) continue;
      if (rect.width * rect.height > 0.35 * vw * vh) continue;
      const visibleHeight = Math.min(rect.bottom, vh) - Math.max(rect.top, 0);
      const clipped = visibleHeight < 0.5 * rect.height || rect.top < 0 || rect.bottom > vh;
      if (clipped) {
        // Center the real control, then report its post-scroll position so
        // the click lands on a fully visible target instead of a clipped or
        // offscreen guess.
        try {
          el.scrollIntoView({ block: 'center', behavior: 'instant' });
        } catch (err2) {
          el.scrollIntoView();
        }
        const shown = el.getBoundingClientRect();
        return {
          x: Math.round(shown.left + shown.width / 2),
          y: Math.round(shown.top + shown.height / 2),
          scrolled: true,
          tag: String(el.tagName || '').toLowerCase(),
          role: String(el.getAttribute('role') || ''),
          text: String((el.innerText || el.value || '')).replace(/\\s+/g, ' ').trim().slice(0, 80),
        };
      }
      return {
        x: Math.round(rect.left + rect.width / 2),
        y: Math.round(rect.top + rect.height / 2),
        scrolled: false,
        tag: String(el.tagName || '').toLowerCase(),
        role: String(el.getAttribute('role') || ''),
        text: String((el.innerText || el.value || '')).replace(/\\s+/g, ' ').trim().slice(0, 80),
      };
    }
  }
  return null;
})()
""".strip()


def _modifier_mask(keys: list[str]) -> int:
    return sum(_MODIFIER_BITS.get(key.strip().lower(), 0) for key in keys)


_READ_ACTIVE_TEXT_JS = """
(() => {
  const el = document.activeElement;
  if (!el) return null;
  const tag = String(el.tagName || '').toLowerCase();
  let text = '';
  if (tag === 'input' || tag === 'textarea') {
    text = el.value || '';
  } else if (el.isContentEditable) {
    text = el.innerText || el.textContent || '';
  } else {
    text = el.value || el.innerText || '';
  }
  return {tag: tag, text: String(text).replace(/\\s+/g, ' ').trim().slice(0, 200)};
})()
""".strip()


class CdpMouse:
    """Send pointer and keyboard events to one Chromium page target."""

    mode = "cdp"

    def __init__(self, ws_url: str) -> None:
        self._ws_url = ws_url
        self._read_timeout_seconds = _cdp_read_timeout_seconds()
        self._lock = threading.Lock()
        self._id = 0
        self._ws: Any = None
        self._connect()

    def _connect(self) -> None:
        try:
            import websocket  # type: ignore[import]
        except ImportError:
            from websockets.sync.client import connect

            try:
                self._ws = connect(self._ws_url, open_timeout=4, proxy=None)
            except TypeError:
                self._ws = connect(self._ws_url, open_timeout=4)
            log.info("Connected to the assignment page through CDP.")
            return

        self._ws = websocket.create_connection(
            self._ws_url,
            timeout=4,
            http_proxy_host=None,
            https_proxy_host=None,
        )
        log.info("Connected to the assignment page through CDP.")

    def _recv(self) -> Any:
        if self._ws is None:
            raise RuntimeError("The assignment browser connection is closed.")
        if hasattr(self._ws, "recv"):
            try:
                return self._ws.recv(timeout=self._read_timeout_seconds)
            except TypeError:
                return self._ws.recv()
        raise RuntimeError("CDP WebSocket has no receive method.")

    @property
    def is_connected(self) -> bool:
        return self._ws is not None

    def _send(self, method: str, params: dict[str, Any]) -> dict[str, Any] | None:
        with self._lock:
            if self._ws is None:
                raise RuntimeError("The assignment browser connection is closed.")
            self._id += 1
            request_id = self._id
            message = json.dumps({"id": request_id, "method": method, "params": params})
            try:
                self._ws.send(message)
                while True:
                    raw = self._recv()
                    if not raw:
                        return None
                    payload = json.loads(raw)
                    # CDP sends page events between command responses.
                    if payload.get("id") == request_id:
                        if payload.get("error"):
                            raise RuntimeError(str(payload["error"]))
                        return payload
            except Exception as exc:
                message = str(exc)
                if method == "Page.handleJavaScriptDialog" and "-32602" in message:
                    # Probing for a dialog when none exists is a normal,
                    # expected outcome of popup dismissal.
                    log.debug("No JavaScript dialog to dismiss: %s", message)
                else:
                    log.warning("CDP command failed (%s): %s", method, exc)
                self._close_unlocked()
                raise RuntimeError(f"Assignment browser connection failed during {method}.") from exc

    def _close_unlocked(self) -> None:
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    def close(self) -> None:
        with self._lock:
            self._close_unlocked()

    def capture_screenshot(self, exclude_right_px: int = 0) -> tuple[bytes, float, float]:
        """Capture the full page viewport without depending on desktop focus.

        ``exclude_right_px`` is retained for callers from older builds.  The
        panel is draggable, so cropping a fixed strip from the right can also
        remove an assignment's own navigation sidebar.  The panel is hidden
        during capture instead, which keeps every page control visible.
        """
        metrics = self._send("Page.getLayoutMetrics", {}) or {}
        result_metrics = metrics.get("result", {})
        viewport = result_metrics.get("cssVisualViewport") or result_metrics.get("layoutViewport") or {}
        css_width = float(viewport.get("clientWidth") or viewport.get("width") or 0)
        css_height = float(viewport.get("clientHeight") or viewport.get("height") or 0)
        panel_present = False
        visible_width = css_width
        if css_width > 1:
            try:
                panel_check = self._send(
                    "Runtime.evaluate",
                    {
                        "expression": "Boolean(document.getElementById('__viszmo_panel__'))",
                        "returnByValue": True,
                    },
                ) or {}
                panel_present = bool(panel_check.get("result", {}).get("result", {}).get("value"))
            except RuntimeError:
                panel_present = False
            if panel_present:
                self._send(
                    "Runtime.evaluate",
                    {
                        "expression": "(() => { const panel = document.getElementById('__viszmo_panel__'); if (panel) panel.style.visibility = 'hidden'; })()",
                    },
                )
        try:
            capture_format = _screenshot_format()
            capture_params: dict[str, Any] = {
                "format": capture_format,
                "fromSurface": True,
                "captureBeyondViewport": False,
            }
            if capture_format == "jpeg":
                capture_params["quality"] = _screenshot_jpeg_quality()
            result = self._send("Page.captureScreenshot", capture_params) or {}
            data = result.get("result", {}).get("data")
            if not data:
                raise RuntimeError("CDP did not return an assignment page screenshot.")
        finally:
            if panel_present:
                try:
                    self._send(
                        "Runtime.evaluate",
                        {
                            "expression": "(() => { const panel = document.getElementById('__viszmo_panel__'); if (panel) panel.style.visibility = 'visible'; })()",
                        },
                    )
                except RuntimeError:
                    pass
        try:
            return base64.b64decode(data), visible_width, css_height
        except Exception as exc:
            raise RuntimeError("CDP returned an invalid assignment page screenshot.") from exc

    def _send_many(self, commands: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any] | None]:
        """Pipeline several CDP commands in order and collect responses by id.

        Chromium processes input events in arrival order and matches responses
        by request id, so one network round trip can carry a whole run of
        keystrokes instead of two round trips per character.
        """
        if not commands:
            return []
        with self._lock:
            if self._ws is None:
                raise RuntimeError("The assignment browser connection is closed.")
            ids: list[int] = []
            try:
                for method, params in commands:
                    self._id += 1
                    request_id = self._id
                    ids.append(request_id)
                    self._ws.send(json.dumps({"id": request_id, "method": method, "params": params}))
            except Exception as exc:
                log.warning("CDP batch send failed: %s", exc)
                self._close_unlocked()
                raise RuntimeError("Assignment browser connection failed while sending input.") from exc
            responses: dict[int, dict[str, Any]] = {}
            pending = set(ids)
            try:
                while pending:
                    raw = self._recv()
                    if not raw:
                        break
                    payload = json.loads(raw)
                    response_id = payload.get("id")
                    if response_id in pending:
                        pending.discard(response_id)
                        responses[response_id] = payload
            except Exception as exc:
                log.warning("CDP batch read failed: %s", exc)
                self._close_unlocked()
                raise RuntimeError("Assignment browser connection failed while reading input results.") from exc
            results: list[dict[str, Any] | None] = []
            for request_id in ids:
                payload = responses.get(request_id)
                if payload is not None and payload.get("error"):
                    raise RuntimeError(str(payload["error"]))
                results.append(payload)
            return results

    def read_active_text(self) -> dict[str, Any] | None:
        """Return the tag and visible text of the focused field, if any.

        Used right after typing to verify what the page actually recorded —
        math editors silently dropping a template key or auto-formatting a
        value never reach the screenshot as an obvious error.
        """
        try:
            result = self._send(
                "Runtime.evaluate",
                {"expression": _READ_ACTIVE_TEXT_JS, "returnByValue": True},
            ) or {}
        except RuntimeError:
            return None
        value = result.get("result", {}).get("result", {}).get("value")
        if not isinstance(value, dict):
            return None
        return value

    def page_url(self) -> str:
        """Return the assignment tab's current URL, or '' when unavailable.

        Spawned worker processes cannot read the launcher's in-memory state,
        so the live page is the only portable identity for progress notes.
        """
        try:
            result = self._send(
                "Runtime.evaluate",
                {"expression": "String(location.href || '')", "returnByValue": True},
            ) or {}
        except RuntimeError:
            return ""
        value = result.get("result", {}).get("result", {}).get("value")
        return str(value or "")

    def snap_to_element(
        self,
        x: int,
        y: int,
        bounds: tuple[int, int, int, int] | None = None,
    ) -> dict[str, Any] | None:
        """Return the real interactive element near (x, y), if one is found.

        ``bounds`` is an optional CSS-pixel rect (left, top, right, bottom)
        describing where a snapped center may land. A candidate outside it
        would be a different control than the model intended, so the probe
        reports no match instead.
        """
        expression = (
            _SNAP_ELEMENT_JS_TEMPLATE.replace("__X__", str(int(x)))
            .replace("__Y__", str(int(y)))
        )
        try:
            result = self._send(
                "Runtime.evaluate",
                {"expression": expression, "returnByValue": True},
            ) or {}
        except RuntimeError:
            return None
        value = (
            result.get("result", {})
            .get("result", {})
            .get("value")
        )
        if not isinstance(value, dict):
            return None
        try:
            sx = int(value["x"])
            sy = int(value["y"])
        except (KeyError, TypeError, ValueError):
            return None
        if bounds is not None:
            left, top, right, bottom = bounds
            if not (left <= sx <= right and top <= sy <= bottom):
                return None
        return value

    def click(self, x: int, y: int) -> None:
        for event_type, buttons in (("mousePressed", 1), ("mouseReleased", 0)):
            self._send(
                "Input.dispatchMouseEvent",
                {
                    "type": event_type,
                    "x": int(x),
                    "y": int(y),
                    "pointerType": "mouse",
                    "button": "left",
                    "clickCount": 1,
                    "buttons": buttons,
                },
            )

    def scroll(self, x: int, y: int, clicks: int) -> None:
        """Scroll at page coordinates; negative clicks mean down."""
        self._send(
            "Input.dispatchMouseEvent",
            {
                "type": "mouseWheel",
                "x": int(x),
                "y": int(y),
                "pointerType": "mouse",
                "deltaX": 0,
                "deltaY": -int(clicks) * 120,
            },
        )

    def type_text(self, text: str, interval: float = 0.01) -> None:
        """Type text with pipelined keystrokes.

        Characters are grouped so one CDP batch carries a run of ordered
        keyDown/keyUp pairs. The inter-key interval becomes an inter-chunk
        pause, which keeps live math editors responsive without paying a
        round trip and a sleep for every single character.
        """
        if not text:
            return
        chunk_chars = 12
        commands: list[tuple[str, dict[str, Any]]] = []

        def flush() -> None:
            nonlocal commands
            if commands:
                self._send_many(commands)
                commands = []
                if interval > 0:
                    time.sleep(interval)

        for char in text:
            if not _has_physical_key(char):
                flush()
                self._send("Input.insertText", {"text": char})
                if interval > 0:
                    time.sleep(interval)
                continue
            payload = _key_to_cdp(char)
            modifiers = _character_modifiers(char)
            commands.append((
                "Input.dispatchKeyEvent",
                {
                    "type": "keyDown",
                    "modifiers": modifiers,
                    "text": char,
                    "unmodifiedText": _unmodified_text(char),
                    **payload,
                },
            ))
            commands.append((
                "Input.dispatchKeyEvent",
                {"type": "keyUp", "modifiers": modifiers, **payload},
            ))
            if len(commands) >= chunk_chars * 2:
                flush()
        flush()

    def press_key(self, key: str) -> None:
        payload = _key_to_cdp(key)
        modifiers = _character_modifiers(key)
        for event_type in ("keyDown", "keyUp"):
            self._send(
                "Input.dispatchKeyEvent",
                {"type": event_type, "modifiers": modifiers, **payload},
            )

    def hotkey(self, *keys: str) -> None:
        normalized = [key.strip().lower() for key in keys]
        modifiers = [key for key in normalized if key in _MODIFIER_BITS]
        regular = [key for key in normalized if key not in _MODIFIER_BITS]
        mask = _modifier_mask(modifiers)
        for key in modifiers:
            self._send(
                "Input.dispatchKeyEvent",
                {"type": "keyDown", "modifiers": mask, **_key_to_cdp(key)},
            )
        for key in regular:
            payload = _key_to_cdp(key)
            self._send("Input.dispatchKeyEvent", {"type": "keyDown", "modifiers": mask, **payload})
            self._send("Input.dispatchKeyEvent", {"type": "keyUp", "modifiers": mask, **payload})
        for key in reversed(modifiers):
            self._send(
                "Input.dispatchKeyEvent",
                {"type": "keyUp", "modifiers": 0, **_key_to_cdp(key)},
            )

    def select_all(self) -> None:
        self.hotkey("ctrl", "a")


def _discover_cdp_ws_url() -> str | None:
    """Return the WebSocket for the target assigned by the desktop launcher."""
    if _configured_cdp_target:
        return _configured_cdp_target
    try:
        import chrome_session

        return chrome_session.assigned_websocket_url()
    except Exception as exc:
        log.debug("Managed browser discovery failed: %s", exc)
    return None


def configure_cdp_target(ws_url: str | None) -> None:
    """Use an explicit CDP target when running in a spawned worker."""
    global _configured_cdp_target
    _configured_cdp_target = str(ws_url or "").strip() or None
    reset_mouse()


def configured_cdp_target() -> str | None:
    """Return the worker-local CDP target override, if one is configured."""
    return _configured_cdp_target


def clear_cdp_target() -> None:
    """Clear the worker-local CDP target override and close its client."""
    global _configured_cdp_target
    _configured_cdp_target = None
    reset_mouse()


_mouse: CdpMouse | None = None
_mouse_ws_url: str | None = None


def get_mouse() -> CdpMouse:
    """Return the CDP client or raise a clear setup error.

    There is deliberately no OS-level fallback. A missing managed browser is
    safer than touching the user's real cursor or another application.
    """
    global _mouse, _mouse_ws_url
    ws_url = _discover_cdp_ws_url()
    if not ws_url:
        raise RuntimeError(
            "No managed assignment browser is connected. Launch Viszmo's assignment browser first."
        )
    if _mouse is not None and _mouse_ws_url == ws_url and _mouse.is_connected:
        return _mouse
    reset_mouse()
    _mouse = CdpMouse(ws_url)
    _mouse_ws_url = ws_url
    return _mouse


def reset_mouse() -> None:
    """Close the page connection without closing the browser itself."""
    global _mouse, _mouse_ws_url
    if _mouse is not None:
        _mouse.close()
    _mouse = None
    _mouse_ws_url = None
