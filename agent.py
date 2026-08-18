#!/usr/bin/env python3
"""Desktop Vision + Action Loop agent.

Takes a screenshot, asks a multimodal LLM for one JSON action, executes it
with pyautogui, and repeats until `done` or the step limit.

Usage:
    python agent.py "Open calculator and compute 45 * 2"
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import io
import json
import logging
import os
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

# ---------------------------------------------------------------------------
# DPI awareness MUST be set before importing pyautogui / taking screenshots.
# Per-monitor awareness keeps screenshot pixels aligned with mouse coordinates
# on Windows displays that are scaled (125% / 150% / 200%).
# ---------------------------------------------------------------------------
def enable_dpi_awareness() -> None:
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


enable_dpi_awareness()

from PIL import Image, ImageGrab  # noqa: E402
import pyautogui  # noqa: E402

try:
    from dotenv import load_dotenv
except ImportError:  # optional; env vars can be set in the shell
    load_dotenv = None

log = logging.getLogger("agent")

ActionName = Literal["click", "fill", "type", "press_key", "scroll", "wait", "done"]
ALLOWED_ACTIONS = {"click", "fill", "type", "press_key", "scroll", "wait", "done"}

MAX_API_SIDE = 1024
JPEG_QUALITY = 70
DEFAULT_MAX_STEPS = 40
DEFAULT_COUNTDOWN = 5
TYPE_INTERVAL = 0.03
FOCUS_SETTLE = 0.3
POST_TYPE_DELAY = 0.2
MOVE_DURATION = 0.25
LLM_RETRIES = 4
MAX_ACTIONS_PER_TURN = 8
CHAIN_GAP = 0.05
NAV_PAUSE = 0.25
CHANGE_WAIT = 0.9
HISTORY_KEEP = 8
MAX_OUTPUT_TOKENS = 360
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash-lite"
GEMINI_MODEL_FALLBACKS = (
    "gemini-3.5-flash-lite",
    "gemini-3.6-flash",
)

DEFAULT_BROWSER_TASK = """
Work the website currently visible in the browser (left of the copilot sidebar).
Do not wait for a more specific task. Infer the goal from the page.

Your job:
- Answer every visible question now, then immediately advance. The user will NOT press Next.
- Complete every visible form field / radio / checkbox on this screen in ONE turn.
- Always click Next / Continue / Save / Submit / Apply yourself as the LAST action of that turn if the button is visible.
- Keep going through every following question/page until the flow is finished. Never stop after only answering.
- If content is cut off, scroll, then answer, then Next — still in the same turn when possible.
- Prefer fill for text (click the field, then type the extracted answer). Prefer click for radios, checkboxes, buttons, and Next.

How to fill answers:
- Prefer facts the user already typed on the page, in the URL, or in extra notes.
- For ordinary product/site questions, answer from the page plus general knowledge.
- If a required personal field is empty and notes do not provide it, skip that field and still click Next if the rest is done.
- Never invent passwords, 2FA codes, payment card numbers, or SSNs. Skip those fields.
- Never click through cookie walls by accepting everything if a Reject / Necessary-only control is visible — prefer the stricter option.
- Do not close the user's tabs or the copilot sidebar.
""".strip()

SYSTEM_PROMPT = """You are a browser copilot. First READ the screenshot, then ACT.

Return JSON only:
{
  "thought": "Extracted: <the answer/value you read>. Target: <the field or control to fill/click>.",
  "actions": [
    {"action": "fill", "x": <int>, "y": <int>, "text": "<extracted value>"},
    {"action": "click", "x": <int>, "y": <int>}
  ]
}

Workflow (every turn):
1) Perception — Read visible questions, labels, tables, images, and any answer already on screen. Put the extracted value in `thought` BEFORE acting.
2) Targeting — Find the matching input, textarea, radio, checkbox, or button. x,y are pixels in THIS image (top-left origin). Click the CENTER of the control. Coordinates must be inside the image width/height.
3) Execution — For text fields use fill (click the box, then type the extracted text). For choices/buttons use click. Do fields in visual order, top to bottom. If Continue/Next/Submit is visible, click it LAST.

Actions allowed: fill{x,y,text}, click{x,y}, type{text}, press_key{key}, scroll{direction,clicks}, done{reason}.
No wait.

