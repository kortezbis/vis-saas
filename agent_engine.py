"""Single-phase workspace copilot: screenshot → Gemini JSON plan → execute → repeat."""

from __future__ import annotations

import ctypes
import io
import logging
import math
import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

log = logging.getLogger("agent_engine")

SIDEBAR_WIDTH = 380
MOVE_DURATION = 0.08
TYPE_INTERVAL = 0.01
FOCUS_AFTER_CLICK = 0.12
POST_TYPE_DELAY = 0.08
POST_KEY_DELAY = 0.08
DROPDOWN_SETTLE = 0.18
BOX_SCALE = 1000.0
CLICK_POSITION = 0.5
MAX_ACTIONS = 64
MAX_ITERATIONS = 200
MAX_CONSECUTIVE_DUPES = 2
MAX_UNCHANGED_FRAMES = 12
FORCE_ADVANCE_AFTER = 2
PAGE_SETTLE = 0.8
MISSING_BOX_WAIT = 0.15
MIN_STATE_CONFIDENCE = 0.80
LLM_RETRIES = 4
DEFAULT_MODEL = "gemini-3.5-flash-lite"
MODEL_FALLBACKS = ("gemini-3.5-flash-lite", "gemini-3.6-flash", "gemini-2.0-flash")

ActionName = Literal["click", "type", "press_key", "wait", "done"]
ActionTarget = Literal[
    "answer",
    "choice",
    "dropdown",
    "option",
    "submit",
    "advance",
    "navigation",
    "template",
    "wait",
    "done",
    "unknown",
]
QuestionState = Literal[
    "unsolved",
    "answered",
    "graded_correct",
    "graded_incorrect",
    "locked",
    "loading",
    "complete",
    "blocked",
    "unknown",
]
ModeName = Literal["auto"]

INPUT_TARGETS = {"answer", "choice", "dropdown", "option"}
PROTECTED_STATES = {"graded_correct", "graded_incorrect", "locked"}
RECHECK_STATES = {"unknown", "loading"}


# ---------------------------------------------------------------------------
# 1. STRICT JSON SCHEMA (Pydantic → Gemini structured output)
# ---------------------------------------------------------------------------
def _normalize_box(value: Any) -> list[int] | None:
    """Coerce and validate a normalized [ymin, xmin, ymax, xmax] box."""
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError("box_2d must contain exactly four coordinates")

    try:
        raw = [float(item) for item in value]
    except (TypeError, ValueError) as exc:
        raise ValueError("box_2d coordinates must be numeric") from exc

    if not all(math.isfinite(item) for item in raw):
        raise ValueError("box_2d coordinates must be finite")

    box = [int(round(item)) for item in raw]
    if any(item < 0 or item > int(BOX_SCALE) for item in box):
        raise ValueError("box_2d coordinates must be within 0-1000")

    ymin, xmin, ymax, xmax = box
    if ymin >= ymax or xmin >= xmax:
        raise ValueError("box_2d must have positive height and width")
    return box


