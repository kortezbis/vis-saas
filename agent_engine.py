"""Single-phase workspace copilot: screenshot → provider JSON plan → execute → repeat."""

from __future__ import annotations

import base64
import ctypes
import hashlib
import io
import json
import logging
import math
import os
import queue
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
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
GENERAL_FAST_ADVANCE_ATTEMPTS = 2
FORCE_ADVANCE_AFTER = 2
# Math keeps a browser-rendering buffer, but does not need the old 0.8s pause
# after every action now that simple choice plans can continue in one turn.
PAGE_SETTLE = 0.45
MISSING_BOX_WAIT = 0.15
MIN_STATE_CONFIDENCE = 0.80
LLM_RETRIES = 4
# Flash-tier plans normally return in well under 15s. A cap near 30s fails
# fast into the fallback queue instead of stalling a whole iteration.
DEFAULT_GEMINI_REQUEST_TIMEOUT_SECONDS = 30.0
MIN_GEMINI_REQUEST_TIMEOUT_SECONDS = 8.0
MAX_GEMINI_REQUEST_TIMEOUT_SECONDS = 120.0
# Reasoning models legitimately think longer than flash plans, so OpenAI
# keeps a slightly higher default ceiling.
DEFAULT_OPENAI_REQUEST_TIMEOUT_SECONDS = 40.0
# One timed-out attempt per model before rotating; the queue is only abandoned
# after every fallback has had its chance.
MAX_TIMEOUTED_MODEL_ATTEMPTS = 4
MAX_TIMEOUT_RECOVERY_ATTEMPTS = 3
MAX_SERVICE_RECOVERY_ATTEMPTS = 3
SERVICE_RECOVERY_DELAY_SECONDS = 5.0
DEFAULT_MODEL = "gemini-3.5-flash"


class DesktopUsageError(RuntimeError):
    pass