Rules:
- Prefer fill over type so the field is focused first, then the extracted answer is typed.
- Multiple fields on one screen: one fill per field in the same actions array.
- Question + choices: click the matching choice, then click Continue/Next (not the taskbar).
- Never invent passwords, 2FA, card numbers, or SSNs.
- done only on thank-you/finished, or a password/2FA/payment wall.
"""


# ---------------------------------------------------------------------------
# Environment / safety
# ---------------------------------------------------------------------------
def compose_browser_task(notes: str = "") -> str:
    notes = (notes or "").strip()
    if not notes:
        return DEFAULT_BROWSER_TASK
    return (
        DEFAULT_BROWSER_TASK
        + "\n\nAdditional user notes (use these when filling fields):\n"
        + notes
    )


def load_env() -> None:
    if load_dotenv is None:
        return
    env_path = Path(__file__).resolve().parent / ".env"
    load_dotenv(env_path if env_path.exists() else None)


def configure_pyautogui() -> None:
    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.02
    pyautogui.MINIMUM_DURATION = 0.0


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


# ---------------------------------------------------------------------------
# Perception
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Frame:
    jpeg_bytes: bytes
    api_w: int
    api_h: int
    screen_w: int
    screen_h: int
    workspace_w: int
    workspace_h: int
    capture_w: int
    capture_h: int
    sidebar_width: int
    origin_x: int = 0
    origin_y: int = 0
    layout_mode: str = "float"


def capture_region(left: int, top: int, width: int, height: int) -> Image.Image:
    screen_w, screen_h = pyautogui.size()
    left = max(0, min(left, screen_w - 1))
    top = max(0, min(top, screen_h - 1))
    right = max(left + 1, min(left + width, screen_w))
    bottom = max(top + 1, min(top + height, screen_h))
    bbox = (left, top, right, bottom)
    try:
        image = ImageGrab.grab(bbox=bbox, all_screens=False)
    except TypeError:
        image = ImageGrab.grab(bbox=bbox)
    if image.mode != "RGB":
        image = image.convert("RGB")
    return image


def primary_work_area() -> tuple[int, int, int, int]:
    """Screen work area excluding the Windows taskbar when possible."""
    screen_w, screen_h = pyautogui.size()
    if sys.platform != "win32":
        return 0, 0, screen_w, screen_h
    try:
        class RECT(ctypes.Structure):
            _fields_ = [
                ("left", ctypes.c_long),
                ("top", ctypes.c_long),
                ("right", ctypes.c_long),
                ("bottom", ctypes.c_long),
            ]

        rect = RECT()
        SPI_GETWORKAREA = 48
        if ctypes.windll.user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0):
            left, top = int(rect.left), int(rect.top)
            width = max(1, int(rect.right) - left)
            height = max(1, int(rect.bottom) - top)
            return left, top, width, height
    except Exception:
        pass
    return 0, 0, screen_w, screen_h


def capture_primary_screen() -> tuple[Image.Image, int, int, int, int]:
    left, top, width, height = primary_work_area()
    image = capture_region(left, top, width, height)
    return image, left, top, width, height


def crop_sidebar(
    image: Image.Image,
    screen_w: int,
    screen_h: int,
    sidebar_width: int,
) -> tuple[Image.Image, int, int]:
    """Drop the right-hand copilot strip so the model never sees or clicks it."""
    capture_w, capture_h = image.size
    if sidebar_width <= 0:
        return image, screen_w, screen_h

    px_scale_x = capture_w / max(screen_w, 1)
    crop_px = int(round(sidebar_width * px_scale_x))
    crop_px = min(max(crop_px, 0), max(capture_w - 1, 0))
    workspace = image.crop((0, 0, capture_w - crop_px, capture_h))
    workspace_w = max(screen_w - sidebar_width, 1)
    return workspace, workspace_w, screen_h


def compress_for_api(image: Image.Image) -> tuple[bytes, int, int]:
    w, h = image.size
    longest = max(w, h)
    if longest > MAX_API_SIDE:
        scale = MAX_API_SIDE / longest
        w, h = max(1, int(w * scale)), max(1, int(h * scale))
        image = image.resize((w, h), Image.Resampling.LANCZOS)

    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return buf.getvalue(), w, h


def perceive(sidebar_width: int = 0, layout_mode: str = "float") -> Frame:
    screen_w, screen_h = pyautogui.size()
    origin_x, origin_y = 0, 0
    workspace_w = max(screen_w - max(sidebar_width, 0), 1)
    workspace_h = screen_h
    region = None
    try:
        from layout import browser_workspace_rect

        region = browser_workspace_rect(sidebar_width, layout_mode)
    except Exception:
        region = None

    if region is not None:
        origin_x, origin_y = region.left, region.top
        workspace_w, workspace_h = region.width, region.height
        workspace = capture_region(origin_x, origin_y, workspace_w, workspace_h)
    else:
        image, origin_x, origin_y, work_w, work_h = capture_primary_screen()
        workspace, workspace_w, workspace_h = crop_sidebar(
            image, work_w, work_h, sidebar_width
        )
        screen_w, screen_h = pyautogui.size()

    capture_w, capture_h = workspace.size
    jpeg_bytes, api_w, api_h = compress_for_api(workspace)
    return Frame(
        jpeg_bytes=jpeg_bytes,
        api_w=api_w,
        api_h=api_h,
        screen_w=screen_w,
        screen_h=screen_h,
        workspace_w=workspace_w,
        workspace_h=workspace_h,
        capture_w=capture_w,
        capture_h=capture_h,
        sidebar_width=max(sidebar_width, 0),
        origin_x=origin_x,
        origin_y=origin_y,
        layout_mode=layout_mode,
    )


def map_to_screen(x: float, y: float, frame: Frame) -> tuple[int, int]:
    """Map LLM image coordinates onto pyautogui workspace coordinates."""
    fractional = (x % 1 != 0) or (y % 1 != 0)
    if fractional and 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0:
        nx, ny = x, y
    elif y > frame.api_h + 2 or x > frame.api_w + 2:
        # Model used capture/screen pixels instead of the resized API image.
        basis_w = frame.capture_w if y > frame.api_h or x > frame.api_w else frame.api_w
        basis_h = frame.capture_h if y > frame.api_h else frame.api_h
        if x <= frame.capture_w + 2 and y <= frame.capture_h + 2:
            nx = x / max(frame.capture_w, 1)
            ny = y / max(frame.capture_h, 1)
        else:
            nx = x / max(basis_w, 1)
            ny = y / max(basis_h, 1)
        log.info(
            "Coord remap: image (%s,%s) outside API %sx%s, treating as capture %sx%s",
            x,
            y,
            frame.api_w,
            frame.api_h,
            frame.capture_w,
            frame.capture_h,
        )
    else:
        nx = x / max(frame.api_w, 1)
        ny = y / max(frame.api_h, 1)

    nx = min(max(nx, 0.02), 0.98)
    # Keep clicks in the page content band, never the last 6% (taskbar / window edge).
    ny = min(max(ny, 0.04), 0.94)
    inset_x = max(int(frame.workspace_w * 0.01), 6)
    inset_y = max(int(frame.workspace_h * 0.03), 18)
    sx = frame.origin_x + int(round(nx * frame.workspace_w))
    sy = frame.origin_y + int(round(ny * frame.workspace_h))
    sx = min(max(sx, frame.origin_x + inset_x), frame.origin_x + frame.workspace_w - inset_x)
    sy = min(max(sy, frame.origin_y + inset_y), frame.origin_y + frame.workspace_h - inset_y)
    return sx, sy


# ---------------------------------------------------------------------------
# Reasoning
# ---------------------------------------------------------------------------
def extract_json_value(text: str) -> Any:
    raw = text.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, count=1)
        raw = re.sub(r"\s*```.*$", "", raw, flags=re.DOTALL)
    decoder = json.JSONDecoder()
    starts = [raw]
    brace = raw.find("{")
    bracket = raw.find("[")
    if brace >= 0:
        starts.append(raw[brace:])
    if bracket >= 0:
        starts.append(raw[bracket:])
    last_error: Exception | None = None
    for candidate in starts:
        try:
            data, end = decoder.raw_decode(candidate.lstrip())
            leftover = candidate.lstrip()[end:].strip()
            if leftover:
                log.info("Ignored extra JSON text: %s", leftover[:160])
            return data
        except json.JSONDecodeError as exc:
            last_error = exc
            continue
    raise ValueError(f"LLM did not return JSON: {text[:400]!r}") from last_error


def parse_decision(data: Any) -> list[dict[str, Any]]:
    thought = ""
    items: list[Any]
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict) and isinstance(data.get("actions"), list):
        thought = str(data.get("thought") or "").strip()
        items = data["actions"]
    elif isinstance(data, dict) and data.get("action"):
        thought = str(data.get("thought") or "").strip()
        items = [data]
    else:
        raise ValueError(f"LLM JSON was not an action or actions list: {data!r}")

    parsed: list[dict[str, Any]] = []
    for item in items[:MAX_ACTIONS_PER_TURN]:
        if not isinstance(item, dict):
            continue
        action = validate_action(item)
        if action["action"] == "wait":
            continue
        if thought and not action.get("thought"):
            action["thought"] = thought
        if parsed and _same_click(parsed[-1], action):
            continue
        parsed.append(action)
    if not parsed:
        raise ValueError("LLM returned no executable actions")
    return parsed


def _same_click(a: dict[str, Any], b: dict[str, Any]) -> bool:
    if a.get("action") != "click" or b.get("action") != "click":
        return False
    return abs(int(a["x"]) - int(b["x"])) <= 8 and abs(int(a["y"]) - int(b["y"])) <= 8


def validate_action(data: dict[str, Any]) -> dict[str, Any]:
    action = data.get("action")
    if action not in ALLOWED_ACTIONS:
        raise ValueError(f"Unknown action {action!r}. Allowed: {sorted(ALLOWED_ACTIONS)}")

    thought = str(data["thought"]).strip() if data.get("thought") else ""

    if action == "click":
        if "x" not in data or "y" not in data:
            raise ValueError("click requires integer x and y")
        result: dict[str, Any] = {"action": "click", "x": int(data["x"]), "y": int(data["y"])}
    elif action == "fill":
        if "x" not in data or "y" not in data or "text" not in data:
            raise ValueError("fill requires x, y, and text")
        result = {"action": "fill", "x": int(data["x"]), "y": int(data["y"]), "text": str(data["text"])}
    elif action == "type":
        if "text" not in data:
            raise ValueError("type requires text")
        result = {"action": "type", "text": str(data["text"])}
    elif action == "press_key":
        if "key" not in data or not str(data["key"]).strip():
            raise ValueError("press_key requires key")
        result = {"action": "press_key", "key": str(data["key"]).strip().lower()}
    elif action == "scroll":
        direction = str(data.get("direction", "down")).lower()
        if direction not in {"up", "down"}:
            direction = "down"
        clicks = int(data.get("clicks", 4))
        clicks = min(max(clicks, 1), 12)
        result = {"action": "scroll", "direction": direction, "clicks": clicks}
    elif action == "wait":
        seconds = float(data.get("seconds", 0.35))
        seconds = min(max(seconds, 0.1), 1.2)
        result = {"action": "wait", "seconds": seconds}
    else:
        result = {"action": "done", "reason": str(data.get("reason", "Task complete"))}

    if thought:
        result["thought"] = thought
    return result


def continue_click(frame: Frame) -> dict[str, Any]:
    return {
        "action": "click",
        "x": int(frame.api_w * 0.55),
        "y": int(frame.api_h * 0.88),
        "thought": "click Continue/Next",
    }


def is_premature_done(action: dict[str, Any], step: int) -> bool:
    if action.get("action") != "done":
        return False
    text = f"{action.get('reason', '')} {action.get('thought', '')}".lower()
    false_done = any(
        word in text
        for word in ("inspect", "viewing", "analysis", "dashboard", "loading", "similarweb")
    )
    success = any(
        word in text
        for word in ("thank", "complete", "finished", "success", "blocked", "password", "2fa", "payment")
    )
    if false_done:
        return True
    if step < 4 and not success:
        return True
    return not success and step < 12


def consecutive_enters(history: list[str]) -> int:
    count = 0
    for row in reversed(history):
        if "press_key" in row and "enter" in row:
            count += 1
        else:
            break
    return count


def sanitize_actions(
    actions: list[dict[str, Any]],
    history: list[str],
    frame: Frame,
    step: int,
) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    for action in actions:
        if action["action"] == "done" and is_premature_done(action, step):
            log.info("Ignoring premature done: %s", action.get("reason") or action.get("thought"))
            continue
        if action["action"] == "press_key" and action.get("key") == "enter" and consecutive_enters(history) >= 2:
            log.info("Ignoring Enter spam; clicking Continue instead.")
            cleaned.append(continue_click(frame))
            continue
        cleaned.append(action)

    if not cleaned:
        cleaned = [continue_click(frame)]

    # Same-choice loop: if we already clicked this image point, go Continue instead.
    last = history[-1] if history else ""
    out: list[dict[str, Any]] = []
    for action in cleaned:
        if (
            action["action"] == "click"
            and last
            and f'"x": {action["x"]}' in last
            and f'"y": {action["y"]}' in last
        ):
            log.info("Same click as last step (%s,%s) — switching to Continue.", action["x"], action["y"])
            out.append(continue_click(frame))
            continue
        out.append(action)
    return out


def frame_fingerprint(frame: Frame) -> bytes:
    image = Image.open(io.BytesIO(frame.jpeg_bytes)).convert("L").resize((48, 27))
    return image.tobytes()


def wait_for_screen_change(
    previous: bytes,
    sidebar_width: int,
    layout_mode: str,
    timeout: float = CHANGE_WAIT,
) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.12)
        nxt = perceive(sidebar_width=sidebar_width, layout_mode=layout_mode)
        if frame_fingerprint(nxt) != previous:
            return True
    return False


def build_user_prompt(task: str, frame: Frame, history: list[str], step: int, max_steps: int) -> str:
    recent = history[-HISTORY_KEEP:]
    history_block = "\n".join(recent) if recent else "(none yet)"
    sidebar_note = ""
    if frame.sidebar_width:
        sidebar_note = (
            f"The right-hand chat sidebar ({frame.sidebar_width}px) is CROPPED OUT.\n"
            f"Physical workspace size: {frame.workspace_w} x {frame.workspace_h} at ({frame.origin_x}, {frame.origin_y})\n"
        )
    return (
        f"Task: {task}\n"
        f"Step: {step}/{max_steps}\n"
        f"{sidebar_note}"
        f"Image size (use these coordinates): {frame.api_w} x {frame.api_h}\n"
        f"Previous actions:\n{history_block}\n"
        "Return JSON: click the visible answer, then click Continue. Coordinates must fit the image."
    )


class LLMClient:
    def decide(self, task: str, frame: Frame, history: list[str], step: int, max_steps: int) -> list[dict[str, Any]]:
        raise NotImplementedError


class GeminiClient(LLMClient):
    def __init__(self, api_key: str, model: str) -> None:
        from google import genai
        from google.genai import types

        self._types = types
        self._model = model
        self._client = genai.Client(api_key=api_key)
        self._models = self._model_queue(model)

    @staticmethod
    def _model_queue(preferred: str) -> list[str]:
        ordered = [preferred]
        for name in GEMINI_MODEL_FALLBACKS:
            if name not in ordered:
                ordered.append(name)
        return ordered

    def decide(self, task: str, frame: Frame, history: list[str], step: int, max_steps: int) -> list[dict[str, Any]]:
        types = self._types
        prompt = build_user_prompt(task, frame, history, step, max_steps)
        last_error: Exception | None = None
        for model in list(self._models):
            for attempt in range(1, LLM_RETRIES + 1):
                try:
                    response = self._client.models.generate_content(
                        model=model,
                        contents=[
                            types.Part.from_bytes(data=frame.jpeg_bytes, mime_type="image/jpeg"),
                            prompt,
                        ],
                        config=self._gen_config(types),
                    )
                    text = (response.text or "").strip()
                    if not text:
                        raise ValueError("Gemini returned an empty response")
                    if model != self._model:
                        log.info("Switched Gemini model to %s", model)
                        self._model = model
                        self._models = self._model_queue(model)
                    return parse_decision(extract_json_value(text))
                except Exception as exc:
                    last_error = exc
                    message = str(exc).lower()
                    if "404" in message or "not_found" in message or "no longer available" in message:
                        log.warning("%s is not available on this key. Trying next model.", model)
                        break
                    retryable = any(
                        token in message
                        for token in ("503", "429", "unavailable", "high demand", "resource_exhausted", "overloaded")
                    )
                    if not retryable or attempt >= LLM_RETRIES:
                        raise
                    delay = 8 * attempt if "429" in message or "resource_exhausted" in message else 2 * attempt
                    log.warning("Gemini busy (%s). Retry %s/%s in %ss.", exc, attempt, LLM_RETRIES, delay)
                    time.sleep(delay)
        raise last_error or RuntimeError("Gemini call failed")

    def _gen_config(self, types: Any) -> Any:
        kwargs: dict[str, Any] = {
            "system_instruction": SYSTEM_PROMPT,
            "max_output_tokens": MAX_OUTPUT_TOKENS,
            "response_mime_type": "application/json",
            "automatic_function_calling": types.AutomaticFunctionCallingConfig(disable=True),
        }
        thinking = None
        try:
            thinking = types.ThinkingConfig(thinking_level=types.ThinkingLevel.MINIMAL)
        except Exception:
            thinking = None
        if thinking is not None:
            kwargs["thinking_config"] = thinking
        return types.GenerateContentConfig(**kwargs)


class OpenAIClient(LLMClient):
    def __init__(self, api_key: str, model: str) -> None:
        from openai import OpenAI

        self._model = model
        self._client = OpenAI(api_key=api_key)

    def decide(self, task: str, frame: Frame, history: list[str], step: int, max_steps: int) -> list[dict[str, Any]]:
        prompt = build_user_prompt(task, frame, history, step, max_steps)
        b64 = base64.b64encode(frame.jpeg_bytes).decode("ascii")
        response = self._client.chat.completions.create(
            model=self._model,
            temperature=0.1,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{b64}",
                                "detail": "low",
                            },
                        },
                    ],
                },
            ],
        )
        text = (response.choices[0].message.content or "").strip()
        if not text:
            raise ValueError("OpenAI returned an empty response")
        return parse_decision(extract_json_value(text))


def build_llm_client(provider: str) -> LLMClient:
    provider = provider.lower().strip()
    if provider == "gemini":
        api_key = (
            os.getenv("GEMINI_API_KEY")
            or os.getenv("GOOGLE_API_KEY")
            or os.getenv("GOOGLE_GENAI_API_KEY")
        )
        if not api_key:
            raise RuntimeError(
                "Missing Gemini API key. Set GEMINI_API_KEY or GOOGLE_API_KEY "
                "(see .env.example)."
            )
        model = os.getenv("GEMINI_MODEL", DEFAULT_GEMINI_MODEL)
        log.info("Using Gemini model %s", model)
        return GeminiClient(api_key=api_key, model=model)

    if provider == "openai":
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("Missing OpenAI API key. Set OPENAI_API_KEY (see .env.example).")
        model = os.getenv("OPENAI_MODEL", "gpt-4o")
        return OpenAIClient(api_key=api_key, model=model)

    raise RuntimeError(f"Unsupported LLM_PROVIDER={provider!r}. Use 'gemini' or 'openai'.")


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------
def split_hotkey(key: str) -> list[str]:
    parts = [p.strip() for p in key.replace(" ", "").split("+") if p.strip()]
    aliases = {"windows": "win", "return": "enter", "escape": "esc", "control": "ctrl", "cmd": "win"}
    return [aliases.get(p, p) for p in parts]


def _focus_browser() -> None:
    try:
        from layout import focus_browser

        focus_browser()
    except Exception:
        pass


def _click_at(x: int, y: int, kind: str = "click") -> None:
    try:
        from overlay import show_target

        show_target(x, y, kind=kind)
    except Exception:
        pass
    pyautogui.moveTo(x, y, duration=MOVE_DURATION)
    pyautogui.click(x, y)
    time.sleep(FOCUS_SETTLE)


def _type_text(text: str) -> None:
    pyautogui.write(text, interval=TYPE_INTERVAL)
    time.sleep(POST_TYPE_DELAY)


def execute(action: dict[str, Any], frame: Frame) -> str:
    name = action["action"]

    if name == "click":
        x, y = map_to_screen(action["x"], action["y"], frame)
        log.info("Executing click at screen (%s, %s)  [image (%s, %s)]", x, y, action["x"], action["y"])
        _click_at(x, y)
        return f"clicked screen ({x}, {y})"

    if name == "fill":
        x, y = map_to_screen(action["x"], action["y"], frame)
        text = action["text"]
        preview = text if len(text) <= 80 else text[:77] + "..."
        log.info("Executing fill at (%s, %s) %r", x, y, preview)
        _click_at(x, y, kind="fill")
        pyautogui.hotkey("ctrl", "a")
        time.sleep(0.03)
        _type_text(text)
        return f"filled {preview!r} at ({x}, {y})"

    if name == "type":
        text = action["text"]
        preview = text if len(text) <= 80 else text[:77] + "..."
        log.info("Executing type %r", preview)
        _type_text(text)
        return f"typed {preview!r}"

    if name == "press_key":
        keys = split_hotkey(action["key"])
        log.info("Executing press_key %s", "+".join(keys))
        if len(keys) == 1:
            pyautogui.press(keys[0])
        else:
            pyautogui.hotkey(*keys)
        return f"pressed {'+'.join(keys)}"

    if name == "scroll":
        direction = action["direction"]
        clicks = int(action["clicks"])
        delta = -clicks if direction == "down" else clicks
        log.info("Executing scroll %s (%s)", direction, clicks)
        pyautogui.moveTo(
            frame.origin_x + max(frame.workspace_w // 2, 1),
            frame.origin_y + max(frame.workspace_h // 2, 1),
        )
        pyautogui.scroll(delta)
        return f"scrolled {direction} {clicks}"

    if name == "wait":
        seconds = action["seconds"]
        log.info("Executing wait %.2fs", seconds)
        time.sleep(seconds)
        return f"waited {seconds:.2f}s"

    reason = action.get("reason", "")
    log.info("Agent finished: %s", reason)
    return f"done ({reason})"


def format_decision(action: dict[str, Any]) -> str:
    if action.get("thought"):
        return str(action["thought"])
    name = action["action"]
    if name == "click":
        return f"click  x={action['x']} y={action['y']}"
    if name == "fill":
        text = action["text"]
        preview = text if len(text) <= 80 else text[:77] + "..."
        return f"fill  {preview!r}"
    if name == "type":
        text = action["text"]
        preview = text if len(text) <= 80 else text[:77] + "..."
        return f"type   {preview!r}"
    if name == "press_key":
        return f"press_key  {action['key']}"
    if name == "scroll":
        return f"scroll {action.get('direction', 'down')}"
    if name == "wait":
        return f"wait   {action['seconds']}s"
    return f"done   {action.get('reason', '')}"


# ---------------------------------------------------------------------------
# Loop
# ---------------------------------------------------------------------------
def countdown(seconds: int) -> None:
    log.info("Starting in %ss. Move the mouse to a screen corner to abort (FAILSAFE).", seconds)
    for remaining in range(seconds, 0, -1):
        log.info("  %s...", remaining)
        time.sleep(1)


def run_agent(
    task: str,
    llm: LLMClient,
    max_steps: int = DEFAULT_MAX_STEPS,
    sidebar_width: int = 0,
    layout_mode: str = "float",
    should_abort: Callable[[], bool] | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> str:
    """Run the vision/action loop. Returns done | aborted | error | max_steps."""
    configure_pyautogui()
    history: list[str] = []

    def emit(event: dict[str, Any]) -> None:
        if on_event is not None:
            on_event(event)

    def aborted() -> bool:
        return bool(should_abort and should_abort())

    emit({"type": "status", "text": "Starting task execution..."})
    try:
        from overlay import ensure_started, show_browser_frame

        ensure_started()
        show_browser_frame()
    except Exception:
        pass
    _focus_browser()
    time.sleep(0.12)

    for step in range(1, max_steps + 1):
        if aborted():
            emit({"type": "aborted", "text": "Stopped by user."})
            return "aborted"

        log.info("---------- step %s/%s ----------", step, max_steps)
        emit({
            "type": "step",
            "step": step,
            "max": max_steps,
            "text": f"Analyzing screen (Step {step}/{max_steps})...",
        })
        try:
            from overlay import show_browser_frame

            show_browser_frame()
        except Exception:
            pass

        frame = perceive(sidebar_width=sidebar_width, layout_mode=layout_mode)
        log.info(
            "Workspace %sx%s origin (%s,%s) mode=%s crop %spx -> API %sx%s (%s KB)",
            frame.workspace_w,
            frame.workspace_h,
            frame.origin_x,
            frame.origin_y,
            frame.layout_mode,
            frame.sidebar_width,
            frame.api_w,
            frame.api_h,
            max(1, len(frame.jpeg_bytes) // 1024),
        )

        try:
            actions = llm.decide(task, frame, history, step, max_steps)
            actions = sanitize_actions(actions, history, frame, step)
            if len(actions) == 1 and actions[0]["action"] == "click":
                thought = (actions[0].get("thought") or "").lower()
                if not any(word in thought for word in ("continue", "next", "submit")):
                    actions.append(continue_click(frame))
        except Exception as exc:
            log.error("LLM call failed: %s", exc)
            emit({"type": "error", "text": f"Error: {exc}"})
            return "error"

        if aborted():
            emit({"type": "aborted", "text": "Stopped by user."})
            return "aborted"

        payload = [{"action": a.get("action"), "x": a.get("x"), "y": a.get("y"), "key": a.get("key")} for a in actions]
        log.info("Decision JSON: %s", json.dumps(payload, ensure_ascii=False))
        summary = actions[0].get("thought") or ", ".join(format_decision(a) for a in actions)
        log.info("Decision (%s actions): %s", len(actions), summary)
        emit({
            "type": "log",
            "step": step,
            "text": f"Step {step}: {summary}",
            "action": actions[0],
        })

        before = frame_fingerprint(frame)
        advanced = False
        for index, action in enumerate(actions):
            if aborted():
                emit({"type": "aborted", "text": "Stopped by user."})
                return "aborted"

            if index > 0:
                emit({
                    "type": "log",
                    "step": step,
                    "text": f"  → {format_decision(action)}",
                    "action": action,
                })

            if action["action"] == "done":
                reason = action.get("reason", "Task finished!")
                execute(action, frame)
                emit({"type": "done", "text": reason})
                log.info("Completed in %s step(s).", step)
                return "done"

            try:
                result = execute(action, frame)
            except pyautogui.FailSafeException:
                log.error("FAILSAFE triggered (mouse in a screen corner). Aborting.")
                emit({"type": "error", "text": "FAILSAFE: mouse moved to a screen corner. Stopped."})
                return "aborted"

            history.append(f"{step}.{index + 1} {json.dumps(action, ensure_ascii=False)} -> {result}")
            if action["action"] in {"click", "fill"}:
                advanced = True
            if index < len(actions) - 1:
                time.sleep(CHAIN_GAP)

        if advanced:
            changed = wait_for_screen_change(before, sidebar_width, layout_mode)
            if not changed:
                log.info("Screen did not change after clicks; Continue fallback next step.")
                history.append(f"{step}.screen unchanged")

    log.warning("Hit max step limit (%s) without done.", max_steps)
    emit({"type": "done", "text": f"Stopped after {max_steps} steps without finishing."})
    return "max_steps"


def run_loop(task: str, llm: LLMClient, max_steps: int, countdown_s: int) -> int:
    countdown(countdown_s)
    status = run_agent(task, llm, max_steps=max_steps, sidebar_width=0)
    return 0 if status == "done" else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Vision + Action Loop desktop agent")
    parser.add_argument("task", nargs="+", help="Natural-language task for the agent")
    parser.add_argument(
        "--provider",
        default=os.getenv("LLM_PROVIDER", "gemini"),
        choices=("gemini", "openai"),
        help="Multimodal LLM provider (default: gemini, or LLM_PROVIDER)",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=int(os.getenv("MAX_STEPS", DEFAULT_MAX_STEPS)),
        help=f"Abort after this many actions (default: {DEFAULT_MAX_STEPS})",
    )
    parser.add_argument(
        "--countdown",
        type=int,
        default=DEFAULT_COUNTDOWN,
        help="Seconds to wait before the first screenshot (default: 5)",
    )
    return parser.parse_args()


def main() -> int:
    load_env()
    setup_logging()
    args = parse_args()
    task = " ".join(args.task).strip()
    if not task:
        log.error("Task is empty.")
        return 1

    try:
        llm = build_llm_client(args.provider)
    except RuntimeError as exc:
        log.error("%s", exc)
        return 1
    log.info("Provider: %s", args.provider)
    log.info("Task: %s", task)
    log.info("FAILSAFE is ON. pyautogui.PAUSE = 0.02s")
    return run_loop(task, llm, max_steps=args.max_steps, countdown_s=args.countdown)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        raise SystemExit(130)