class Action(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: ActionName = Field(
        description="Action type: 'click', 'type', 'press_key', 'wait', or 'done'"
    )
    target: ActionTarget = Field(
        default="unknown",
        description="Semantic target: answer, choice, dropdown, option, submit, advance, navigation, template, wait, or done.",
    )
    box_2d: list[int] | None = Field(
        default=None,
        description="[ymin, xmin, ymax, xmax] on a 0-1000 scale. Required for click.",
    )
    text: str | None = Field(default=None, description="Text to type if action is 'type'")
    key: str | None = Field(
        default=None,
        description="Key to press if action is 'press_key' (e.g. 'tab', 'right', 'enter')",
    )
    seconds: float | None = Field(
        default=None,
        ge=0.05,
        le=15.0,
        description="Seconds to wait if action is 'wait' (0.05-15).",
    )
    reason: str | None = Field(default=None, description="Completion reason if action is 'done'")
    label: str | None = Field(default=None, description="Short label shown in sidebar")

    @field_validator("action", mode="before")
    @classmethod
    def normalize_action(cls, value: Any) -> str:
        return str(value).strip().lower()

    @field_validator("target", mode="before")
    @classmethod
    def normalize_target(cls, value: Any) -> str:
        target = str(value or "unknown").strip().lower().replace("-", "_")
        aliases = {
            "input": "answer",
            "text": "answer",
            "field": "answer",
            "radio": "choice",
            "checkbox": "choice",
            "select": "dropdown",
            "menu": "dropdown",
            "menu_option": "option",
            "dropdown_option": "option",
            "check": "submit",
            "submit_answer": "submit",
            "next": "advance",
            "continue": "advance",
            "right_arrow": "advance",
            "key": "navigation",
        }
        return aliases.get(target, target)

    @field_validator("box_2d", mode="before")
    @classmethod
    def coerce_box(cls, value: Any) -> list[int] | None:
        return _normalize_box(value)

    @field_validator("key", mode="before")
    @classmethod
    def normalize_key(cls, value: Any) -> str | None:
        if value is None or value == "":
            return None
        key = str(value).strip().lower()
        aliases = {
            "arrowleft": "left",
            "arrowright": "right",
            "arrowup": "up",
            "arrowdown": "down",
            "return": "enter",
            "spacebar": "space",
        }
        return aliases.get(key, key)

    @model_validator(mode="after")
    def require_fields(self) -> Action:
        if self.action == "click" and self.box_2d is None:
            raise ValueError("click requires box_2d")
        if self.action in {"click", "type", "press_key"} and self.target == "unknown":
            raise ValueError(f"{self.action} requires a semantic target")
        if self.action == "type" and (self.text is None or not self.text.strip()):
            raise ValueError("type requires text")
        if self.action == "press_key" and not self.key:
            raise ValueError("press_key requires key")
        if self.action == "wait" and self.seconds is None:
            self.seconds = 1.0
        if self.action == "wait" and self.target == "unknown":
            self.target = "wait"
        if self.action == "done" and self.target == "unknown":
            self.target = "done"
        return self


class ExecutionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_summary: str = Field(description="Brief summary of what action is being taken")
    question_state: QuestionState = Field(
        description="First-screenshot state: unsolved, answered, graded_correct, graded_incorrect, locked, loading, complete, blocked, or unknown.",
    )
    state_confidence: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Confidence in question_state from the visible screenshot, from 0 to 1.",
    )
    is_complete: bool = Field(
        default=False,
        description="True if the entire assignment or homework set is finished",
    )
    actions: list[Action] = Field(
        default_factory=list,
        description="Sequence of actions to execute on the screen",
    )

    @model_validator(mode="after")
    def require_work_or_completion(self) -> ExecutionPlan:
        if not self.actions and not self.is_complete:
            raise ValueError("a non-complete plan requires at least one action")
        if self.is_complete and any(item.action != "done" for item in self.actions):
            raise ValueError("a complete plan may contain only a done action")
        if self.question_state == "complete" and not self.is_complete:
            raise ValueError("complete question_state requires is_complete=true")
        if self.is_complete and self.question_state != "complete":
            raise ValueError("is_complete=true requires question_state=complete")

        if self.question_state in RECHECK_STATES:
            if any(item.action not in {"wait", "done"} for item in self.actions):
                raise ValueError("unknown or loading state may only wait or finish")

        if self.question_state == "blocked":
            if any(item.action not in {"wait", "done"} for item in self.actions):
                raise ValueError("blocked state may only wait or finish")

        if self.question_state in PROTECTED_STATES:
            for item in self.actions:
                if item.action == "type" or item.target in INPUT_TARGETS:
                    raise ValueError("graded or locked state cannot edit an answer target")
                if item.action == "click" and item.target not in {"advance", "navigation", "submit"}:
                    raise ValueError("graded or locked state may only navigate or advance")

        if self.question_state == "answered":
            for item in self.actions:
                if item.action == "type" or item.target in INPUT_TARGETS:
                    raise ValueError("answered state cannot edit an answer target")
                if item.action == "click" and item.target not in {"advance", "navigation", "submit"}:
                    raise ValueError("answered state may only submit or advance")
        return self