class DesktopUsageClient:
    def __init__(self, access_token: str, origin: str | None = None) -> None:
        self.access_token = str(access_token or "").strip()
        configured_origin = str(
            origin or os.getenv("VISZMO_WEBSITE_URL") or "https://www.viszmo.com"
        ).strip()
        self.origin = configured_origin.rstrip("/") or "https://www.viszmo.com"

    def pricing_url(self) -> str:
        return f"{self.origin}/pricing?product=homework"

    def consume(self, request_id: str) -> dict[str, Any]:
        if not self.access_token:
            raise DesktopUsageError("Sign in to Viszmo before answering questions.")
        body = json.dumps({"requestId": request_id}).encode("utf-8")
        request = urllib.request.Request(
            f"{self.origin}/api/desktop-usage",
            data=body,
            headers={
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=12) as response:
                payload = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            payload = exc.read().decode("utf-8", errors="replace")
            try:
                data = json.loads(payload)
            except json.JSONDecodeError:
                data = {}
            if exc.code == 402 and isinstance(data, dict):
                return data
            message = data.get("error") if isinstance(data, dict) else ""
            raise DesktopUsageError(
                str(message or f"Desktop usage request failed (HTTP {exc.code}).")
            ) from exc
        except (OSError, TimeoutError) as exc:
            raise DesktopUsageError(
                "Could not verify desktop access. Check your connection and try again."
            ) from exc

        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise DesktopUsageError("Desktop usage response was invalid.") from exc
        if not isinstance(data, dict):
            raise DesktopUsageError("Desktop usage response was invalid.")
        return data


def desktop_usage_request_id(
    fingerprint: bytes,
    plan: Any,
    actions: list[dict[str, Any]],
) -> str:
    digest = hashlib.sha256()
    digest.update(fingerprint)
    digest.update(str(getattr(plan, "plan_summary", "") or "").strip().encode("utf-8"))
    digest.update(str(getattr(plan, "answer_text", "") or "").strip().encode("utf-8"))
    for action in actions:
        if is_answer_action(action) or str(action.get("target") or "").strip().lower() in {"submit", "advance"}:
            digest.update(json.dumps(action, sort_keys=True, default=str).encode("utf-8"))
    return f"desktop-question-{digest.hexdigest()}"
DEFAULT_MATH_MODEL = "gemini-3.7-flash"
MODEL_FALLBACKS = ("gemini-3.5-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite")
# General leads with the most consistently responsive model; the lite tier
# stays in the chain as the cheap hedge. Both orders survive an env override
# because DEFAULT_MODEL is always appended to the candidate list.
GENERAL_MODEL_FALLBACKS = ("gemini-3.5-flash", "gemini-3.5-flash-lite")
MATH_MODEL_FALLBACKS = ("gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.5-flash-lite")
DEFAULT_OPENAI_MODEL = "gpt-5-nano"
DEFAULT_OPENAI_MATH_MODEL = "gpt-5-mini"
DEFAULT_OPENAI_GENERAL_MODEL = "gpt-5-nano"
# Keep the cheapest route bounded: a temporary outage should be retried by
# the service-recovery loop instead of silently escalating to a costly model.
OPENAI_GENERAL_MODEL_FALLBACKS = (DEFAULT_OPENAI_MODEL,)
OPENAI_MATH_MODEL_FALLBACKS = (DEFAULT_OPENAI_MODEL,)

ActionName = Literal["click", "type", "press_key", "scroll", "wait", "done"]
ActionTarget = Literal[
    "answer",
    "choice",
    "dropdown",
    "option",
    "cancel",
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
ModeName = Literal["math", "general"]

INPUT_TARGETS = {"answer", "choice", "dropdown", "option"}
SAFE_NAVIGATION_TARGETS = {"advance", "navigation", "submit", "cancel"}
PROTECTED_STATES = {"graded_correct", "graded_incorrect", "locked"}
RECHECK_STATES = {"unknown", "loading"}


# ---------------------------------------------------------------------------
# 1. STRICT JSON SCHEMA (Pydantic → provider structured output)
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
    # Models occasionally emit a point instead of a box (zero height or
    # width). The coordinates are still a click intention — expand it into
    # a minimal target rather than discarding the whole plan.
    min_span = 12
    if ymax - ymin < 1:
        ymin = max(0, ymin - min_span // 2)
        ymax = min(int(BOX_SCALE), ymin + min_span)
    if xmax - xmin < 1:
        xmin = max(0, xmin - min_span // 2)
        xmax = min(int(BOX_SCALE), xmin + min_span)
    return [ymin, xmin, ymax, xmax]


class Action(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: ActionName = Field(
        description="Action type: 'click', 'type', 'press_key', 'scroll', 'wait', or 'done'"
    )
    target: ActionTarget = Field(
        default="unknown",
        description="Semantic target: answer, choice, dropdown, option, cancel, submit, advance, navigation, template, wait, or done.",
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
    direction: str | None = Field(
        default=None,
        description="Scroll direction: 'up' or 'down'. Required for scroll.",
    )
    clicks: int | None = Field(
        default=None,
        description="Number of scroll clicks (1-10). Required for scroll.",
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
            "dismiss": "cancel",
            "close": "cancel",
            "cancel_dialog": "cancel",
            "cancel_modal": "cancel",
            "close_dialog": "cancel",
            "close_modal": "cancel",
            "no": "cancel",
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
        if self.action == "scroll":
            if not self.direction:
                self.direction = "down"
            if not self.clicks or self.clicks < 1:
                self.clicks = 3
            if self.target == "unknown":
                self.target = "navigation"
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
    answer_text: str | None = Field(
        default=None,
        description=(
            "Concise human-readable answer for the current visible question. For a choice or "
            "dropdown, use the exact visible option label; for numeric/text, use the exact "
            "expression or text to enter. Null for loading, scrolling, completion, or when no "
            "question answer is available."
        ),
    )
    question_state: QuestionState = Field(
        description="First-screenshot state: unsolved, answered, graded_correct, graded_incorrect, locked, loading, complete, blocked, or unknown.",
    )
    state_confidence: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Confidence in question_state from the visible screenshot, from 0 to 1.",
    )
    requires_verification: bool = Field(
        default=True,
        description=(
            "For Math, true when a typed answer needs a fresh screenshot before Submit/Next "
            "(math template, auto-formatting, complex or uncertain entry, graph/canvas, or "
            "ambiguous UI). False is allowed only for a plain visible field or safe selection "
            "where answer and navigation can run in one plan. General mode ignores this field."
        ),
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
            # Models sometimes append a harmless stray click to a completion
            # verdict (a score-screen link or review button). The verdict is
            # authoritative: strip the strays instead of discarding an entire
            # correct plan at the finish line.
            return self.model_copy(
                update={"actions": [item for item in self.actions if item.action == "done"]}
            )
        if self.question_state == "complete" and not self.is_complete:
            raise ValueError("complete question_state requires is_complete=true")
        if self.is_complete and self.question_state != "complete":
            raise ValueError("is_complete=true requires question_state=complete")

        if self.question_state in RECHECK_STATES:
            if any(
                item.action not in {"wait", "done"}
                and not (item.action == "click" and item.target == "cancel")
                for item in self.actions
            ):
                raise ValueError("unknown or loading state may only cancel, wait, or finish")

        if self.question_state == "blocked":
            if any(
                item.action not in {"wait", "done"}
                and not (item.action == "click" and item.target == "cancel")
                for item in self.actions
            ):
                raise ValueError("blocked state may only cancel, wait, or finish")

        if self.question_state in PROTECTED_STATES:
            for item in self.actions:
                if item.action == "type" or item.target in INPUT_TARGETS:
                    raise ValueError("graded or locked state cannot edit an answer target")
                if item.action == "click" and item.target not in SAFE_NAVIGATION_TARGETS:
                    raise ValueError("graded or locked state may only navigate or advance")

        if self.question_state == "answered":
            for item in self.actions:
                if item.action == "type" or item.target in INPUT_TARGETS:
                    raise ValueError("answered state cannot edit an answer target")
                if item.action == "click" and item.target not in SAFE_NAVIGATION_TARGETS:
                    raise ValueError("answered state may only submit or advance")
        return self


@dataclass(frozen=True)
class ModeProfile:
    move_duration: float
    type_interval: float
    replace_answer_before_type: bool
    focus_after_click: float
    post_type_delay: float
    post_key_delay: float
    dropdown_settle: float
    page_settle: float
    navigation_settle: float
    missing_box_wait: float
    recheck_wait: float
    overlay_pause: float
    force_advance_after: int
    max_unchanged_frames: int
    use_thinking: bool
    thinking_level: str
    verify_answer_before_submit: bool


MATH_PROFILE = ModeProfile(
    move_duration=MOVE_DURATION,
    type_interval=TYPE_INTERVAL,
    replace_answer_before_type=True,
    focus_after_click=FOCUS_AFTER_CLICK,
    post_type_delay=POST_TYPE_DELAY,
    post_key_delay=POST_KEY_DELAY,
    dropdown_settle=DROPDOWN_SETTLE,
    page_settle=PAGE_SETTLE,
    navigation_settle=PAGE_SETTLE,
    missing_box_wait=MISSING_BOX_WAIT,
    recheck_wait=0.25,
    overlay_pause=0.05,
    force_advance_after=FORCE_ADVANCE_AFTER,
    max_unchanged_frames=MAX_UNCHANGED_FRAMES,
    use_thinking=True,
    # Low thinking keeps arithmetic/choice questions responsive. The
    # GEMINI_MATH_THINKING_LEVEL environment variable can still raise this
    # to medium for unusually difficult work.
    thinking_level="low",
    verify_answer_before_submit=True,
)

GENERAL_PROFILE = ModeProfile(
    # General answers are mostly simple choice/short-text interactions, so
    # use a slightly shorter interaction cadence while keeping a small
    # browser-rendering buffer after each event.
    move_duration=0.03,
    type_interval=0.003,
    replace_answer_before_type=False,
    focus_after_click=0.055,
    post_type_delay=0.02,
    post_key_delay=0.03,
    dropdown_settle=0.08,
    page_settle=0.15,
    navigation_settle=0.30,
    missing_box_wait=0.06,
    recheck_wait=0.15,
    overlay_pause=0.02,
    force_advance_after=2,
    max_unchanged_frames=6,
    use_thinking=False,
    thinking_level="minimal",
    verify_answer_before_submit=False,
)


def profile_for(mode: ModeName) -> ModeProfile:
    return MATH_PROFILE if mode == "math" else GENERAL_PROFILE


DEFAULT_UNCHANGED_LLM_SKIPS = 2
MAX_UNCHANGED_LLM_SKIPS = 6


def _unchanged_llm_skip_limit() -> int:
    """Return how many identical-frame iterations wait locally instead of
    paying for another model turn. Zero disables the local recheck."""
    raw = os.getenv("VISZMO_UNCHANGED_LLM_SKIPS", str(DEFAULT_UNCHANGED_LLM_SKIPS))
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        value = DEFAULT_UNCHANGED_LLM_SKIPS
    return max(0, min(MAX_UNCHANGED_LLM_SKIPS, value))

_STATE_AND_PROGRESSION = """
STATE AND PROGRESSION:
1. FIRST classify the screenshot as exactly one question_state: unsolved, answered, graded_correct, graded_incorrect, locked, loading, complete, blocked, or unknown. Give state_confidence from 0 to 1.
2. Treat visible answer text/selections, highlighted/selected choices, green checks, red corrections, disabled fields, lock icons, Correct/Submitted/Graded text, score summaries, and thank-you/completion screens as authoritative state indicators. Indicators may appear beside the question on the left side of the page.
3. If a confirmation dialog or modal is open, it is the active UI layer. Do not click Submit, Next, or any control behind it. Click the visible Cancel/Dismiss/Close button with target=cancel, then re-check the question. Never click Submit again while that dialog is open.
4. If question_state is answered, do not click or type any answer/choice/dropdown/option target. Only click Submit/Check, Next/Continue/right-arrow, cancel an active dialog, or wait for validation.
5. If question_state is graded_correct, graded_incorrect, or locked, do not edit anything. Only advance/navigate, cancel an active dialog, or wait. Never re-solve a graded or already-finished question.
6. If question_state is loading or unknown, return only a short wait and re-check the same screenshot state. Never guess into an uncertain screen.
7. If unsolved, answer only the FIRST unanswered field visible on screen. Use one click/type sequence for that single field. Do NOT batch multiple fields or mix scroll and fill in the same action array.
8. After typing a numeric/text field, stop so the executor can re-screenshot and verify the entry unless requires_verification=false for a plain field with no template or auto-formatting risk and a visible same-question Submit/Check/Next control. For a choice, graph, or dropdown selection, same-question Submit/Check/Next controls may follow in the same plan when already visible.
9. SCROLLING RULE: If a field or button is partially out of view, return ONLY a scroll action (no fill or click). Never combine scroll + fill or scroll + click in the same actions array. After scrolling, wait for the next screenshot before acting.
10. After a choice is already selected/highlighted, do not click it again. Submit if needed, then advance with Next, Continue, or the right arrow.
11. is_complete and question_state=complete are ONLY for the entire assignment: a score/thank-you/submitted-all screen, and no Next/Continue/unanswered item. One finished question means click Next immediately. Never stop after a single question.
12. FINAL SUBMISSION SWEEP (mandatory): Before clicking any whole-assignment finisher — 'Finish & score', 'Submit test', 'Submit quiz', 'Turn in', 'Hand in' — inspect every question indicator on screen (numbered sidebar/grid, dots, progress chips). If ANY item appears blank, unanswered, or unattempted, do NOT finish: click that item's number to open it and answer it first. A completion/score screen that reports unanswered or zero-score-for-blanks items means the assignment is NOT complete — navigate back and fill them. Finishing with visible blanks is a critical failure.
""".strip()

_OUTPUT_CONTRACT = """
OUTPUT CONTRACT:
- Return only one JSON object matching the supplied ExecutionPlan schema. No markdown, explanations, derivations, chain-of-thought, or conversational text.
- plan_summary must be a short operational label, not an explanation.
- answer_text must contain the concise answer for the current visible question whenever one is available: exact visible choice/dropdown label or exact numeric/text expression. Use null only for loading, scrolling, completion, or no answer available.
- requires_verification is a fail-safe Math flag. Use true when a typed answer needs a fresh screenshot before Submit/Next (math template, auto-formatting, complex or uncertain entry, graph/canvas, or ambiguous UI). Set false only for a plain visible field or safe selection with no formatting risk; General mode ignores it.
- Coordinates are precise [ymin, xmin, ymax, xmax] boxes on a 0-1000 scale relative to the supplied workspace screenshot, whose copilot sidebar is cropped out. Use a separate box and action sequence for every independent input/control.
- Actions: click{box_2d, target} — click a UI element; type{text, target} — type text after a preceding click; press_key{key, target} — send one key; scroll{direction, clicks} — scroll the page ('up'/'down', 1-10 clicks), NO box_2d needed; wait{seconds} — pause; done{reason} — signal completion.
- Every click/type/press_key action requires a semantic target: answer, choice, dropdown, option, cancel, submit, advance, navigation, or template. Use target=cancel only for a visible dialog's Cancel/Dismiss/Close button. scroll uses target=navigation by default.
- click requires box_2d. scroll requires direction ('up' or 'down') and clicks (integer 1-10). Never put box_2d on a scroll action.
""".strip()


MATH_SYSTEM_PROMPT = f"""
You are Viszmo in MATH mode — a precise, incremental STEM UI automation agent.
Work on the visible quantitative assignment (algebra, geometry, trigonometry, calculus, statistics, physics, chemistry, graphs, functions, units, and numeric word problems).

INCREMENTAL EXECUTION (critical — failure to follow causes infinite loops):
- Each turn handles EXACTLY ONE visible question. For a choice, graph, or dropdown answer, the plan may include the visible Submit/Check/Next control for that same question. For a numeric/text answer, type only by default and let the next screenshot verify the entry; the only exception is an explicitly low-risk plain field with requires_verification=false.
- Set requires_verification=false only when a plain visible input has no math-template or auto-formatting risk and its visible Submit/Check/Next control can safely follow in this same plan. Keep it true for fraction, exponent, radical, math-editor/template, graph/canvas, formatted, complex, or uncertain entries. Choice and dropdown clicks are already safe to submit/advance in one plan.
- In your "plan_summary", you MUST list the indices of all questions you can see that are already filled (e.g. "Q1 filled, Q2 filled — acting on Q3"). If a field you targeted last turn still shows the same or new text, mark it as done and move to the next.
- SCROLL ISOLATION: If the next target is not fully visible, emit ONLY {{"action": "scroll", "target": "navigation", "direction": "down", "clicks": 3}}. No fill or click in that same array. The next screenshot will show the revealed content.
- REPEAT GUARD: If the prior execution guard matches the current target field and text, skip that field immediately and advance to the next unanswered item.
- When running in Copilot batch review, always populate answer_text and, when a visible non-submit Next/Continue/right-arrow can move to another question without entering an answer, include that navigation action after the answer plan. Never treat a Submit/Check control as non-submit navigation.

MATH-SPECIFIC RULES:
- Recompute every answer from the visible problem; never trust a prior model answer or a value already typed into the field.
- Before typing, perform an independent second check: substitute the result back into the equation, recompute with an alternate algebraic route, estimate the magnitude, or check units/derivative/integral behavior as appropriate.
- Verify signs, formulas, units, significant figures, scientific notation, fractions, exponents, and requested rounding. Keep all scratch work internal.
- Match the active math editor exactly. Use platform syntax such as '/' for fractions and '^' for exponents.
- In an answer box, type only the expression the label requests. Do not add a leading 'y=', 'f(x)=', or other variable name unless the visible prompt or placeholder explicitly requires it.
- Treat one complete expression as one type action. Do not split it into several type actions and never append a second copy to a field that already contains the expression.
- After entering a fraction, exponent, radical, or template slot, use an explicit press_key action (usually right or tab) to leave the template before continuing.
- Do not send math-template navigation keys in ordinary text fields.
- For graph/image multiple-choice questions, identify the correct visual option, click it with target choice, then submit and advance.
- For numeric answers, type only the final value in the requested format (decimal, fraction, unit suffix, etc.).
- Always populate answer_text with the exact answer you computed, even when the action plan is only typing/clicking it. In Copilot batch review, a visible non-submit Next/Continue/right-arrow may follow so the next page can be scanned without entering or submitting the answer.
- For numeric answers, set requires_verification=true by default. Set it false only for a plain text/numeric field whose visible value will not be transformed or formatted by the UI; then Submit/Check/Next may follow in the same plan. Never set it false for a math editor/template, auto-formatting field, graph/canvas, complex multi-step answer, or any uncertain state.

{_STATE_AND_PROGRESSION}

MATH ANSWER FORMATTING:
- Preserve units, π, degree symbols, and any format shown in the prompt or answer box placeholder.
- For radio buttons, checkboxes, and graph choices, click the correct option, then submit/advance.
- For dropdowns, click the closed control (target dropdown), then click the exact visible option (target option) or type the native-select label and press Enter.

{_OUTPUT_CONTRACT}
""".strip()

GENERAL_SYSTEM_PROMPT = f"""
You are Viszmo in GENERAL mode — a fast, incremental educational UI automation agent.
Work on reading, vocabulary, literature, history, government, civics, language arts, social studies, and any non-quantitative assignment.

INCREMENTAL EXECUTION (critical — failure to follow causes infinite loops):
- Each turn handles EXACTLY ONE visible question: answer its active field, then use any visible Submit/Check/Next control needed for that same question. Never answer multiple question cards in one action array.
- Act only on the FIRST unanswered field that is currently visible. The platform progress counter (for example, 3/20) and question-card labels are not a reliable index for every visible card; do not use them to jump between cards.
- In your "plan_summary", describe the active visible prompt/control briefly (for example, "DMZ definition — selecting True"). Do not list several question numbers and then choose a coordinate from a different card.
- SCROLL ISOLATION: If the target question or button is not fully visible, emit ONLY {{"action": "scroll", "target": "navigation", "direction": "down", "clicks": 3}}. No other actions in that array. Re-evaluate on the next screenshot.
- REPEAT GUARD: If the prior execution guard describes the same target as the current plan, skip that item and move to the next unanswered question.

SPEED (critical for General mode):
- Most questions are simple multiple choice, true/false, or short text. One click on the correct option is usually enough.
- If Submit/Check/Next is already visible for that same question, include it after the answer in the same plan to avoid an unnecessary model turn.
- Use wait actions only when the UI is visibly loading; keep waits short.
- Do not re-read or re-answer an already selected, graded, or finished item.
- If the prior turn clicked an answer and the page is still on that question, treat that answer as committed. Never click the same choice again; use the visible Submit/Check or Next/Continue control, and use the navigation fallback if the page does not expose one.
- Never mark the assignment complete merely because one question is selected, answered, or graded. `complete` requires the final score/thank-you/submitted-all screen or a clearly finished page with no remaining navigation.

GENERAL-SPECIFIC RULES:
- Read the visible passage, excerpt, table, timeline, map label, and question stem before acting. Base answers only on what is on screen.
- For multiple choice, matching, true/false, dropdowns, checkboxes, and short answers, choose or write the best supported response from the provided material.
- Preserve punctuation, capitalization, spelling, and answer length requested by the platform.
- Do not use math-editor syntax unless the field is visibly a math input.
- For dropdowns, click the closed control (target dropdown), then click the exact visible option (target option) or type the native-select label and press Enter.

{_STATE_AND_PROGRESSION}

GENERAL ANSWER FORMATTING:
- Short-answer fields: type the exact concise response the UI expects.
- For radio/checkbox choices, click the supported option, then submit/advance.

{_OUTPUT_CONTRACT}
""".strip()

SYSTEM_PROMPT = GENERAL_SYSTEM_PROMPT

MATH_GOAL = (
    "Solve each visible quantitative question with full precision, enter the answer in the platform's "
    "required format, click Check/Submit, then Next/Continue. Skip finished or graded items. "
    "Continue through the entire assignment — never stop after one question."
)

GENERAL_GOAL = (
    "Quickly read the visible prompt, answer each unsolved basic question, submit, click Next/Continue, "
    "and keep going until the entire assignment is finished. Prefer the fewest clicks. "
    "Skip already answered or graded items."
)

DEFAULT_GOAL = MATH_GOAL

MATH_USER_TURN = """Subject mode: MATH. Incremental execution enforced.
Analyze the current workspace screenshot and produce the next single-step executable plan.

STATE TRACKING (required in plan_summary):
- Scan the entire visible page. List every question index you can see and whether each one is already filled/selected.
- Example: "Q1 filled, Q2 filled, Q3 empty — targeting Q3 answer box".
- Do NOT target any field already listed as filled, even if the prior guard doesn't mention it.

ACTION RULES THIS TURN:
- If the next unanswered field is fully visible: emit fill+type for that ONE field only by default. A visible Submit/Check/Next may follow only when requires_verification=false for a low-risk plain field. No scrolling in this same array.
- For a math answer box, type the complete requested expression exactly once; use the field label/placeholder to decide whether a variable name such as 'y=' belongs in the answer.
- Set requires_verification=true for a new numeric answer unless it is a plain visible text/numeric field with no template or auto-formatting behavior. With requires_verification=false only, visible Submit/Check/Next may follow the typed answer in this array; otherwise recheck the typed value on the next screenshot first.
- Always populate answer_text with the exact answer to the visible question. In Copilot batch review, include a visible non-submit Next/Continue/right-arrow when it can navigate without entering or submitting the answer.
- For a choice, graph, or dropdown answer, include visible Submit/Check/Next/Continue controls for that same question when they are already on screen.
- If the previous attempt was graded incorrect, solve the visible problem again from first principles; do not make a blind sign or digit change.
- If the next unanswered field is NOT fully visible: emit ONLY one scroll action (direction: down, clicks: 3). No fill or click.
- If the current question is solved and Next/Continue is visible: emit only the click to advance.
- Never bundle scroll + fill, scroll + click, or multiple independent fills into one actions array.

First classify question_state and state_confidence from visible answer/status indicators.
If the question is answered, graded, locked, loading, or uncertain: do not edit answer targets.
A finished question is NOT the end of the assignment. Stop only when the whole assignment is done.
Image size: {api_w} x {api_h} (native PNG, sidebar cropped).
Prior execution guard: {guard}
Goal: {goal}
"""

GENERAL_USER_TURN = """Subject mode: GENERAL (fast). Incremental execution enforced.
Analyze the current workspace screenshot and produce the next single-step executable plan.

STATE TRACKING (required in plan_summary):
- Scan the entire visible page. List every question you can see and whether each is already selected/answered.
- Example: "Q1 selected, Q2 selected, Q3 unanswered — clicking Q3 correct choice".
- Do NOT re-click any option already listed as selected or highlighted.

ACTION RULES THIS TURN:
- If the target choice is fully visible: emit ONE click on the correct option. Do not scroll in this same array.
- If Submit/Check or Next/Continue is visible for that same active question, include the required navigation control after the answer click.
- If the target is NOT fully visible: emit ONLY one scroll action (direction: down, clicks: 3). No click or fill.
- Never bundle scroll + click or scroll + fill into one actions array.
- If the prior execution guard says an answer was already clicked, do not click that answer again. Select Submit/Check or Next/Continue, or emit a short wait only while the page is visibly loading.
- `is_complete=true` is allowed only for the entire assignment's final completion state, never for one answered or graded question.

First classify question_state and state_confidence from visible answer/status indicators.
If the question is answered, graded, locked, loading, or uncertain: do not edit answer targets.
A finished question is NOT the end of the assignment. Stop only when the whole assignment is done.
Image size: {api_w} x {api_h} (native PNG, sidebar cropped).
Prior execution guard: {guard}
Goal: {goal}
"""

USER_TURN = GENERAL_USER_TURN


def normalize_mode(value: str | None) -> ModeName:
    token = str(value or "math").strip().lower()
    if token in {"general", "gen", "reading", "humanities", "english", "history"}:
        return "general"
    return "math"


AutonomyName = Literal["copilot", "autopilot", "dry_run"]


def normalize_autonomy(value: str | None) -> AutonomyName:
    token = str(value or "autopilot").strip().lower().replace("-", "").replace("_", "")
    if token in {"copilot", "assist", "assisted", "suggest"}:
        return "copilot"
    if token in {"dryrun", "dry", "preview", "rehearsal"}:
        return "dry_run"
    return "autopilot"


def apply_dry_run(actions: list[dict[str, Any]], autonomy: str) -> tuple[list[dict[str, Any]], int]:
    """Strip Submit/Check clicks in dry-run mode; everything else passes.

    Returns the kept actions plus how many submits were withheld so the run
    report can show exactly what stayed uncommitted.
    """
    if normalize_autonomy(autonomy) != "dry_run":
        return actions, 0
    kept: list[dict[str, Any]] = []
    skipped = 0
    for item in actions:
        if str(item.get("target") or "").strip().lower() == "submit":
            skipped += 1
            continue
        kept.append(item)
    return kept, skipped


def prompt_for(mode: ModeName) -> str:
    return MATH_SYSTEM_PROMPT if mode == "math" else GENERAL_SYSTEM_PROMPT


def user_turn_for(mode: ModeName) -> str:
    return MATH_USER_TURN if mode == "math" else GENERAL_USER_TURN


def goal_for(mode: ModeName | str | None = None, notes: str = "") -> str:
    if notes.strip():
        return notes.strip()
    resolved = normalize_mode(str(mode) if mode is not None else "math")
    return MATH_GOAL if resolved == "math" else GENERAL_GOAL


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

from PIL import Image  # noqa: E402


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
    coordinate_space: str = "screen"
    input_scale_x: float = 1.0
    input_scale_y: float = 1.0

    @property
    def input_width(self) -> int:
        return max(1, int(round(self.width * self.input_scale_x)))

    @property
    def input_height(self) -> int:
        return max(1, int(round(self.height * self.input_scale_y)))


def load_env() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    env_path = Path(__file__).resolve().parent / ".env"
    load_dotenv(env_path if env_path.exists() else None)


def setup_logging() -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    # Packaged builds and detached processes lose stdout; a file handler keeps
    # diagnostics available regardless of how the backend was launched.
    log_file = os.getenv("VISZMO_LOG_FILE", "").strip()
    if log_file:
        try:
            Path(log_file).parent.mkdir(parents=True, exist_ok=True)
            handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
        except Exception as exc:
            sys.stderr.write(f"Could not open log file {log_file}: {exc}\n")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
    )


def _encode_workspace_image(image: Image.Image) -> tuple[bytes, str]:
    """Encode a captured page image in the configured wire format."""
    from virtual_mouse import _screenshot_format, _screenshot_jpeg_quality

    if _screenshot_format() == "png":
        encoded = io.BytesIO()
        image.save(encoded, format="PNG")
        return encoded.getvalue(), "image/png"
    encoded = io.BytesIO()
    image.save(encoded, format="JPEG", quality=_screenshot_jpeg_quality())
    return encoded.getvalue(), "image/jpeg"


def capture_high_res_workspace(
    sidebar_width: int = SIDEBAR_WIDTH,
    snip_box: tuple[float, float, float, float] | None = None,
) -> Workspace:
    """Capture the managed assignment page, optionally cropped to a CSS-pixel snip."""
    from virtual_mouse import get_mouse

    mouse = get_mouse()
    image_bytes, css_width, css_height = mouse.capture_screenshot(exclude_right_px=sidebar_width)
    image = Image.open(io.BytesIO(image_bytes))
    mime_type = (
        "image/png"
        if getattr(image, "format", "").upper() == "PNG"
        else "image/jpeg"
    )
    if snip_box is not None:
        x, y, width, height = snip_box
        scale_to_image_x = image.width / css_width if css_width > 0 else 1.0
        scale_to_image_y = image.height / css_height if css_height > 0 else 1.0
        left = max(0, min(image.width - 1, round(x * scale_to_image_x)))
        top = max(0, min(image.height - 1, round(y * scale_to_image_y)))
        right = max(left + 1, min(image.width, round((x + width) * scale_to_image_x)))
        bottom = max(top + 1, min(image.height, round((y + height) * scale_to_image_y)))
        image = image.crop((left, top, right, bottom))
        image_bytes, mime_type = _encode_workspace_image(image)
        css_width = width
        css_height = height
    scale_x = css_width / image.width if css_width > 0 else 1.0
    scale_y = css_height / image.height if css_height > 0 else 1.0
    return Workspace(
        image_bytes=image_bytes,
        mime_type=mime_type,
        api_w=image.width,
        api_h=image.height,
        origin_x=0,
        origin_y=0,
        width=image.width,
        height=image.height,
        coordinate_space="page",
        input_scale_x=scale_x,
        input_scale_y=scale_y,
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


def input_action_identity(action: dict[str, Any]) -> tuple[str, str] | None:
    """Return a box-independent identity for a typed answer field."""
    if str(action.get("action") or "").strip().lower() != "type":
        return None
    target = str(action.get("target") or "unknown").strip().lower()
    if target not in INPUT_TARGETS:
        return None
    text = " ".join(str(action.get("text") or "").split()).casefold()
    if not text:
        return None
    return target, text


def is_answer_action(action: dict[str, Any]) -> bool:
    """Return whether an action edits/selects an answer rather than navigating."""
    name = str(action.get("action") or "").strip().lower()
    target = str(action.get("target") or "").strip().lower()
    return name == "type" and target in INPUT_TARGETS or (
        name == "click" and target in INPUT_TARGETS
    )


def copilot_answer_text(plan: ExecutionPlan) -> str | None:
    """Return the model's human-readable answer for the Copilot transcript."""
    answer = " ".join(str(plan.answer_text or "").split()).strip()
    if answer:
        return answer

    # Backward-compatible fallback for a provider response that predates the
    # explicit answer_text field. Typed answers are still recoverable; choice
    # labels are recoverable when the model supplied Action.label.
    for item in plan.actions:
        if item.action == "type" and item.target in INPUT_TARGETS and item.text:
            return " ".join(str(item.text).split()).strip() or None
        if item.action == "click" and item.target in {"choice", "option"} and item.label:
            return " ".join(str(item.label).split()).strip() or None
    return None


def copilot_navigation_actions(plan: ExecutionPlan, profile: ModeProfile) -> list[dict[str, Any]]:
    """Keep only navigation that does not enter or submit an answer.

    Copilot batch review can move through a page when the portal exposes a
    separate Next/Continue/right-arrow control. It never clicks Submit/Check
    and never reuses the model's answer-entry actions.
    """
    if plan.is_complete:
        return []

    if plan.state_confidence < MIN_STATE_CONFIDENCE or plan.question_state in RECHECK_STATES:
        waits = [item.model_dump() for item in plan.actions if item.action == "wait"]
        return waits[:1] or [_state_recheck_action(profile)]

    navigation: list[dict[str, Any]] = []
    for item in plan.actions:
        if item.action == "scroll":
            navigation.append(item.model_dump())
            continue
        if item.target not in {"advance", "navigation"}:
            continue
        if item.action == "click":
            navigation.append(item.model_dump())
            continue
        if item.action == "press_key" and item.key == "right":
            navigation.append(item.model_dump())

    # Scroll isolation and one-question execution keep the batch predictable.
    if any(item.get("action") == "scroll" for item in navigation):
        return [item for item in navigation if item.get("action") == "scroll"][:1]
    return navigation[:2]


def answer_action_identity(action: dict[str, Any]) -> tuple[Any, ...] | None:
    """Return a tolerant identity for a previously selected answer.

    Choice boxes can move by a few pixels between model responses. Quantizing
    the box center lets the General-mode repeat guard recognize the same
    option without confusing it with a different answer row.
    """
    if not is_answer_action(action):
        return None

    typed = input_action_identity(action)
    if typed is not None:
        return ("type", typed[0], typed[1])

    box = valid_box_2d(action.get("box_2d"))
    if box is None:
        return None
    ymin, xmin, ymax, xmax = box
    center_x = (xmin + xmax) // 2
    center_y = (ymin + ymax) // 2
    return (
        "click",
        str(action.get("target") or "unknown").strip().lower(),
        center_x // 20,
        center_y // 20,
    )


def remove_repeated_answer_actions(
    actions: list[dict[str, Any]],
    identity: tuple[Any, ...],
) -> tuple[list[dict[str, Any]], int]:
    """Remove a General-mode answer action that already ran.

    Keep any Submit/Next action in the same model plan so a repeated choice
    can still complete the normal submit/advance sequence.
    """
    filtered: list[dict[str, Any]] = []
    removed = 0
    for action in actions:
        if answer_action_identity(action) == identity:
            removed += 1
            if (
                filtered
                and str(action.get("action") or "").strip().lower() == "type"
                and str(filtered[-1].get("action") or "").strip().lower() == "click"
                and str(filtered[-1].get("target") or "").strip().lower()
                == str(action.get("target") or "").strip().lower()
            ):
                filtered.pop()
            continue
        filtered.append(action)
    return filtered, removed


def remove_repeated_input_actions(
    actions: list[dict[str, Any]],
    identity: tuple[str, str],
) -> tuple[list[dict[str, Any]], int]:
    """Remove a repeated type and its immediately preceding focus click."""
    filtered: list[dict[str, Any]] = []
    removed = 0
    target, _ = identity
    for action in actions:
        if input_action_identity(action) == identity:
            if (
                filtered
                and filtered[-1].get("action") == "click"
                and str(filtered[-1].get("target") or "").strip().lower() == target
            ):
                filtered.pop()
            removed += 1
            continue
        filtered.append(action)
    return filtered, removed


def box_to_coords(box_2d: list[int], workspace: Workspace) -> tuple[int, int] | None:
    """Map a normalized target box to its physical center point."""
    box = valid_box_2d(box_2d)
    if box is None:
        return None
    ymin, xmin, ymax, xmax = (float(v) for v in box)
    input_width = workspace.input_width
    input_height = workspace.input_height
    target_x = workspace.origin_x + int((xmin + (xmax - xmin) * CLICK_POSITION) / BOX_SCALE * input_width)
    target_y = workspace.origin_y + int(((ymin + ymax) / 2.0) / BOX_SCALE * input_height)
    inset_x = max(int(input_width * 0.005), 3)
    inset_y = max(int(input_height * 0.005), 3)
    target_x = min(max(target_x, workspace.origin_x + inset_x), workspace.origin_x + input_width - inset_x)
    target_y = min(max(target_y, workspace.origin_y + inset_y), workspace.origin_y + input_height - inset_y)
    return target_x, target_y


DEFAULT_CLICK_SNAP = 1


def _click_snap_enabled() -> bool:
    raw = os.getenv("VISZMO_CLICK_SNAP", str(DEFAULT_CLICK_SNAP))
    try:
        return bool(int(float(raw)))
    except (TypeError, ValueError):
        return True


def _click_snap_bounds(box_2d: Any, workspace: Workspace) -> tuple[int, int, int, int] | None:
    """Return the CSS-pixel rect a snapped click center may land inside.

    The model's own box, inflated by ~25% per side, keeps the snap honest: a
    match outside it would be a different control than the model intended.
    """
    box = valid_box_2d(box_2d)
    if box is None:
        return None
    ymin, xmin, ymax, xmax = (float(v) for v in box)
    width = workspace.input_width
    height = workspace.input_height
    left = workspace.origin_x + xmin / BOX_SCALE * width
    top = workspace.origin_y + ymin / BOX_SCALE * height
    right = workspace.origin_x + xmax / BOX_SCALE * width
    bottom = workspace.origin_y + ymax / BOX_SCALE * height
    pad_x = max(12.0, (right - left) * 0.25)
    pad_y = max(12.0, (bottom - top) * 0.25)
    return (
        int(left - pad_x),
        int(top - pad_y),
        int(right + pad_x),
        int(bottom + pad_y),
    )


def _snap_click_to_element(
    x: int,
    y: int,
    box_2d: Any,
    workspace: Workspace,
) -> tuple[int, int] | None:
    """Snap an intended click onto the real interactive element underneath.

    Returns the element's true center when one is found inside the model's
    box; otherwise None leaves the original coordinates untouched.
    """
    if not _click_snap_enabled() or workspace.coordinate_space != "page":
        return None
    bounds = _click_snap_bounds(box_2d, workspace)
    if bounds is None:
        return None
    try:
        from virtual_mouse import get_mouse

        mouse = get_mouse()
        snap = getattr(mouse, "snap_to_element", None)
        info = snap(x, y, bounds) if snap else None
    except Exception as exc:
        log.debug("Click snap lookup failed: %s", exc)
        return None
    if not isinstance(info, dict):
        return None
    try:
        snapped_x = int(info["x"])
        snapped_y = int(info["y"])
    except (KeyError, TypeError, ValueError):
        return None
    scrolled = bool(info.get("scrolled"))
    left, top, right, bottom = bounds
    if not scrolled and not (left <= snapped_x <= right and top <= snapped_y <= bottom):
        # The probe returned something outside the model's intended area;
        # never teleport a click to a different control.
        log.info("Ignoring snap outside the target box: (%s,%s)", snapped_x, snapped_y)
        return None
    if scrolled:
        # The element was identified at the model's point, then centered in
        # view — its new position legitimately leaves the original box.
        log.info(
            "Snapped click after scrolling into view: <%s> '%s' at (%s,%s)",
            info.get("tag", "?"),
            str(info.get("text", ""))[:40],
            snapped_x,
            snapped_y,
        )
        return snapped_x, snapped_y
    log.info(
        "Snapped click to real <%s> element '%s' at (%s,%s)",
        info.get("tag", "?"),
        str(info.get("text", ""))[:40],
        snapped_x,
        snapped_y,
    )
    return snapped_x, snapped_y


# ---------------------------------------------------------------------------
# LLM clients
# ---------------------------------------------------------------------------
def normalize_provider(value: str | None = None) -> Literal["gemini", "openai"]:
    """Resolve the configured vision provider without exposing credentials."""

    token = str(value if value is not None else os.getenv("LLM_PROVIDER", "gemini"))
    token = token.strip().lower().replace("_", "-")
    if token in {"openai", "openai-api", "gpt"}:
        return "openai"
    return "gemini"


def _api_key() -> str:
    key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or os.getenv("GOOGLE_GENAI_API_KEY")
    if not key:
        raise RuntimeError("Missing GEMINI_API_KEY (see .env.example).")
    return key


def _openai_api_key() -> str:
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key or key == "your_openai_key_here":
        raise RuntimeError(
            "Missing OPENAI_API_KEY. Add a rotated key to your local .env file "
            "or select LLM_PROVIDER=gemini (see .env.example)."
        )
    return key


def _model_queue(mode: ModeName | str | None = None) -> list[str]:
    resolved_mode = normalize_mode(mode)
    global_model = os.getenv("GEMINI_MODEL", "").strip()
    if resolved_mode == "general":
        preferred = (
            os.getenv("GEMINI_GENERAL_MODEL", "").strip()
            or global_model
            or DEFAULT_MODEL
        )
    else:
        preferred = (
            os.getenv("GEMINI_MATH_MODEL", "").strip()
            or DEFAULT_MATH_MODEL
        )
    models: list[str] = []
    if resolved_mode == "math":
        candidates = (preferred, *MATH_MODEL_FALLBACKS, global_model)
    else:
        # General questions should stay on the low-cost/low-latency route.
        # DEFAULT_MODEL is appended so an env override can never collapse the
        # queue to a single model — hedging and rotation need at least two.
        candidates = (preferred, global_model, DEFAULT_MODEL, *GENERAL_MODEL_FALLBACKS)
    for name in candidates:
        name = str(name or "").strip()
        if not name:
            continue
        if name not in models:
            models.append(name)
    return models


def _openai_model_queue(mode: ModeName | str | None = None) -> list[str]:
    resolved_mode = normalize_mode(mode)
    global_model = os.getenv("OPENAI_MODEL", "").strip()
    if resolved_mode == "general":
        preferred = (
            os.getenv("OPENAI_GENERAL_MODEL", "").strip()
            or global_model
            or DEFAULT_OPENAI_GENERAL_MODEL
        )
        candidates = (preferred, global_model, *OPENAI_GENERAL_MODEL_FALLBACKS)
    else:
        preferred = (
            os.getenv("OPENAI_MATH_MODEL", "").strip()
            or global_model
            or DEFAULT_OPENAI_MATH_MODEL
        )
        candidates = (preferred, *OPENAI_MATH_MODEL_FALLBACKS, global_model)
    models: list[str] = []
    for name in candidates:
        name = str(name or "").strip()
        if name and name not in models:
            models.append(name)
    return models


def _thinking_config(types: Any, level: str = "minimal") -> Any | None:
    normalized_level = str(level or "minimal").strip().upper()
    try:
        thinking_level = getattr(types.ThinkingLevel, normalized_level, None)
        if thinking_level is None:
            raise AttributeError("ThinkingLevel is unavailable")
        return types.ThinkingConfig(thinking_level=thinking_level)
    except Exception:
        # Gemini 2.5 uses a token budget instead of the named levels used by
        # Gemini 3.x. Keep this fallback bounded and deterministic.
        try:
            budgets = {"MINIMAL": 256, "LOW": 768, "MEDIUM": 1536, "HIGH": 3072}
            return types.ThinkingConfig(thinking_budget=budgets.get(normalized_level, 256))
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
    return any(
        token in message
        for token in (
            "404",
            "not_found",
            "not found",
            "model does not exist",
            "no longer available",
            "unsupported model",
        )
    )


def _is_service_busy(exc: Exception) -> bool:
    """Recognize temporary provider capacity failures such as 503 UNAVAILABLE."""
    message = str(exc).lower()
    normalized_message = message.replace("\\_", "_").replace("_", " ")
    return any(
        token in message or token in normalized_message
        for token in (
            "503",
            "429",
            "unavailable",
            "high demand",
            "service unavailable",
            "temporarily unavailable",
            "rate limit",
            "rate_limit",
            "too many requests",
            "overloaded",
        )
    )


def _is_timeout_error(exc: Exception) -> bool:
    """Recognize SDK/network timeouts without importing a specific HTTP client."""
    if isinstance(exc, TimeoutError):
        return True
    message = str(exc).lower()
    normalized_message = message.replace("\\_", "_").replace("_", " ")
    error_name = type(exc).__name__.lower()
    return any(
        token in message or token in normalized_message or token in error_name
        for token in (
            "timeout",
            "timed out",
            "deadline exceeded",
            "deadline_exceeded",
            "gateway timeout",
            "server disconnected",
            "504",
        )
    )


def _gemini_request_timeout_seconds() -> float:
    """Return a bounded per-request timeout configurable through the environment."""
    raw = os.getenv(
        "GEMINI_REQUEST_TIMEOUT_SECONDS",
        str(DEFAULT_GEMINI_REQUEST_TIMEOUT_SECONDS),
    )
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = DEFAULT_GEMINI_REQUEST_TIMEOUT_SECONDS
    return max(
        MIN_GEMINI_REQUEST_TIMEOUT_SECONDS,
        min(MAX_GEMINI_REQUEST_TIMEOUT_SECONDS, value),
    )


def _openai_request_timeout_seconds() -> float:
    """Return the bounded timeout for one OpenAI Responses API request."""

    raw = os.getenv(
        "OPENAI_REQUEST_TIMEOUT_SECONDS",
        str(DEFAULT_OPENAI_REQUEST_TIMEOUT_SECONDS),
    )
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = DEFAULT_OPENAI_REQUEST_TIMEOUT_SECONDS
    return max(
        MIN_GEMINI_REQUEST_TIMEOUT_SECONDS,
        min(MAX_GEMINI_REQUEST_TIMEOUT_SECONDS, value),
    )


def _gemini_http_options(types: Any) -> Any | None:
    """Build SDK HTTP options while remaining compatible with older SDK builds.

    The Python SDK expects ``HttpOptions.timeout`` in milliseconds; the app
    configuration stays in seconds so it remains readable to users.
    """
    try:
        timeout_ms = int(round(_gemini_request_timeout_seconds() * 1000.0))
        return types.HttpOptions(timeout=timeout_ms)
    except Exception as exc:
        log.warning("Gemini SDK does not expose HTTP timeout options: %s", exc)
        return None


def _build_gemini_client() -> Any:
    """Construct a fresh Gemini client.

    Rebuilding discards any pooled HTTP connections a timed-out request may
    have left wedged, which is cheaper than another 30s stall on a dead
    keep-alive socket.
    """
    from google import genai
    from google.genai import types as gemini_types

    client_kwargs: dict[str, Any] = {"api_key": _api_key()}
    http_options = _gemini_http_options(gemini_types)
    if http_options is not None:
        client_kwargs["http_options"] = http_options
    return genai.Client(**client_kwargs)


def _build_openai_client() -> Any:
    """Construct a fresh OpenAI client for the same reason."""
    from openai import OpenAI

    return OpenAI(
        api_key=_openai_api_key(),
        timeout=_openai_request_timeout_seconds(),
    )


def _retry_delay(exc: Exception, attempt: int) -> float | None:
    message = str(exc).lower()
    retryable = any(
        token in message
        for token in (
            "503",
            "429",
            "unavailable",
            "high demand",
            "resource_exhausted",
            "rate limit",
            "rate_limit",
            "too many requests",
            "overloaded",
        )
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
            if _is_unavailable(exc) or _is_service_busy(exc):
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


def _hedge_delay_seconds() -> float:
    """Seconds to wait before also racing the next fallback model.

    Provider stalls are the dominant failure mode; after this delay a second
    model starts in parallel and the first valid plan wins. Zero would race
    everything at once (doubled cost on every turn), so the floor keeps the
    hedge as a stall response only.
    """
    raw = os.getenv("VISZMO_HEDGE_DELAY_SECONDS", "8")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = 8.0
    return max(2.0, min(25.0, value))


def _max_output_tokens(provider: str, mode: ModeName | str | None = None) -> int:
    """Return a configurable output ceiling for the structured one-question plan.

    The defaults leave room for the full JSON action plan while preventing an
    unusually verbose response from becoming a large bill. Per-mode settings
    take precedence over the provider-wide setting, and the ceiling is kept
    deliberately broad so normal plans are not shortened.
    """
    provider_key = "OPENAI" if str(provider).strip().lower() == "openai" else "GEMINI"
    mode_key = normalize_mode(mode).upper()
    default = 3_072 if mode_key == "MATH" else 2_048
    raw = (
        os.getenv(f"{provider_key}_{mode_key}_MAX_OUTPUT_TOKENS", "").strip()
        or os.getenv(f"{provider_key}_MAX_OUTPUT_TOKENS", "").strip()
        or str(default)
    )
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        value = default
    return max(1_024, min(8_192, value))


def _decide_gemini(
    client: Any,
    types: Any,
    workspace: Workspace,
    goal: str,
    models: list[str],
    mode: str | None = None,
    guard: str = "none",
    on_usage: Callable[[Any], None] | None = None,
    on_status: Callable[[str], None] | None = None,
    client_factory: Callable[[], Any] | None = None,
) -> tuple[str, ExecutionPlan]:
    mode = normalize_mode(mode)
    image_part = types.Part.from_bytes(data=workspace.image_bytes, mime_type=workspace.mime_type)
    template = user_turn_for(mode)
    prompt = (
        template.replace("{api_w}", str(workspace.api_w))
        .replace("{api_h}", str(workspace.api_h))
        .replace("{guard}", guard)
        .replace("{goal}", goal)
    )
    profile = profile_for(mode)
    thinking_level_env = (
        "GEMINI_GENERAL_THINKING_LEVEL"
        if mode == "general"
        else "GEMINI_MATH_THINKING_LEVEL"
    )
    thinking_level = os.getenv(thinking_level_env, profile.thinking_level).strip() or profile.thinking_level
    thinking = _thinking_config(types, thinking_level) if profile.use_thinking else None
    system_prompt = prompt_for(mode)

    results: queue.Queue = queue.Queue()

    def attempt(model_name: str, attempt_client: Any) -> None:
        try:
            config_kwargs = _config_kwargs(
                types,
                system_instruction=system_prompt,
                response_mime_type="application/json",
                response_json_schema=_json_schema(),
                max_output_tokens=_max_output_tokens("gemini", mode),
            )
            # Gemini 3.x removed the legacy sampling parameters. Omitting
            # temperature also leaves structured output deterministic through
            # the schema and the accuracy-first prompt.
            if not str(model_name).lower().startswith("gemini-3"):
                config_kwargs["temperature"] = 0.0
            if thinking is not None:
                config_kwargs["thinking_config"] = thinking
            response = _generate(
                attempt_client,
                types,
                model_name,
                [image_part, prompt],
                types.GenerateContentConfig(**config_kwargs),
            )
            results.put((model_name, response, None))
        except Exception as exc:
            results.put((model_name, None, exc))

    pending = list(models)
    inflight: list[str] = []
    hedge_clients: dict[str, Any] = {models[0]: client}
    timeout_failures = 0
    last_error: Exception | None = None
    winner: tuple[str, ExecutionPlan] | None = None
    next_launch_at = time.monotonic()

    while True:
        now = time.monotonic()
        if (
            pending
            and len(inflight) < len(models)
            and (not inflight or now >= next_launch_at)
        ):
            model = pending.pop(0)
            attempt_client = hedge_clients.get(model)
            if attempt_client is None:
                try:
                    # Fresh pools per hedged model keep one wedged connection
                    # from contaminating the whole race.
                    attempt_client = client_factory() if client_factory else client
                except Exception:
                    attempt_client = client
                hedge_clients[model] = attempt_client
            if inflight:
                log.info("Hedging to %s while earlier models are still thinking.", model)
                if on_status is not None:
                    on_status(f"Also trying {model} in parallel...")
            threading.Thread(
                target=attempt,
                args=(model, attempt_client),
                daemon=True,
                name=f"viszmo-decide-{model}",
            ).start()
            inflight.append(model)
            next_launch_at = time.monotonic() + _hedge_delay_seconds()
            continue

        if not inflight:
            break

        try:
            done_model, response, exc = results.get(timeout=0.25)
        except queue.Empty:
            continue

        inflight.remove(done_model)

        if exc is None:
            if on_usage is not None:
                try:
                    from cost_model import usage_from_response

                    on_usage(usage_from_response(response, done_model, mode=mode))
                except Exception as usage_exc:
                    log.debug("Could not read Gemini usage metadata: %s", usage_exc)
            try:
                plan = _parse_plan(response)
                if not plan.actions and not plan.is_complete:
                    raise ValueError("Model returned no actions and is_complete is false")
                log.info(
                    "Plan (%s): %s (%s actions, complete=%s)",
                    done_model,
                    plan.plan_summary,
                    len(plan.actions),
                    plan.is_complete,
                )
                winner = (done_model, plan)
                break
            except (ValueError, ValidationError) as parse_exc:
                last_error = parse_exc
                log.warning("Plan failed on %s (%s). Waiting for other models.", done_model, parse_exc)
                continue

        last_error = exc
        if _is_timeout_error(exc):
            timeout_failures += 1
            log.warning(
                "Gemini request timed out on %s after %.1fs (%s/%s model attempts).",
                done_model,
                _gemini_request_timeout_seconds(),
                timeout_failures,
                MAX_TIMEOUTED_MODEL_ATTEMPTS,
            )
            if on_status is not None:
                on_status(f"Gemini timed out on {done_model}; trying the next fallback model...")
            continue
        if _is_service_busy(exc):
            log.warning("Gemini is temporarily unavailable on %s (%s). Trying next model.", done_model, exc)
            if on_status is not None:
                on_status(f"Gemini is temporarily unavailable on {done_model}; trying the next fallback model...")
            continue
        if _is_unavailable(exc) or isinstance(exc, (ValueError, ValidationError)):
            log.warning("Plan failed on %s (%s). Trying next model.", done_model, exc)
            continue
        raise

    if winner is not None:
        return winner
    raise last_error or RuntimeError("Gemini plan failed")


def _openai_response_text(response: Any) -> str:
    """Read text from the Responses API while tolerating SDK object variants."""

    direct = getattr(response, "output_text", None)
    if direct:
        return str(direct).strip()

    output = getattr(response, "output", None) or []
    for item in output:
        content = item.get("content") if isinstance(item, dict) else getattr(item, "content", None)
        for part in content or []:
            if isinstance(part, dict):
                value = part.get("text")
            else:
                value = getattr(part, "text", None)
            if isinstance(value, dict):
                value = value.get("value") or value.get("text")
            if value:
                return str(value).strip()
    return ""


def _openai_text_config() -> dict[str, Any]:
    schema = {
        "type": "json_schema",
        "name": "execution_plan",
        "strict": False,
        "schema": _json_schema(),
    }
    config: dict[str, Any] = {"format": schema}
    verbosity = os.getenv("OPENAI_VERBOSITY", "low").strip().lower()
    if verbosity in {"low", "medium", "high"}:
        config["verbosity"] = verbosity
    return config


def _generate_openai(
    client: Any,
    model: str,
    workspace: Workspace,
    prompt: str,
    mode: ModeName,
) -> Any:
    """Make one multimodal Responses API call for the current screenshot."""

    image_data = base64.b64encode(workspace.image_bytes).decode("ascii")
    data_url = f"data:{workspace.mime_type};base64,{image_data}"
    request: dict[str, Any] = {
        "model": model,
        "instructions": prompt_for(mode),
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_image", "image_url": data_url, "detail": "high"},
                    {"type": "input_text", "text": prompt},
                ],
            }
        ],
        "text": _openai_text_config(),
        "max_output_tokens": _max_output_tokens("openai", mode),
    }
    reasoning_effort = os.getenv("OPENAI_REASONING_EFFORT", "").strip().lower()
    if reasoning_effort in {"minimal", "low", "medium", "high"}:
        request["reasoning"] = {"effort": reasoning_effort}
    response = client.responses.create(**request)
    if not _openai_response_text(response):
        raise ValueError("OpenAI returned an empty response")
    return response


def _parse_openai_plan(response: Any) -> ExecutionPlan:
    text = _openai_response_text(response)
    if not text:
        raise ValueError("OpenAI returned no execution plan")
    return ExecutionPlan.model_validate_json(text)


def _decide_openai(
    client: Any,
    workspace: Workspace,
    goal: str,
    models: list[str],
    mode: str | None = None,
    guard: str = "none",
    on_usage: Callable[[Any], None] | None = None,
    on_status: Callable[[str], None] | None = None,
    client_factory: Callable[[], Any] | None = None,
) -> tuple[str, ExecutionPlan]:
    mode = normalize_mode(mode)
    template = user_turn_for(mode)
    prompt = (
        template.replace("{api_w}", str(workspace.api_w))
        .replace("{api_h}", str(workspace.api_h))
        .replace("{guard}", guard)
        .replace("{goal}", goal)
    )
    last_error: Exception | None = None
    timeout_failures = 0
    for model in models:
        try:
            response = _generate_openai(client, model, workspace, prompt, mode)
            if on_usage is not None:
                try:
                    from cost_model import usage_from_response

                    on_usage(usage_from_response(response, model, mode=mode))
                except Exception as exc:
                    log.debug("Could not read OpenAI usage metadata: %s", exc)
            plan = _parse_openai_plan(response)
            if not plan.actions and not plan.is_complete:
                raise ValueError("Model returned no actions and is_complete is false")
            log.info(
                "Plan (%s): %s (%s actions, complete=%s)",
                model,
                plan.plan_summary,
                len(plan.actions),
                plan.is_complete,
            )
            return model, plan
        except Exception as exc:
            last_error = exc
            if _is_timeout_error(exc):
                timeout_failures += 1
                log.warning(
                    "OpenAI request timed out on %s after %.1fs (%s/%s model attempts).",
                    model,
                    _openai_request_timeout_seconds(),
                    timeout_failures,
                    MAX_TIMEOUTED_MODEL_ATTEMPTS,
                )
                if on_status is not None:
                    on_status(
                        f"OpenAI timed out on {model}; trying the next fallback model..."
                    )
                if client_factory is not None:
                    try:
                        client = client_factory()
                        log.info("Rebuilt the OpenAI client after the timed-out request.")
                    except Exception as rebuild_exc:
                        log.debug("Could not rebuild the OpenAI client: %s", rebuild_exc)
                if timeout_failures >= MAX_TIMEOUTED_MODEL_ATTEMPTS:
                    break
                continue
            if _is_service_busy(exc):
                log.warning("OpenAI is temporarily unavailable on %s (%s). Trying next model.", model, exc)
                if on_status is not None:
                    on_status(
                        f"OpenAI is temporarily unavailable on {model}; trying the next fallback model..."
                    )
                continue
            if _is_unavailable(exc) or isinstance(exc, (ValueError, ValidationError)):
                log.warning("Plan failed on %s (%s). Trying next model.", model, exc)
                continue
            raise
    raise last_error or RuntimeError("OpenAI plan failed")


def _backup_model_spec() -> tuple[str, str] | None:
    """Parse VISZMO_BACKUP_MODEL as 'provider:model'.

    A bare model name implies OpenAI, since the primary provider already
    races its own siblings. Unset or empty disables the cross-provider layer.
    """
    raw = os.getenv("VISZMO_BACKUP_MODEL", "").strip()
    if not raw:
        return None
    if ":" in raw:
        provider_raw, model = raw.split(":", 1)
        provider = normalize_provider(provider_raw)
    else:
        provider, model = "openai", raw
    model = model.strip()
    if not model:
        return None
    return provider, model


def _backup_delay_seconds() -> float:
    """Seconds before the cross-provider backup joins the race."""
    raw = os.getenv("VISZMO_BACKUP_DELAY_SECONDS", "14")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = 14.0
    return max(4.0, min(60.0, value))


def decide(
    client: Any,
    types: Any,
    workspace: Workspace,
    goal: str,
    models: list[str],
    mode: str | None = None,
    guard: str = "none",
    on_usage: Callable[[Any], None] | None = None,
    on_status: Callable[[str], None] | None = None,
    provider: str | None = None,
    client_factory: Callable[[], Any] | None = None,
) -> tuple[str, ExecutionPlan]:
    """Plan the next browser action with the configured multimodal provider.

    Layer 1: the primary provider races its own model queue (hedged).
    Layer 2: an optional cross-provider backup model joins after a delay,
    covering provider-wide outages that sibling models cannot escape.
    """
    primary = normalize_provider(provider)

    def run_primary(results_q: queue.Queue, race_won: threading.Event) -> None:
        try:
            if primary == "openai":
                got = _decide_openai(
                    client,
                    workspace,
                    goal,
                    models,
                    mode=mode,
                    guard=guard,
                    on_usage=on_usage,
                    on_status=on_status,
                    client_factory=client_factory or _build_openai_client,
                )
            else:
                got = _decide_gemini(
                    client,
                    types,
                    workspace,
                    goal,
                    models,
                    mode=mode,
                    guard=guard,
                    on_usage=on_usage,
                    on_status=on_status,
                    client_factory=client_factory or _build_gemini_client,
                )
            race_won.set()
            results_q.put(("ok",) + got)
        except Exception as exc:
            # Deliberately no win signal here: a failed primary is exactly
            # when the backup layer should stop waiting and start racing.
            results_q.put(("primary_err", exc))

    backup = _backup_model_spec()
    if backup is None or normalize_provider(backup[0]) == primary:
        results_q: queue.Queue = queue.Queue()
        race_won = threading.Event()
        run_primary(results_q, race_won)
        while True:
            kind, *payload = results_q.get()
            if kind == "ok":
                return payload[0], payload[1]
            raise payload[0]

    backup_provider, backup_model_name = backup
    if on_status is not None:
        on_status(f"Backup ready: {backup_model_name} joins if {primary.title()} stays slow.")
    results_q: queue.Queue = queue.Queue()
    race_won = threading.Event()
    threading.Thread(
        target=run_primary,
        args=(results_q, race_won),
        daemon=True,
        name="viszmo-decide-primary",
    ).start()

    def run_backup() -> None:
        # Wake early only when a plan already won; otherwise the elapsed
        # delay itself is the trigger to join the race.
        if race_won.wait(_backup_delay_seconds()):
            return
        log.info("Cross-provider backup %s is joining the race.", backup_model_name)
        try:
            if backup_provider == "openai":
                backup_client = _build_openai_client()
                got = _decide_openai(
                    backup_client,
                    workspace,
                    goal,
                    [backup_model_name],
                    mode=mode,
                    guard=guard,
                    on_usage=on_usage,
                    on_status=on_status,
                    client_factory=_build_openai_client,
                )
            else:
                backup_client = _build_gemini_client()
                got = _decide_gemini(
                    backup_client,
                    types,
                    workspace,
                    goal,
                    [backup_model_name],
                    mode=mode,
                    guard=guard,
                    on_usage=on_usage,
                    on_status=on_status,
                    client_factory=_build_gemini_client,
                )
            race_won.set()
            results_q.put(("ok",) + got)
        except Exception as exc:
            results_q.put(("backup_err", exc))

    threading.Thread(
        target=run_backup,
        daemon=True,
        name="viszmo-decide-backup",
    ).start()

    primary_error: Exception | None = None
    backup_error: Exception | None = None
    primary_reported = False
    backup_reported = False
    while True:
        kind, *payload = results_q.get()
        if kind == "ok":
            race_won.set()
            log.info("Plan accepted from %s.", payload[0])
            if on_status is not None:
                on_status(f"Plan accepted from {payload[0]}.")
            return payload[0], payload[1]
        if kind == "primary_err":
            primary_error = payload[0]
            primary_reported = True
        elif kind == "backup_err":
            backup_error = payload[0]
            log.warning("Cross-provider backup %s failed: %s", backup_model_name, payload[0])
            backup_reported = True
        if primary_reported and backup_reported:
            break
    raise primary_error or backup_error or RuntimeError("All model layers failed")


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


def _should_force_advance(stuck_frames: int, profile: ModeProfile, question_state: str) -> bool:
    """Only skip answering when the screen is stuck AND the question looks finished."""
    if stuck_frames < profile.force_advance_after:
        return False
    if question_state in {"unsolved", "unknown", "loading"}:
        return False
    return question_state in PROTECTED_STATES | {"answered", "complete"}


def _lock_answer_edits(
    *,
    post_submit_lock: bool,
    force_advance: bool,
    question_state: str,
) -> bool:
    if question_state in PROTECTED_STATES | {"answered"}:
        return True
    if post_submit_lock:
        return True
    if force_advance and question_state not in {"unsolved", "unknown", "loading"}:
        return True
    return False


def _state_recheck_action(profile: ModeProfile) -> dict[str, Any]:
    return Action(
        action="wait",
        target="wait",
        seconds=profile.recheck_wait,
        label="Rechecking question state",
    ).model_dump()


def _same_general_answer_sequence(
    first: dict[str, Any],
    candidate: dict[str, Any],
) -> bool:
    """Return whether two actions belong to one General answer entry.

    General mode is intentionally one logical field at a time. A text field
    may need a focus click followed by typing, and a dropdown may need its
    option selection or a native Enter key. Independent choice clicks must
    never be batched together.
    """
    first_target = str(first.get("target") or "").strip().lower()
    candidate_target = str(candidate.get("target") or "").strip().lower()
    first_name = str(first.get("action") or "").strip().lower()
    candidate_name = str(candidate.get("action") or "").strip().lower()

    if first_target == candidate_target == "answer":
        return candidate_name in {"type", "click"}

    if {first_target, candidate_target} <= {"dropdown", "option"}:
        if candidate_name in {"click", "type"}:
            return True
        return (
            candidate_name == "press_key"
            and str(candidate.get("key") or "").strip().lower() in {"enter", "space"}
        )

    # A native text entry can occasionally be emitted without a focus click.
    return first_name == candidate_name == "type" and first_target == candidate_target


def _gate_general_actions(
    plan: ExecutionPlan,
    safe: list[dict[str, Any]],
    profile: ModeProfile,
) -> list[dict[str, Any]]:
    """Limit General to one active question while keeping the fast path.

    The model can see several cards at once. Keep one answer field per plan so
    a coordinate mistake cannot answer multiple cards, but preserve visible
    Submit/Next controls for that same question to avoid extra model turns.
    """
    if not safe:
        return [_state_recheck_action(profile)]

    first = safe[0]
    first_name = str(first.get("action") or "").strip().lower()

    if plan.question_state == "complete":
        return safe[:1]

    if first_name in {"wait", "scroll"}:
        return safe[:1]

    if plan.question_state == "unsolved":
        if first.get("action") == "click" and first.get("target") == "cancel":
            return safe[:1]
        if is_answer_action(first):
            batch = [first]
            remainder_start = 1
            for index, candidate in enumerate(safe[1:], start=1):
                if _same_general_answer_sequence(first, candidate):
                    batch.append(candidate)
                    remainder_start = index + 1
                else:
                    break
            # Keep navigation that belongs to the same active question. If a
            # second answer target appears, stop before it; never batch into a
            # second visible card.
            for candidate in safe[remainder_start:]:
                if is_answer_action(candidate):
                    break
                if candidate.get("target") in SAFE_NAVIGATION_TARGETS:
                    batch.append(candidate)
                    continue
                break
            return batch

        # An unsolved question must not be advanced just because the model
        # emitted a navigation action. Re-read the page and let the next plan
        # identify the active answer target.
        return [_state_recheck_action(profile)]

    # Once the model has confirmed an answer/graded state, keep all visible
    # navigation controls for that same question in the fast path.
    if first.get("target") in SAFE_NAVIGATION_TARGETS:
        return safe
    if first.get("action") == "click" and first.get("target") == "cancel":
        return safe[:1]
    return [_state_recheck_action(profile)]


def gated_actions(
    plan: ExecutionPlan,
    profile: ModeProfile,
    post_submit_lock: bool = False,
) -> list[dict[str, Any]]:
    """Apply a last-mile no-edit gate before any screen action is executed."""
    if plan.state_confidence < MIN_STATE_CONFIDENCE or plan.question_state in RECHECK_STATES:
        cancel_actions = [
            item.model_dump()
            for item in plan.actions
            if item.action == "click" and item.target == "cancel"
        ]
        if cancel_actions:
            return cancel_actions[:1]
        return [_state_recheck_action(profile)]

    if plan.question_state == "complete":
        return [item.model_dump() for item in plan.actions]

    safe: list[dict[str, Any]] = []
    for item in plan.actions:
        if post_submit_lock:
            if item.action == "type" or item.target in INPUT_TARGETS:
                continue
            if item.action == "click" and item.target not in SAFE_NAVIGATION_TARGETS:
                continue
        elif plan.question_state in PROTECTED_STATES:
            if item.action == "type" or item.target in INPUT_TARGETS:
                continue
            if item.action == "click" and item.target not in SAFE_NAVIGATION_TARGETS:
                continue
        elif plan.question_state == "answered":
            if item.action == "type" or item.target in INPUT_TARGETS:
                continue
            if item.action == "click" and item.target not in SAFE_NAVIGATION_TARGETS:
                continue
        safe.append(item.model_dump())

    if not profile.verify_answer_before_submit:
        return _gate_general_actions(plan, safe, profile)

    if not plan.requires_verification:
        # The model may opt into the fast path for a plain visible field or a
        # safe choice/dropdown selection. Template navigation is always a
        # verification boundary, even if the model misclassifies the field.
        template_risk = any(item.get("target") == "template" for item in safe)
        if not template_risk:
            return safe or [_state_recheck_action(profile)]

    if profile.verify_answer_before_submit:
        # Give the accuracy-sensitive mode a fresh screenshot after answer
        # typing. Choice/graph/dropdown clicks can stay on the fast path and
        # submit/advance in the same turn. Typed math expressions normally get
        # a fresh screenshot before submission; the plan-level fast path above
        # permits only explicitly low-risk plain fields to skip it.
        submit_index = next(
            (
                index
                for index, item in enumerate(safe)
                if item.get("target") in {"submit", "advance"}
            ),
            None,
        )
        if submit_index is not None:
            typed_answer_indexes = [
                index
                for index, item in enumerate(safe[:submit_index])
                if item.get("action") == "type"
            ]
            if typed_answer_indexes:
                end_index = typed_answer_indexes[-1] + 1
                while end_index < submit_index and (
                    safe[end_index].get("target") == "template"
                    or (
                        safe[end_index].get("action") == "press_key"
                        and safe[end_index].get("key") in {"right", "tab", "enter"}
                    )
                ):
                    end_index += 1
                return safe[:end_index]

    return safe or [_state_recheck_action(profile)]


def _type_text(text: str, profile: ModeProfile) -> None:
    from virtual_mouse import get_mouse
    get_mouse().type_text(text, interval=profile.type_interval)
    time.sleep(profile.post_type_delay)


def _normalize_field_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def verify_typed_entry(action: dict[str, Any], profile: ModeProfile) -> str | None:
    """Read back the focused field and retype once if the entry did not land.

    Returns None on success/no-op, or a human-readable warning when even the
    retry did not match — the next screenshot turn remains the final judge.
    """
    expected = _normalize_field_text(action.get("text"))
    if not expected:
        return None
    from virtual_mouse import get_mouse

    mouse = get_mouse()
    read = getattr(mouse, "read_active_text", None)
    if not callable(read):
        # Without a live DOM read there is nothing to compare against;
        # never retype blindly.
        return None
    actual = ""
    for attempt in range(2):
        try:
            info = read()
        except Exception:
            info = None
        if isinstance(info, dict):
            actual = _normalize_field_text(info.get("text"))
            if actual == expected:
                if attempt > 0:
                    log.info("Typed entry verified after one retype.")
                return None
        if attempt == 0:
            log.warning(
                "Typed entry mismatch (expected %r, field has %r); retyping once.",
                expected[:60],
                actual[:60],
            )
            try:
                if profile.replace_answer_before_type:
                    mouse.select_all()
                    time.sleep(profile.focus_after_click * 0.25)
                _type_text(expected, profile)
            except Exception as exc:
                log.debug("Retype failed: %s", exc)
                break
    return f"The typed answer may not have registered correctly (field shows '{actual[:40]}')."


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


def _focus_at(
    x: int,
    y: int,
    profile: ModeProfile,
    kind: str = "click",
    show_overlay: bool = True,
) -> None:
    if show_overlay:
        try:
            from overlay import hide_target, show_target

            show_target(x, y, kind=kind)
            hide_target()
            time.sleep(profile.overlay_pause)
        except Exception:
            pass
    from virtual_mouse import get_mouse
    get_mouse().click(x, y)
    time.sleep(profile.focus_after_click)


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
    profile: ModeProfile,
    should_abort: Callable[[], bool] | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    recent_signatures: list[tuple[Any, ...]] | None = None,
) -> str:
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
                from virtual_mouse import get_mouse
                get_mouse().press_key(str(action.get("key") or "enter"))
                executed_input = True
                time.sleep(profile.post_key_delay)
                continue

            if name == "scroll":
                from virtual_mouse import get_mouse
                direction = str(action.get("direction") or "down")
                clicks = int(action.get("clicks") or 3)
                delta = -clicks if direction == "down" else clicks
                sx = workspace.origin_x + workspace.input_width // 2
                sy = workspace.origin_y + workspace.input_height // 2
                get_mouse().scroll(sx, sy, delta)
                log.info("Scroll %s %s clicks", direction, clicks)
                executed_input = True
                continue

            if name == "type":
                if (
                    profile.replace_answer_before_type
                    and str(action.get("target") or "").strip().lower() == "answer"
                ):
                    from virtual_mouse import get_mouse

                    # Math editors retain the previous expression when a new
                    # value is typed. Replace only the currently focused field
                    # so a retry cannot append a second expression.
                    get_mouse().select_all()
                    time.sleep(profile.focus_after_click * 0.25)
                _type_text(str(action.get("text") or ""), profile)
                executed_input = True
                warning = verify_typed_entry(action, profile)
                if warning:
                    emit({"type": "progress", "text": f"⚠ {warning}"})
                continue

            if name == "click":
                mapped = box_to_coords(list(action.get("box_2d") or []), workspace)
                if mapped is None:
                    log.warning("Skipping click with missing/invalid box_2d: %r", action.get("box_2d"))
                    time.sleep(profile.missing_box_wait)
                    continue
                x, y = mapped
                snapped = _snap_click_to_element(x, y, action.get("box_2d"), workspace)
                if snapped is not None:
                    x, y = snapped
                log.info("Click at assignment-page coordinates (%s, %s)", x, y)
                _focus_at(x, y, profile, show_overlay=workspace.coordinate_space == "screen")
                if action.get("target") == "dropdown":
                    time.sleep(profile.dropdown_settle)
                executed_input = True
                continue

            log.warning("Skipping unknown action %r", name)
        except Exception as exc:
            log.warning("Action failed (%s); continuing. %s", name, exc)
            time.sleep(profile.missing_box_wait)
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
    mode: str = "math",
    autonomy: str = "autopilot",
    snip_box: tuple[float, float, float, float] | None = None,
    usage_token: str | None = None,
    usage_origin: str | None = None,
) -> str:
    from virtual_mouse import configured_cdp_target, get_mouse, reset_mouse
    import chrome_session

    def emit(event: dict[str, Any]) -> None:
        if on_event is not None:
            on_event(event)

    if chrome_session.assigned_target() is None and not configured_cdp_target():
        emit({
            "type": "error",
            "text": "No managed assignment browser is connected. Launch the assignment browser, then press Run again.",
        })
        return "error"

    # The panel and the dashboard WebSocket are independent task managers;
    # this OS-level guard is the one choke point that keeps two workers from
    # driving the same tab at once.
    guard = _SingleTaskGuard()
    if not guard.acquire():
        emit({
            "type": "error",
            "text": "Another Viszmo task is already running in this browser. Stop it first, then press Run again.",
        })
        return "error"

    mouse: Any | None = None
    try:
        reset_mouse()
        mouse = get_mouse()
        if getattr(mouse, "mode", "") != "cdp":
            emit({
                "type": "error",
                "text": (
                    "Viszmo could not connect to its managed assignment browser. "
                    "Launch the assignment browser again, then press Run."
                ),
            })
            return "error"
        return _run_oneshot_inner(
            goal=goal,
            sidebar_width=sidebar_width,
            should_abort=should_abort,
            on_event=on_event,
            mode=mode,
            autonomy=autonomy,
            snip_box=snip_box,
            usage_token=usage_token,
            usage_origin=usage_origin,
        )
    except Exception as exc:
        log.exception("Background browser session failed")
        emit({"type": "error", "text": str(exc)})
        return "error"
    finally:
        reset_mouse()
        guard.release()


def _run_oneshot_inner(
    goal: str,
    sidebar_width: int = SIDEBAR_WIDTH,
    should_abort: Callable[[], bool] | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    mode: str = "math",
    autonomy: str = "autopilot",
    snip_box: tuple[float, float, float, float] | None = None,
    usage_token: str | None = None,
    usage_origin: str | None = None,
) -> str:
    def emit(event: dict[str, Any]) -> None:
        if on_event is not None:
            on_event(event)

    def aborted() -> bool:
        return bool(should_abort and should_abort())

    mode = normalize_mode(mode)
    autonomy = normalize_autonomy(autonomy)
    profile = profile_for(mode)
    usage_client = DesktopUsageClient(usage_token, usage_origin) if usage_token is not None else None
    provider = normalize_provider()
    types: Any | None = None

    try:
        if provider == "openai":
            client = _build_openai_client()
            models = _openai_model_queue(mode)
        else:
            from google.genai import types as gemini_types

            types = gemini_types
            client = _build_gemini_client()
            models = _model_queue(mode)
    except Exception as exc:
        log.error("LLM client failed: %s", exc)
        emit({"type": "error", "text": str(exc)})
        return "error"

    def refresh_llm_client() -> None:
        """Replace the provider client so a recovery never reuses a socket
        that a timed-out request may have left wedged."""
        nonlocal client
        try:
            if provider == "openai":
                client = _build_openai_client()
            else:
                client = _build_gemini_client()
            log.info("Rebuilt the %s client before retrying.", provider.title())
        except Exception as exc:
            log.debug("Could not rebuild the LLM client: %s", exc)

    log.info("Launch provider=%s mode=%s goal=%s", provider, mode, goal[:120])

    def note_progress(plan_obj: Any) -> None:
        """Persist one answered item against the live assignment URL.

        Records even when the model omitted answer_text — the resumed-run
        context depends on the count, and choice turns frequently skip
        restating what was clicked.
        """
        try:
            from virtual_mouse import get_mouse

            url = get_mouse().page_url()
            if not url:
                return
            import task_memory

            task_memory.record_answer(
                url,
                str(getattr(plan_obj, "plan_summary", "") or ""),
                str(getattr(plan_obj, "answer_text", "") or ""),
            )
        except Exception as exc:
            log.debug("Progress note failed: %s", exc)

    def close_progress_book() -> None:
        try:
            from virtual_mouse import get_mouse

            url = get_mouse().page_url()
            if url:
                import task_memory

                task_memory.mark_complete(url)
        except Exception as exc:
            log.debug("Progress close failed: %s", exc)

    try:
        from virtual_mouse import get_mouse

        prior_url = get_mouse().page_url()
        if prior_url:
            import task_memory

            prior_count = task_memory.prior_answer_count(prior_url)
            prior_entry = task_memory.load(prior_url)
            if prior_entry.get("completed"):
                emit({
                    "type": "log",
                    "text": "This assignment was previously completed here; running a fresh pass anyway.",
                })
            elif prior_count:
                emit({
                    "type": "log",
                    "text": f"Resuming: {prior_count} answer(s) from earlier runs are recorded for this assignment.",
                })
    except Exception as exc:
        log.debug("Resume context unavailable: %s", exc)

    solved_any = False
    previous_fingerprint: bytes | None = None
    submitted_fingerprint: bytes | None = None
    last_typed_input_fingerprint: bytes | None = None
    last_typed_input_identity: tuple[str, str] | None = None
    repeated_input_attempts = 0
    last_general_answer_fingerprint: bytes | None = None
    last_general_answer_identity: tuple[Any, ...] | None = None
    general_stalled_advance_attempts = 0
    unchanged_frames = 0
    action_history: list[tuple[Any, ...]] = []
    usage_records: list[Any] = []
    copilot_answers: list[dict[str, Any]] = []
    copilot_seen_answers: set[tuple[str, str]] = set()
    timeout_recovery_attempts = 0
    service_recovery_attempts = 0
    unchanged_llm_skips = 0
    last_frame_actions_ran = False
    max_unchanged_llm_skips = _unchanged_llm_skip_limit()
    run_started_monotonic = time.monotonic()
    total_plans = 0
    total_answer_entries = 0
    total_submits = 0
    total_advances = 0
    total_timeout_recoveries = 0
    total_service_recoveries = 0
    total_submits_held = 0
    dry_run_announced = False
    consecutive_dry_holds = 0

    decision_goal = goal
    if autonomy == "copilot":
        decision_goal = (
            f"{goal}\n"
            "COPILOT BATCH REVIEW: Do not rely on answer-entry actions being executed. "
            "Always return answer_text for the current question. If a visible non-submit "
            "Next/Continue/right-arrow can move to another question without entering or "
            "submitting an answer, include that navigation action after the answer plan. "
            "Never treat Submit/Check as batch navigation."
        )
    if snip_box is not None:
        decision_goal = (
            f"{goal}\n"
            "SNIP REVIEW: The screenshot is a user-selected single question. Return answer_text for that "
            "question only. Do not propose page interaction, scrolling, navigation, answer entry, Submit, or Check. "
            "Use one short wait action as the required plan action; it will not be run."
        )

    def record_usage(record: Any) -> None:
        usage_records.append(record)
        try:
            usage = record.as_dict()
        except AttributeError:
            usage = dict(record)
        if usage.get("pricing_known", True):
            cost_text = f"USD {float(usage.get('estimated_cost_usd') or 0.0):.6f}"
        else:
            cost_text = "unknown"
        emit({
            "type": "usage",
            "usage": usage,
            "text": (
                f"API {usage.get('model', 'model')} • "
                f"{usage.get('input_tokens', 0)} in / {usage.get('output_tokens', 0)} out "
                f"• est. {cost_text}"
            ),
        })

    def emit_usage_summary() -> None:
        try:
            from cost_model import sum_usage

            summary = sum_usage(usage_records)
        except Exception:
            summary = None
        if summary is not None:
            emit({
                "type": "usage_summary",
                "usage": summary,
                "text": f"Task API estimate: USD {summary['estimated_cost_usd']:.6f}",
            })
        elapsed_seconds = max(0, int(time.monotonic() - run_started_monotonic))
        emit({
            "type": "log",
            "run_report": {
                "plans": total_plans,
                "answer_entries": total_answer_entries,
                "submits": total_submits,
                "submits_held": total_submits_held,
                "advances": total_advances,
                "timeout_recoveries": total_timeout_recoveries,
                "service_recoveries": total_service_recoveries,
                "elapsed_seconds": elapsed_seconds,
            },
            "text": (
                f"Run report: {total_plans} model turn(s) • {total_answer_entries} answer(s) typed/selected • "
                f"{total_submits} submit(s) • {total_advances} advance(s) • "
                f"{total_timeout_recoveries} timeout + {total_service_recoveries} busy recoveries • "
                f"{elapsed_seconds}s elapsed"
            ),
        })
        if total_submits_held:
            emit({
                "type": "log",
                "text": (
                    f"Dry run complete: {total_submits_held} Submit click(s) were held back. "
                    "Review the page, then commit when satisfied."
                ),
            })

    def consume_desktop_question(request_id: str) -> str:
        if usage_client is None:
            return "allowed"
        try:
            result = usage_client.consume(request_id)
        except DesktopUsageError as exc:
            emit({"type": "error", "text": str(exc)})
            return "error"

        if not result.get("allowed"):
            emit({
                "type": "paywall",
                "remaining": result.get("remaining", 0),
                "allowance": result.get("allowance", 10),
                "url": usage_client.pricing_url(),
                "text": result.get(
                    "error",
                    "Your 10 free desktop questions are used. Subscribe to continue.",
                ),
            })
            return "paywall"

        emit({
            "type": "desktop_usage",
            "used": result.get("used"),
            "remaining": result.get("remaining"),
            "allowance": result.get("allowance"),
            "subscribed": result.get("subscribed", False),
            "unlimited": result.get("unlimited", False),
        })
        if not result.get("subscribed") and int(result.get("remaining") or 0) <= 0:
            emit({
                "type": "paywall",
                "remaining": 0,
                "allowance": result.get("allowance", 10),
                "url": usage_client.pricing_url(),
                "text": "You used your 10 free desktop questions. Subscribe to continue.",
            })
            return "paywall"
        return "allowed"

    def recover_after_timeout(exc: Exception) -> str:
        """Keep the browser open and retry after a timed-out browser/model call."""
        nonlocal timeout_recovery_attempts, total_timeout_recoveries

        if aborted():
            emit({"type": "aborted", "text": "Stopped by user."})
            return "aborted"

        timeout_recovery_attempts += 1
        total_timeout_recoveries += 1
        if timeout_recovery_attempts > MAX_TIMEOUT_RECOVERY_ATTEMPTS:
            message = (
                "A request did not respond after several attempts. The assignment was left open; "
                "press Run to retry this question."
            )
            log.error("%s Last error: %s", message, exc)
            emit({"type": "error", "text": message})
            return "error"

        emit({
            "type": "progress",
            "text": (
                "The browser/model request is taking longer than expected; keeping the assignment open "
                f"and recovering the page ({timeout_recovery_attempts}/{MAX_TIMEOUT_RECOVERY_ATTEMPTS})..."
            ),
        })
        try:
            # A timed-out call can leave a site dialog or browser confirmation
            # open. Escape is a safe local recovery action: it dismisses that
            # UI without submitting or changing the answer, then the next
            # loop iteration recaptures the same question.
            from virtual_mouse import get_mouse, reset_mouse

            # A CDP read timeout closes the old socket inside CdpMouse. Force
            # a fresh connection before sending the recovery key.
            reset_mouse()
            get_mouse().press_key("escape")
            emit({
                "type": "progress",
                "text": "Dismissed the active popup; retrying the current question...",
            })
        except Exception as recovery_exc:
            log.warning("Could not dismiss a popup after timeout: %s", recovery_exc)
            emit({
                "type": "progress",
                "text": "No popup was dismissed; retrying the current question...",
            })
        if not _sleep_with_abort(profile.recheck_wait, aborted):
            emit({"type": "aborted", "text": "Stopped by user."})
            return "aborted"
        return "retry"

    def recover_after_service_busy(exc: Exception) -> str:
        """Leave the question untouched while provider capacity recovers."""
        nonlocal service_recovery_attempts, total_service_recoveries

        if aborted():
            emit({"type": "aborted", "text": "Stopped by user."})
            return "aborted"

        service_recovery_attempts += 1
        total_service_recoveries += 1
        if service_recovery_attempts > MAX_SERVICE_RECOVERY_ATTEMPTS:
            message = (
                f"{provider.title()} is still unavailable after trying the fallback models. "
                "The assignment was left open; press Run to retry this question."
            )
            log.error("%s Last error: %s", message, exc)
            emit({"type": "error", "text": message})
            return "error"

        emit({
            "type": "progress",
            "text": (
                f"{provider.title()} is busy on all configured models; keeping the assignment open and "
                f"retrying in {SERVICE_RECOVERY_DELAY_SECONDS:.0f}s "
                f"({service_recovery_attempts}/{MAX_SERVICE_RECOVERY_ATTEMPTS})..."
            ),
        })
        if not _sleep_with_abort(SERVICE_RECOVERY_DELAY_SECONDS, aborted):
            emit({"type": "aborted", "text": "Stopped by user."})
            return "aborted"
        return "retry"

    for iteration in range(1, MAX_ITERATIONS + 1):
        if aborted():
            emit({"type": "aborted", "text": "Stopped by user."})
            return "aborted"

        emit({"type": "progress", "text": f"Capturing assignment page ({iteration})..."})
        try:
            workspace = capture_high_res_workspace(sidebar_width, snip_box=snip_box)
        except Exception as exc:
            if _is_timeout_error(exc):
                recovery_status = recover_after_timeout(exc)
                if recovery_status == "retry":
                    refresh_llm_client()
                    continue
                return recovery_status
            log.exception("Assignment page capture failed")
            emit({"type": "error", "text": f"Could not capture the assignment browser: {exc}"})
            return "error"
        fingerprint = workspace_fingerprint(workspace)
        # Keep the duplicate-input identity across small screenshot changes.
        # Typing an answer commonly changes focus/caret/validation pixels, so
        # clearing this state on any fingerprint change allowed the model to
        # re-enter the same answer on the same question.
        post_submit_lock = submitted_fingerprint is not None and fingerprint == submitted_fingerprint
        if submitted_fingerprint is not None and not post_submit_lock:
            submitted_fingerprint = None
        log.info(
            "Iteration %s/%s workspace %sx%s %s %s KB origin=(%s,%s)",
            iteration,
            MAX_ITERATIONS,
            workspace.width,
            workspace.height,
            workspace.mime_type,
            max(1, len(workspace.image_bytes) // 1024),
            workspace.origin_x,
            workspace.origin_y,
        )

        stuck_frames = unchanged_frames
        if previous_fingerprint is not None and fingerprint == previous_fingerprint:
            unchanged_frames += 1
            stuck_frames = unchanged_frames
            if unchanged_frames >= profile.max_unchanged_frames:
                emit_usage_summary()
                if mode == "general":
                    emit({
                        "type": "error",
                        "text": (
                            "General mode stopped safely because the assignment page did not visibly change. "
                            "No additional answer or navigation was sent; review this question and press Launch again."
                        ),
                    })
                    return "error"
                emit({"type": "done", "text": "Could not reach the next question from this screen. Open the next item, then press Launch."})
                return "done"
        else:
            unchanged_frames = 0
            stuck_frames = 0
            unchanged_llm_skips = 0
            # Action coordinates are meaningful only for the current visible
            # page. Reusing this history after navigation can mistake the same
            # option row on a new card for a duplicate and force an unsafe
            # advance.
            action_history.clear()
        previous_fingerprint = fingerprint

        same_after_general_answer = (
            mode == "general"
            and last_general_answer_fingerprint is not None
            and fingerprint == last_general_answer_fingerprint
        )
        if mode == "general" and last_general_answer_identity is not None and not same_after_general_answer:
            # A changed page means the previous answer caused a visible
            # transition. Do not carry that answer identity into the next
            # question, where the same option may be valid again.
            last_general_answer_fingerprint = None
            last_general_answer_identity = None
            general_stalled_advance_attempts = 0

        try:
            emit({"type": "progress", "text": f"Evaluating workspace ({iteration})..."})
            if same_after_general_answer:
                if general_stalled_advance_attempts >= GENERAL_FAST_ADVANCE_ATTEMPTS:
                    emit({
                        "type": "error",
                        "text": (
                            "The answer was sent, but the assignment page did not advance after "
                            f"{GENERAL_FAST_ADVANCE_ATTEMPTS} fast attempts. The assignment was left open."
                        ),
                    })
                    return "error"
                general_stalled_advance_attempts += 1
                emit({
                    "type": "progress",
                    "text": "Answer already sent; using the fast next-question recovery...",
                })
                model = "local-navigation"
                plan = ExecutionPlan(
                    plan_summary="Answer already sent; advancing without another model turn",
                    question_state="answered",
                    state_confidence=1.0,
                    actions=[_advance_fallback()],
                )
                force_advance = False
            else:
                if (
                    snip_box is None
                    and autonomy != "copilot"
                    and stuck_frames > 0
                    and last_frame_actions_ran
                    and unchanged_llm_skips < max_unchanged_llm_skips
                ):
                    # The last executed plan left the pixels identical. A new
                    # model turn on the same screenshot mostly repeats itself,
                    # so wait locally first; the model is consulted again as
                    # soon as the page changes or the skip budget runs out.
                    unchanged_llm_skips += 1
                    timeout_recovery_attempts = 0
                    service_recovery_attempts = 0
                    emit({
                        "type": "progress",
                        "text": (
                            "The page looks unchanged; letting it settle "
                            f"({unchanged_llm_skips}/{max_unchanged_llm_skips}) before the next model turn..."
                        ),
                    })
                    settle_wait = profile.page_settle + profile.recheck_wait
                    if not _sleep_with_abort(settle_wait, aborted):
                        emit({"type": "aborted", "text": "Stopped by user."})
                        return "aborted"
                    continue

                guard_parts: list[str] = []
                if post_submit_lock:
                    guard_parts.append(
                        "Previous turn clicked Submit/Check and this is the same screenshot; do not edit answer targets."
                    )
                if stuck_frames >= profile.force_advance_after and mode != "general":
                    guard_parts.append(
                        "The screenshot looks unchanged from the last turn. "
                        "If the question is already answered, graded, or finished, only submit or advance. "
                        "If it is still unsolved, answer it now."
                    )
                guard = " ".join(guard_parts) or "none"
                model, plan = decide(
                    client,
                    types,
                    workspace,
                    decision_goal,
                    models,
                    mode=mode,
                    guard=guard,
                    on_usage=record_usage,
                    on_status=lambda text: emit({"type": "progress", "text": text}),
                    provider=provider,
                )
                # General keeps the fast route: navigation must come from the
                # model plan unless this is the bounded post-answer recovery
                # above.
                force_advance = (
                    _should_force_advance(stuck_frames, profile, plan.question_state)
                    if mode != "general"
                    else False
                )
            timeout_recovery_attempts = 0
            service_recovery_attempts = 0
            total_plans += 1
            emit({
                "type": "log",
                "text": f"State: {plan.question_state} ({plan.state_confidence:.0%}) • {plan.plan_summary}",
                "model": model,
                "question_state": plan.question_state,
                "state_confidence": plan.state_confidence,
            })
        except Exception as exc:
            if _is_timeout_error(exc):
                recovery_status = recover_after_timeout(exc)
                if recovery_status == "retry":
                    refresh_llm_client()
                    continue
                return recovery_status
            if _is_service_busy(exc):
                recovery_status = recover_after_service_busy(exc)
                if recovery_status == "retry":
                    continue
                return recovery_status
            log.error("LLM call failed: %s", exc)
            emit({"type": "error", "text": str(exc)})
            return "error"

        if aborted():
            emit({"type": "aborted", "text": "Stopped by user."})
            return "aborted"

        if autonomy == "copilot":
            answer = copilot_answer_text(plan)
            if (
                answer
                and plan.state_confidence >= MIN_STATE_CONFIDENCE
                and plan.question_state not in RECHECK_STATES
                and plan.question_state not in {"complete", "blocked"}
            ):
                answer_key = (
                    " ".join(str(plan.plan_summary or "").split()).casefold(),
                    answer.casefold(),
                )
                if answer_key not in copilot_seen_answers:
                    usage_status = consume_desktop_question(
                        desktop_usage_request_id(
                            fingerprint,
                            plan,
                            [],
                        )
                    )
                    if usage_status != "allowed":
                        emit_usage_summary()
                        return usage_status
                    copilot_seen_answers.add(answer_key)
                    record = {
                        "number": len(copilot_answers) + 1,
                        "question": " ".join(str(plan.plan_summary or "").split()),
                        "answer": answer,
                        "model": model,
                        "question_state": plan.question_state,
                    }
                    copilot_answers.append(record)
                    emit({"type": "copilot_answer", **record})

            if snip_box is not None:
                emit_usage_summary()
                emit({
                    "type": "copilot_complete",
                    "count": len(copilot_answers),
                    "text": (
                        "Snip answer ready."
                        if copilot_answers
                        else "No answer could be identified from this snip. Try drawing a tighter box around the question."
                    ),
                })
                return "done"

            if plan.is_complete:
                emit_usage_summary()
                emit({
                    "type": "copilot_complete",
                    "count": len(copilot_answers),
                    "text": (
                        f"Collected {len(copilot_answers)} answer"
                        f"{'s' if len(copilot_answers) != 1 else ''}."
                    ),
                })
                return "done"

            navigation = copilot_navigation_actions(plan, profile)
            if not navigation:
                emit_usage_summary()
                emit({
                    "type": "copilot_complete",
                    "count": len(copilot_answers),
                    "text": (
                        f"Collected {len(copilot_answers)} answer"
                        f"{'s' if len(copilot_answers) != 1 else ''}. "
                        "No safe non-submit navigation control was visible for the next question."
                    ),
                })
                return "done"

            status = execute_actions(
                navigation,
                workspace,
                profile,
                should_abort=should_abort,
                on_event=on_event,
                recent_signatures=action_history,
            )
            if status in {"aborted", "error"}:
                return status
            if status == "repeat":
                emit_usage_summary()
                emit({
                    "type": "copilot_complete",
                    "count": len(copilot_answers),
                    "text": (
                        f"Collected {len(copilot_answers)} answer"
                        f"{'s' if len(copilot_answers) != 1 else ''}, but navigation repeated."
                    ),
                })
                return "done"
            if not _sleep_with_abort(profile.navigation_settle, aborted):
                emit({"type": "aborted", "text": "Stopped by user."})
                return "aborted"
            continue

        lock_answers = _lock_answer_edits(
            post_submit_lock=post_submit_lock,
            force_advance=force_advance,
            question_state=plan.question_state,
        )
        actions = gated_actions(plan, profile, post_submit_lock=lock_answers)
        if autonomy == "dry_run" and not dry_run_announced:
            emit({
                "type": "log",
                "text": "Dry run: every answer is filled but Submit buttons stay untouched.",
            })
            dry_run_announced = True
        actions, submits_held = apply_dry_run(actions, autonomy)
        if submits_held:
            total_submits_held += submits_held
            consecutive_dry_holds += 1
            emit({
                "type": "progress",
                "text": f"Dry run: held {submits_held} Submit click(s); nothing committed.",
            })
            if not any(item.get("action") in {"wait", "done"} or item.get("box_2d") or item.get("action") == "press_key" or item.get("action") == "scroll" for item in actions):
                actions = [_state_recheck_action(profile)]
            if consecutive_dry_holds >= 3:
                # The model keeps proposing submission and dry-run keeps
                # refusing — that is the natural end of a rehearsal. Conclude
                # instead of looping on the final screen.
                emit_usage_summary()
                close_progress_book()
                emit({
                    "type": "done",
                    "text": (
                        f"Dry run complete: {total_submits_held} Submit click(s) were held back. "
                        "Review the page, then commit when satisfied."
                    ),
                })
                return "done"
        elif any(
            item.get("action") in {"click", "type", "press_key", "scroll"} for item in actions
        ):
            consecutive_dry_holds = 0
        if plan.is_complete and plan.state_confidence >= MIN_STATE_CONFIDENCE and not force_advance:
            emit_usage_summary()
            close_progress_book()
            emit({"type": "done", "text": "Assignment completed successfully!"})
            return "done"
        if force_advance and plan.is_complete:
            log.info("Ignoring is_complete on an unchanged finished question; advancing instead.")

        actions = actions[:MAX_ACTIONS]
        has_progress = any(item.get("target") in SAFE_NAVIGATION_TARGETS for item in actions)
        if force_advance and not has_progress:
            emit({"type": "progress", "text": "This question is already finished; going to the next one..."})
            actions = [_advance_fallback()]

        if same_after_general_answer and last_general_answer_identity is not None:
            matching_answer = any(
                answer_action_identity(item) == last_general_answer_identity
                for item in actions
            )
            if matching_answer:
                actions, removed = remove_repeated_answer_actions(
                    actions,
                    last_general_answer_identity,
                )
                if removed:
                    emit({
                        "type": "progress",
                        "text": "The same answer was requested again without visual confirmation; waiting instead of advancing...",
                    })
                    actions = [_state_recheck_action(profile)]

        if last_typed_input_identity is not None:
            matching_input = any(
                input_action_identity(item) == last_typed_input_identity for item in actions
            )
            if matching_input:
                repeated_input_attempts += 1
                actions, removed = remove_repeated_input_actions(actions, last_typed_input_identity)
                if removed <= 0:
                    last_typed_input_fingerprint = None
                    last_typed_input_identity = None
                    repeated_input_attempts = 0
                    matching_input = False
                else:
                    matching_input = True
                emit({
                    "type": "progress",
                    "text": (
                        "Skipping duplicate entry; the answer was already sent to this input. "
                        "Rechecking the current question..."
                    ),
                })
                if not actions:
                    if repeated_input_attempts >= 2:
                        emit({
                            "type": "error",
                            "text": (
                                "The same answer was requested repeatedly, but the page did not visibly "
                                "advance. The duplicate entry was blocked; check this field and press Run again."
                            ),
                        })
                        return "error"
                    if not _sleep_with_abort(profile.recheck_wait, aborted):
                        emit({"type": "aborted", "text": "Stopped by user."})
                        return "aborted"
                    continue
            else:
                last_typed_input_fingerprint = None
                last_typed_input_identity = None
                repeated_input_attempts = 0

        plan_sigs = [action_signature(item) for item in actions if item.get("action") not in {"wait", "done"}]
        if plan_sigs and action_history and plan_sigs == action_history[-len(plan_sigs) :]:
            repeated_answer_actions = [item for item in actions if is_answer_action(item)]
            if mode == "general" and repeated_answer_actions:
                # A repeated answer plan is not permission to skip the card.
                # Re-read it and stop safely if the selection still cannot be
                # confirmed.
                log.warning("Repeated General answer plan; rechecking without navigation.")
                emit({
                    "type": "progress",
                    "text": "The answer plan repeated; rechecking the current card without advancing...",
                })
                actions = [_state_recheck_action(profile)]
            elif plan.question_state == "unsolved":
                log.warning("Same unsolved plan repeated; waiting for the UI to catch up.")
                emit({"type": "progress", "text": "Click may not have registered; trying again..."})
                if not _sleep_with_abort(profile.recheck_wait, aborted):
                    emit({"type": "aborted", "text": "Stopped by user."})
                    return "aborted"
                continue
            # A repeated navigation plan is also not proof that the control
            # registered. Recheck before permitting another navigation click.
            if any(item.get("target") in SAFE_NAVIGATION_TARGETS for item in actions):
                log.warning("Navigation plan repeats without a page transition; rechecking.")
                emit({"type": "progress", "text": "Navigation did not change the page; rechecking before trying again..."})
                actions = [_state_recheck_action(profile)]

        answer_actions = [item for item in actions if is_answer_action(item)]
        submit_actions = [
            item for item in actions if item.get("target") == "submit"
        ]
        if autonomy != "dry_run" and (answer_actions or submit_actions):
            # Authorize before touching the page so an exhausted account can
            # never execute an eleventh answer and only discover the paywall
            # afterward. The request id makes safe retries idempotent.
            usage_status = consume_desktop_question(
                desktop_usage_request_id(fingerprint, plan, actions)
            )
            if usage_status != "allowed":
                emit_usage_summary()
                return usage_status

        status = execute_actions(
            actions,
            workspace,
            profile,
            should_abort=should_abort,
            on_event=on_event,
            recent_signatures=action_history,
        )
        # A turn whose execution left identical pixels earns one local settle
        # window on the next iteration before another paid model turn.
        last_frame_actions_ran = status in {"done", "empty"}
        advance_actions = [
            item
            for item in actions
            if item.get("target") == "advance"
            or (
                str(item.get("action") or "").strip().lower() == "press_key"
                and str(item.get("key") or "").strip().lower() == "right"
            )
        ]
        scroll_actions = [
            item
            for item in actions
            if str(item.get("action") or "").strip().lower() == "scroll"
        ]
        if status not in {"aborted", "error", "repeat"}:
            total_answer_entries += len(answer_actions)
            total_submits += len(submit_actions)
            total_advances += len(advance_actions)
            if answer_actions or submit_actions:
                note_progress(plan)
        typed_inputs = [input_action_identity(item) for item in actions]
        typed_inputs = [item for item in typed_inputs if item is not None]
        if status not in {"aborted", "error", "repeat"} and typed_inputs:
            last_typed_input_fingerprint = fingerprint
            last_typed_input_identity = typed_inputs[-1]
            repeated_input_attempts = 0
        if status not in {"aborted", "error"} and any(item.get("target") == "submit" for item in actions):
            submitted_fingerprint = fingerprint
        if mode == "general" and status not in {"aborted", "error", "repeat"}:
            if answer_actions and not submit_actions and not advance_actions:
                answer_identity = answer_action_identity(answer_actions[-1])
                if answer_identity is not None:
                    last_general_answer_fingerprint = fingerprint
                    last_general_answer_identity = answer_identity
            elif submit_actions:
                # Submit has its own same-frame lock. Keep a pending answer
                # through a pure advance attempt so a failed right-arrow does
                # not reopen the answer-click loop on the next iteration.
                last_general_answer_fingerprint = None
                last_general_answer_identity = None
        if status in {"aborted", "error"}:
            return status
        if status == "complete":
            emit_usage_summary()
            close_progress_book()
            emit({"type": "done", "text": plan.actions[-1].reason if plan.actions and plan.actions[-1].reason else "Assignment completed successfully!"})
            return "done"
        if status == "repeat":
            if mode == "general":
                emit({
                    "type": "error",
                    "text": (
                        "General mode blocked a duplicate action because the page did not confirm the prior step. "
                        "The assignment was left open for review."
                    ),
                })
                return "error"
            if plan.question_state == "unsolved":
                emit({"type": "progress", "text": "Retrying the same click..."})
                if not _sleep_with_abort(profile.recheck_wait, aborted):
                    emit({"type": "aborted", "text": "Stopped by user."})
                    return "aborted"
                continue
            emit({"type": "progress", "text": "That click already happened; moving to the next question..."})
            from virtual_mouse import get_mouse
            get_mouse().press_key("right")
            time.sleep(profile.post_key_delay)
            solved_any = True
            if not _sleep_with_abort(profile.page_settle, aborted):
                emit({"type": "aborted", "text": "Stopped by user."})
                return "aborted"
            continue
        if status == "empty":
            if plan.state_confidence < MIN_STATE_CONFIDENCE or plan.question_state in RECHECK_STATES:
                emit({"type": "progress", "text": "Question state is unclear; checking the same screen again..."})
                if not _sleep_with_abort(profile.recheck_wait, aborted):
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
            emit_usage_summary()
            close_progress_book()
            emit({"type": "done", "text": "Assignment completed successfully!"})
            return "done"

        if advance_actions:
            emit({"type": "progress", "text": "Advancing to the next question..."})
            transition_wait = profile.navigation_settle
        elif submit_actions:
            emit({"type": "progress", "text": "Submitted answer; checking the next state..."})
            transition_wait = profile.page_settle
        elif answer_actions:
            emit({"type": "progress", "text": "Answer selected; checking the question state..."})
            transition_wait = profile.recheck_wait if mode == "general" else profile.page_settle
        elif scroll_actions:
            emit({"type": "progress", "text": "Scroll applied; checking the next visible item..."})
            transition_wait = profile.page_settle
        else:
            emit({"type": "progress", "text": "Checking the next question state..."})
            transition_wait = profile.recheck_wait
        if not _sleep_with_abort(transition_wait, aborted):
            emit({"type": "aborted", "text": "Stopped by user."})
            return "aborted"

    emit_usage_summary()
    emit({"type": "done", "text": f"Stopped after {MAX_ITERATIONS} steps. Press Launch to continue."})
    return "done"


class _SingleTaskGuard:
    """Cross-process mutual exclusion for task execution.

    Panel and dashboard tasks run in separate worker processes; an OS-level
    file lock is the only reliable way to guarantee one driver per managed
    tab. Locks auto-release when a process dies, so crashes cannot wedge it.
    """

    _path = Path(os.getenv("VISZMO_TASK_LOCK_FILE") or (Path(tempfile.gettempdir()) / "viszmo-task.lock"))

    def __init__(self) -> None:
        self._fh: Any = None

    def acquire(self) -> bool:
        try:
            self._fh = open(self._path, "a+b")
        except OSError:
            return True
        try:
            import msvcrt

            msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except ImportError:
            try:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except ImportError:
                return True
        except (OSError, ValueError):
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None
            return False

    def release(self) -> None:
        if self._fh is None:
            return
        handle = self._fh
        self._fh = None
        try:
            handle.seek(0)
            try:
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            except ImportError:
                try:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except ImportError:
                    pass
        except (OSError, ValueError):
            pass
        finally:
            try:
                handle.close()
            except OSError:
                pass


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