SYSTEM_PROMPT = """
You are Viszmo, a high-speed autonomous multimodal educational UI automation agent.
Work directly on the visible assignment in the browser workspace and complete it without asking the user to press controls.

SUBJECT ADAPTATION:
- Infer the discipline from the visible prompt, passage, diagram, table, notation, and answer controls. Never rely on a user-selected subject or a fixed subject branch.
- For mathematics, physics, chemistry, statistics, and other quantitative science, calculate internally with full precision; verify signs, formulas, units, significant figures, scientific notation, fractions, exponents, and requested rounding.
- For English, literature, history, government, civics, and other humanities, read all visible source material and use textual, historical, or contextual evidence to choose or write the answer.
- For vocabulary, matching, true/false, dropdowns, radio buttons, checkboxes, short answers, and mixed assignments, adapt to the controls visible on the page.

STATE AND PROGRESSION:
1. FIRST classify the screenshot as exactly one question_state: unsolved, answered, graded_correct, graded_incorrect, locked, loading, complete, blocked, or unknown. Give state_confidence from 0 to 1.
2. Treat visible answer text/selections, highlighted/selected choices, green checks, red corrections, disabled fields, lock icons, Correct/Submitted/Graded text, score summaries, and thank-you/completion screens as authoritative state indicators. Indicators may appear beside the question on the left side of the page.
3. If question_state is answered, do not click or type any answer/choice/dropdown/option target. Only click Submit/Check, Next/Continue/right-arrow, or wait for validation.
4. If question_state is graded_correct, graded_incorrect, or locked, do not edit anything. Only advance/navigate or wait. Never re-solve a graded or already-finished question.
5. If question_state is loading or unknown, return only a short wait and re-check the same screenshot state. Never guess into an uncertain screen.
6. If unsolved, answer every required visible field in visual order, using one click/type sequence for each independent field. Then click Check Answer, Submit, Save, or the platform's validation control. If those buttons are visible, include them in THIS same plan after the answer clicks.
7. After a choice is already selected/highlighted, do not click it again. Submit if needed, then advance with Next, Continue, or the right arrow. Keep going through every remaining question.
8. is_complete and question_state=complete are ONLY for the entire assignment: a score/thank-you/submitted-all screen, and no Next/Continue/unanswered item. One finished, correct, or already-selected question is answered or graded_correct — click Next immediately. Never stop after a single question.

ANSWER FORMATTING:
- Match the active UI template exactly. For math editors use the platform's syntax such as '/' for fractions and '^' for exponents; use an explicit press_key action (usually right or tab) to leave a fraction/exponent template when needed.
- Do not send math-template navigation keys in ordinary text fields. Preserve punctuation, units, capitalization, and requested answer format for non-math responses.
- For radio buttons, checkboxes, and multiple-choice answers, use a click with target choice and then validate/advance.
- For dropdowns, use target dropdown on the closed control. For a custom menu, click the exact visible target option with target option. For a native select, click it, type the exact visible option label, then press Enter with target dropdown. Never type a dropdown answer into an unrelated text field.

OUTPUT CONTRACT:
- Return only one JSON object matching the supplied ExecutionPlan schema. No markdown, explanations, derivations, chain-of-thought, or conversational text.
- plan_summary must be a short operational label, not an explanation.
- Coordinates are precise [ymin, xmin, ymax, xmax] boxes on a 0-1000 scale relative to the supplied workspace screenshot, whose copilot sidebar is cropped out. Use a separate box and action sequence for every independent input/control.
- Every click/type/press_key action requires a semantic target: answer, choice, dropdown, option, submit, advance, navigation, or template. click requires box_2d. type enters only the answer text after the preceding click. press_key sends one navigation/validation key. wait pauses briefly. done is only for a visibly finished or blocked state.
""".strip()

# Compatibility aliases for callers that imported the old mode-specific names.
MATH_SYSTEM_PROMPT = SYSTEM_PROMPT
GENERAL_SYSTEM_PROMPT = SYSTEM_PROMPT

UNIVERSAL_GOAL = (
    "Infer the subject from the visible assignment, skip any question that is already answered or graded, "
    "solve each remaining unsolved question, submit it, click Next/Continue, and stop only when the "
    "entire assignment is finished — never after a single question."
)
DEFAULT_GOAL = UNIVERSAL_GOAL
MATH_GOAL = UNIVERSAL_GOAL
GENERAL_GOAL = UNIVERSAL_GOAL

USER_TURN = """Analyze the current workspace screenshot and produce the next executable plan.
First classify question_state and state_confidence from the visible answer/status indicators.
If the question is answered, graded, locked, loading, or uncertain, do not edit answer targets.
Skip filled, graded, locked, selected, or checkmarked fields. If the active question is already solved or finished, click Next/Continue immediately.
A finished question is not the end of the assignment. Stop only when the whole assignment is done.
Image size: {api_w} x {api_h} (native PNG, sidebar cropped).
Prior execution guard: {guard}
Goal: {goal}
"""


def normalize_mode(value: str | None) -> ModeName:
    """Return the only supported mode; kept for backwards-compatible callers."""
    return "auto"


def prompt_for(mode: ModeName) -> str:
    return SYSTEM_PROMPT


def goal_for(mode: ModeName | str | None = None, notes: str = "") -> str:
    if notes.strip():
        return notes.strip()
    return UNIVERSAL_GOAL


# ---------------------------------------------------------------------------
# Environment / safety
# ---------------------------------------------------------------------------
def enable_dpi_awareness() -> None:
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


enable_dpi_awareness()

from PIL import Image, ImageGrab  # noqa: E402
import pyautogui  # noqa: E402


@dataclass(frozen=True)
class Workspace:
    image_bytes: bytes
    mime_type: str
    api_w: int
    api_h: int
    origin_x: int
    origin_y: int
    width: int
    height: int


def load_env() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    env_path = Path(__file__).resolve().parent / ".env"
    load_dotenv(env_path if env_path.exists() else None)


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def configure_pyautogui() -> None:
    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.02
    pyautogui.MINIMUM_DURATION = 0.0


def work_area() -> tuple[int, int, int, int]:
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
        if ctypes.windll.user32.SystemParametersInfoW(48, 0, ctypes.byref(rect), 0):
            return int(rect.left), int(rect.top), int(rect.right - rect.left), int(rect.bottom - rect.top)
    except Exception:
        pass
    return 0, 0, screen_w, screen_h


def capture_high_res_workspace(sidebar_width: int = SIDEBAR_WIDTH) -> Workspace:
    """Lossless PNG of the monitor that holds the assignment, minus the Viszmo overlay."""
    left, top, width, height = work_area()
    crop = max(int(sidebar_width), 0)
    width = max(width - crop, 1)
    tab_label = "workspace"
    try:
        import layout as layout_mod

        region = layout_mod.assignment_workspace_rect(sidebar_width)
        if region is not None and region.width > 80 and region.height > 80:
            left, top, width, height = region.left, region.top, region.width, region.height
        tab = layout_mod.connected_tab()
        if tab.get("connected"):
            tab_label = str(tab.get("label") or tab.get("title") or "workspace")
    except Exception as exc:
        log.warning("Could not lock capture to the connected tab: %s", exc)
    log.info("Capturing %r at (%s,%s) %sx%s", tab_label, left, top, width, height)
    bbox = (int(left), int(top), int(left + width), int(top + height))
    try:
        image = ImageGrab.grab(bbox=bbox, all_screens=True)
    except TypeError:
        image = ImageGrab.grab(bbox=bbox)
    except Exception as exc:
        log.warning("Multi-monitor grab failed (%s); retrying primary work area.", exc)
        left, top, width, height = work_area()
        width = max(width - crop, 1)
        bbox = (left, top, left + width, top + height)
        image = ImageGrab.grab(bbox=bbox)
    if image.mode not in {"RGB", "RGBA"}:
        image = image.convert("RGB")
    if min(image.size) < 80:
        log.warning("Capture was too small (%sx%s); using primary work area.", *image.size)
        left, top, width, height = work_area()
        width = max(width - crop, 1)
        bbox = (left, top, left + width, top + height)
        image = ImageGrab.grab(bbox=bbox, all_screens=True)
        if image.mode not in {"RGB", "RGBA"}:
            image = image.convert("RGB")

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return Workspace(
        image_bytes=buffer.getvalue(),
        mime_type="image/png",
        api_w=image.size[0],
        api_h=image.size[1],
        origin_x=left,
        origin_y=top,
        width=width,
        height=height,
    )


capture_workspace = capture_high_res_workspace


def valid_box_2d(box: Any) -> list[int] | None:
    """Return a 4-int box or None if missing/invalid. Never raises."""
    try:
        return _normalize_box(box)
    except (TypeError, ValueError):
        return None


def action_signature(action: dict[str, Any]) -> tuple[Any, ...]:
    """Stable (action, target, text, box_2d) signature for loop detection."""
    name = str(action.get("action") or "").strip().lower()
    target = str(action.get("target") or "unknown").strip().lower()
    text = str(action.get("text") or action.get("key") or "")
    box = valid_box_2d(action.get("box_2d"))
    box_t = tuple(box) if box is not None else None
    return (name, target, text, box_t)


def box_to_coords(box_2d: list[int], workspace: Workspace) -> tuple[int, int] | None:
    """Map a normalized target box to its physical center point."""
    box = valid_box_2d(box_2d)
    if box is None:
        return None
    ymin, xmin, ymax, xmax = (float(v) for v in box)
    target_x = workspace.origin_x + int((xmin + (xmax - xmin) * CLICK_POSITION) / BOX_SCALE * workspace.width)
    target_y = workspace.origin_y + int(((ymin + ymax) / 2.0) / BOX_SCALE * workspace.height)
    inset_x = max(int(workspace.width * 0.005), 3)
    inset_y = max(int(workspace.height * 0.005), 3)
    target_x = min(max(target_x, workspace.origin_x + inset_x), workspace.origin_x + workspace.width - inset_x)
    target_y = min(max(target_y, workspace.origin_y + inset_y), workspace.origin_y + workspace.height - inset_y)
    return target_x, target_y


# ---------------------------------------------------------------------------
# Gemini client
# ---------------------------------------------------------------------------
def _api_key() -> str:
    key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or os.getenv("GOOGLE_GENAI_API_KEY")
    if not key:
        raise RuntimeError("Missing GEMINI_API_KEY (see .env.example).")
    return key


def _model_queue() -> list[str]:
    preferred = os.getenv("GEMINI_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    models: list[str] = []
    for name in (preferred, *MODEL_FALLBACKS):
        if name not in models:
            models.append(name)
    return models


def _thinking_config(types: Any, level: str = "MINIMAL") -> Any | None:
    try:
        thinking_level = getattr(types.ThinkingLevel, level, None)
        if thinking_level is None:
            return None
        return types.ThinkingConfig(thinking_level=thinking_level)
    except Exception:
        return None


def _afc_disabled(types: Any) -> Any | None:
    try:
        return types.AutomaticFunctionCallingConfig(disable=True)
    except Exception:
        return None


def _config_kwargs(types: Any, **extra: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = dict(extra)
    afc = _afc_disabled(types)
    if afc is not None:
        kwargs["automatic_function_calling"] = afc
    return kwargs


def workspace_fingerprint(workspace: Workspace) -> bytes:
    image = Image.open(io.BytesIO(workspace.image_bytes)).convert("L").resize((64, 36))
    return image.tobytes()


def _is_unavailable(exc: Exception) -> bool:
    message = str(exc).lower()
    return "404" in message or "not_found" in message or "no longer available" in message


def _retry_delay(exc: Exception, attempt: int) -> float | None:
    message = str(exc).lower()
    retryable = any(
        token in message
        for token in ("503", "429", "unavailable", "high demand", "resource_exhausted", "overloaded")
    )
    if not retryable:
        return None
    if "429" in message or "resource_exhausted" in message:
        return 8 * attempt
    return 2 * attempt


def _generate(client: Any, types: Any, model: str, contents: list[Any], config: Any) -> Any:
    last_error: Exception | None = None
    for attempt in range(1, LLM_RETRIES + 1):
        try:
            response = client.models.generate_content(model=model, contents=contents, config=config)
            text = (response.text or "").strip()
            if not text:
                raise ValueError("Gemini returned an empty response")
            return response
        except Exception as exc:
            last_error = exc
            if _is_unavailable(exc):
                raise
            delay = _retry_delay(exc, attempt)
            if delay is None or attempt >= LLM_RETRIES:
                raise
            log.warning("Gemini busy (%s). Retry %s/%s in %ss.", exc, attempt, LLM_RETRIES, delay)
            time.sleep(delay)
    raise last_error or RuntimeError("Gemini call failed")


def _json_schema() -> dict[str, Any]:
    """Gemini rejects Pydantic's additionalProperties field on response_schema."""
    schema = ExecutionPlan.model_json_schema()

    def strip(node: Any) -> Any:
        if isinstance(node, dict):
            return {
                key: strip(value)
                for key, value in node.items()
                if key not in {"additionalProperties", "additional_properties"}
            }
        if isinstance(node, list):
            return [strip(item) for item in node]
        return node

    return strip(schema)


def _parse_plan(response: Any) -> ExecutionPlan:
    parsed = getattr(response, "parsed", None)
    if isinstance(parsed, ExecutionPlan):
        return parsed
    if parsed is not None:
        try:
            return ExecutionPlan.model_validate(parsed)
        except Exception:
            pass
    text = (getattr(response, "text", None) or "").strip()
    return ExecutionPlan.model_validate_json(text)


def decide(
    client: Any,
    types: Any,
    workspace: Workspace,
    goal: str,
    models: list[str],
    mode: str | None = None,
    guard: str = "none",
) -> tuple[str, ExecutionPlan]:
    image_part = types.Part.from_bytes(data=workspace.image_bytes, mime_type=workspace.mime_type)
    prompt = (
        USER_TURN.replace("{api_w}", str(workspace.api_w))
        .replace("{api_h}", str(workspace.api_h))
        .replace("{guard}", guard)
        .replace("{goal}", goal)
    )
    thinking = _thinking_config(types, "MINIMAL")
    last_error: Exception | None = None
    for model in models:
        try:
            config_kwargs = _config_kwargs(
                types,
                system_instruction=SYSTEM_PROMPT,
                response_mime_type="application/json",
                response_json_schema=_json_schema(),
                temperature=0.0,
                max_output_tokens=4096,
            )
            if thinking is not None:
                config_kwargs["thinking_config"] = thinking
            response = _generate(
                client,
                types,
                model,
                [image_part, prompt],
                types.GenerateContentConfig(**config_kwargs),
            )
            plan = _parse_plan(response)
            if not plan.actions and not plan.is_complete:
                raise ValueError("Model returned no actions and is_complete is false")
            log.info("Plan (%s): %s (%s actions, complete=%s)", model, plan.plan_summary, len(plan.actions), plan.is_complete)
            return model, plan
        except Exception as exc:
            last_error = exc
            if _is_unavailable(exc) or isinstance(exc, (ValueError, ValidationError)):
                log.warning("Plan failed on %s (%s). Trying next model.", model, exc)
                continue
            raise
    raise last_error or RuntimeError("Gemini plan failed")


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------
ADVANCE_GUARD = (
    "The screenshot did not change. This question is already selected, submitted, graded, or finished. "
    "Do not click the same choice/answer again. Click Submit/Check only if it is still enabled; "
    "otherwise click Next, Continue, or the right-arrow immediately. "
    "is_complete=true only if the entire assignment is finished (no Next and a completion/score screen)."
)


def _advance_fallback() -> dict[str, Any]:
    return Action(
        action="press_key",
        target="advance",
        key="right",
        label="Next question",
    ).model_dump()


def _state_recheck_action() -> dict[str, Any]:
    return Action(
        action="wait",
        target="wait",
        seconds=0.35,
        label="Rechecking question state",
    ).model_dump()


def gated_actions(plan: ExecutionPlan, post_submit_lock: bool = False) -> list[dict[str, Any]]:
    """Apply a last-mile no-edit gate before any screen action is executed."""
    if plan.state_confidence < MIN_STATE_CONFIDENCE or plan.question_state in RECHECK_STATES:
        return [_state_recheck_action()]

    if plan.question_state == "complete":
        return [item.model_dump() for item in plan.actions]

    safe: list[dict[str, Any]] = []
    for item in plan.actions:
        if post_submit_lock:
            if item.action == "type" or item.target in INPUT_TARGETS:
                continue
            if item.action == "click" and item.target not in {"advance", "navigation", "submit"}:
                continue
        elif plan.question_state in PROTECTED_STATES:
            if item.action == "type" or item.target in INPUT_TARGETS:
                continue
            if item.action == "click" and item.target not in {"advance", "navigation", "submit"}:
                continue
        elif plan.question_state == "answered":
            if item.action == "type" or item.target in INPUT_TARGETS:
                continue
            if item.action == "click" and item.target not in {"advance", "navigation", "submit"}:
                continue
        safe.append(item.model_dump())

    return safe or [_state_recheck_action()]


def _type_text(text: str) -> None:
    pyautogui.write(text, interval=TYPE_INTERVAL)
    time.sleep(POST_TYPE_DELAY)


def _sleep_with_abort(seconds: float, aborted: Callable[[], bool]) -> bool:
    """Sleep in short slices so a user abort remains responsive."""
    deadline = time.monotonic() + max(0.0, seconds)
    while True:
        if aborted():
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True
        time.sleep(min(0.1, remaining))


def _focus_at(x: int, y: int, kind: str = "click") -> None:
    try:
        from overlay import hide_target, show_target

        show_target(x, y, kind=kind)
        hide_target()
        time.sleep(0.05)
    except Exception:
        pass
    pyautogui.moveTo(x, y, duration=MOVE_DURATION)
    pyautogui.click(x, y)
    time.sleep(FOCUS_AFTER_CLICK)


def _label_for(action: dict[str, Any]) -> str:
    if action.get("label"):
        return str(action["label"])
    name = action["action"]
    target = action.get("target") or "target"
    if name == "type":
        return f"Entering {target}..."
    if name == "click":
        return f"Selecting {target}..."
    if name == "wait":
        return "Waiting..."
    if name == "press_key":
        return f"Pressing {action.get('key', 'key')}"
    if name == "done":
        return action.get("reason") or "Done"
    return name


def execute_actions(
    actions: list[dict[str, Any]],
    workspace: Workspace,
    should_abort: Callable[[], bool] | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    recent_signatures: list[tuple[Any, ...]] | None = None,
) -> str:
    configure_pyautogui()
    history = list(recent_signatures or [])
    consecutive_dupes = 0
    executed_input = False

    def emit(event: dict[str, Any]) -> None:
        if on_event is not None:
            on_event(event)

    def aborted() -> bool:
        return bool(should_abort and should_abort())

    def flush_history() -> None:
        if recent_signatures is not None:
            recent_signatures.clear()
            recent_signatures.extend(history[-16:])

    try:
        from overlay import ensure_started

        ensure_started()
    except Exception:
        pass

    for index, action in enumerate(actions, start=1):
        if aborted():
            emit({"type": "aborted", "text": "Stopped by user."})
            return "aborted"

        name = str(action.get("action") or "").strip().lower()
        sig = action_signature(action)
        if name not in {"wait", "done"}:
            if history and sig == history[-1]:
                consecutive_dupes += 1
                log.warning("Skipping consecutive duplicate action %s", sig)
                if consecutive_dupes >= MAX_CONSECUTIVE_DUPES:
                    log.warning("Repeat loop detected (%s identical actions). Breaking.", sig)
                    flush_history()
                    return "repeat"
                continue
            consecutive_dupes = 0
            history.append(sig)

        label = _label_for(action)
        emit({"type": "progress", "step": index, "max": len(actions), "text": f"⚡ {label}"})
        log.info("Action %s/%s %s %s", index, len(actions), name, label)

        try:
            if name == "done":
                log.info("Batch complete: %s", label)
                flush_history()
                return "complete"

            if name == "wait":
                seconds = float(action.get("seconds") or 1.0)
                if not _sleep_with_abort(seconds, aborted):
                    emit({"type": "aborted", "text": "Stopped by user."})
                    return "aborted"
                continue

            if name == "press_key":
                pyautogui.press(str(action.get("key") or "enter"))
                executed_input = True
                time.sleep(POST_KEY_DELAY)
                continue

            if name == "type":
                _type_text(str(action.get("text") or ""))
                executed_input = True
                continue

            if name == "click":
                mapped = box_to_coords(list(action.get("box_2d") or []), workspace)
                if mapped is None:
                    log.warning("Skipping click with missing/invalid box_2d: %r", action.get("box_2d"))
                    time.sleep(MISSING_BOX_WAIT)
                    continue
                x, y = mapped
                log.info("Click at screen (%s, %s)", x, y)
                _focus_at(x, y)
                if action.get("target") == "dropdown":
                    time.sleep(DROPDOWN_SETTLE)
                executed_input = True
                continue

            log.warning("Skipping unknown action %r", name)
        except pyautogui.FailSafeException:
            emit({"type": "error", "text": "FAILSAFE: mouse moved to a screen corner. Stopped."})
            return "aborted"
        except Exception as exc:
            log.warning("Action failed (%s); continuing. %s", name, exc)
            time.sleep(MISSING_BOX_WAIT)
            continue

    flush_history()

    if not executed_input:
        return "empty"

    return "done"


def run_oneshot(
    goal: str,
    sidebar_width: int = SIDEBAR_WIDTH,
    should_abort: Callable[[], bool] | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    mode: str = "auto",
) -> str:
    def emit(event: dict[str, Any]) -> None:
        if on_event is not None:
            on_event(event)

    def aborted() -> bool:
        return bool(should_abort and should_abort())

    from google import genai
    from google.genai import types

    mode = normalize_mode(mode)

    try:
        client = genai.Client(api_key=_api_key())
        models = _model_queue()
    except Exception as exc:
        log.error("LLM client failed: %s", exc)
        emit({"type": "error", "text": str(exc)})
        return "error"

    log.info("Launch subject=auto goal=%s", goal[:120])

    solved_any = False
    previous_fingerprint: bytes | None = None
    submitted_fingerprint: bytes | None = None
    unchanged_frames = 0
    action_history: list[tuple[Any, ...]] = []

    for iteration in range(1, MAX_ITERATIONS + 1):
        if aborted():
            emit({"type": "aborted", "text": "Stopped by user."})
            return "aborted"

        emit({"type": "progress", "text": f"Capturing screen ({iteration})..."})
        workspace = capture_high_res_workspace(sidebar_width)
        fingerprint = workspace_fingerprint(workspace)
        post_submit_lock = submitted_fingerprint is not None and fingerprint == submitted_fingerprint
        if submitted_fingerprint is not None and not post_submit_lock:
            submitted_fingerprint = None
        log.info(
            "Iteration %s/%s workspace %sx%s PNG %s KB origin=(%s,%s)",
            iteration,
            MAX_ITERATIONS,
            workspace.width,
            workspace.height,
            max(1, len(workspace.image_bytes) // 1024),
            workspace.origin_x,
            workspace.origin_y,
        )

        force_advance = unchanged_frames >= FORCE_ADVANCE_AFTER
        if previous_fingerprint is not None and fingerprint == previous_fingerprint:
            unchanged_frames += 1
            force_advance = unchanged_frames >= FORCE_ADVANCE_AFTER
            if unchanged_frames >= MAX_UNCHANGED_FRAMES:
                emit({"type": "done", "text": "Could not reach the next question from this screen. Open the next item, then press Launch."})
                return "done"
        else:
            unchanged_frames = 0
            force_advance = False
        previous_fingerprint = fingerprint

        try:
            emit({"type": "progress", "text": f"Evaluating workspace ({iteration})..."})
            if force_advance:
                guard = ADVANCE_GUARD
            elif post_submit_lock:
                guard = "Previous turn clicked Submit/Check and this is the same screenshot; do not edit answer targets."
            else:
                guard = "none"
            model, plan = decide(client, types, workspace, goal, models, mode=mode, guard=guard)
            emit({
                "type": "log",
                "text": f"State: {plan.question_state} ({plan.state_confidence:.0%}) • {plan.plan_summary}",
                "model": model,
                "question_state": plan.question_state,
                "state_confidence": plan.state_confidence,
            })
        except Exception as exc:
            log.error("LLM call failed: %s", exc)
            emit({"type": "error", "text": str(exc)})
            return "error"

        if aborted():
            emit({"type": "aborted", "text": "Stopped by user."})
            return "aborted"

        lock_answers = post_submit_lock or force_advance or plan.question_state in PROTECTED_STATES | {"answered"}
        actions = gated_actions(plan, post_submit_lock=lock_answers)
        if plan.is_complete and plan.state_confidence >= MIN_STATE_CONFIDENCE and not force_advance:
            emit({"type": "done", "text": "Assignment completed successfully!"})
            return "done"
        if force_advance and plan.is_complete:
            log.info("Ignoring is_complete on an unchanged finished question; advancing instead.")

        actions = actions[:MAX_ACTIONS]
        has_progress = any(item.get("target") in {"submit", "advance", "navigation"} for item in actions)
        if force_advance and not has_progress:
            emit({"type": "progress", "text": "This question is already finished; going to the next one..."})
            actions = [_advance_fallback()]

        plan_sigs = [action_signature(item) for item in actions if item.get("action") not in {"wait", "done"}]
        if plan_sigs and action_history and plan_sigs == action_history[-len(plan_sigs) :]:
            log.warning("Plan repeats the last executed actions; forcing Next.")
            emit({"type": "progress", "text": "Skipping a repeat click; moving to the next question..."})
            actions = [_advance_fallback()]

        status = execute_actions(
            actions,
            workspace,
            should_abort=should_abort,
            on_event=on_event,
            recent_signatures=action_history,
        )
        if status not in {"aborted", "error"} and any(item.get("target") == "submit" for item in actions):
            submitted_fingerprint = fingerprint
        if status in {"aborted", "error"}:
            return status
        if status == "complete":
            emit({"type": "done", "text": plan.actions[-1].reason if plan.actions and plan.actions[-1].reason else "Assignment completed successfully!"})
            return "done"
        if status == "repeat":
            emit({"type": "progress", "text": "That click already happened; moving to the next question..."})
            pyautogui.press("right")
            time.sleep(POST_KEY_DELAY)
            solved_any = True
            if not _sleep_with_abort(PAGE_SETTLE, aborted):
                emit({"type": "aborted", "text": "Stopped by user."})
                return "aborted"
            continue
        if status == "empty":
            if plan.state_confidence < MIN_STATE_CONFIDENCE or plan.question_state in RECHECK_STATES:
                emit({"type": "progress", "text": "Question state is unclear; checking the same screen again..."})
                if not _sleep_with_abort(0.35, aborted):
                    emit({"type": "aborted", "text": "Stopped by user."})
                    return "aborted"
                continue
            if not solved_any and plan.question_state not in {"blocked", "complete"}:
                emit({
                    "type": "error",
                    "text": "No executable educational question state was detected. Open the assignment, then Launch again.",
                })
                return "error"

        solved_any = True
        if plan.is_complete:
            emit({"type": "done", "text": "Assignment completed successfully!"})
            return "done"

        emit({"type": "progress", "text": "Advancing to the next question..."})
        if not _sleep_with_abort(PAGE_SETTLE, aborted):
            emit({"type": "aborted", "text": "Stopped by user."})
            return "aborted"

    emit({"type": "done", "text": f"Stopped after {MAX_ITERATIONS} steps. Press Launch to continue."})
    return "done"


def run_two_phase_agent(
    user_instruction: str,
    on_status_update: Callable[[str], None] = print,
    sidebar_width: int = SIDEBAR_WIDTH,
) -> str:
    def on_event(event: dict[str, Any]) -> None:
        text = event.get("text")
        if text:
            on_status_update(str(text))

    return run_oneshot(user_instruction, sidebar_width=sidebar_width, on_event=on_event)


run_viszmo_loop = run_oneshot


if __name__ == "__main__":
    load_env()
    setup_logging()
    instruction = " ".join(sys.argv[1:]).strip() or "Find the function r that satisfies the given conditions."
    run_two_phase_agent(instruction)
