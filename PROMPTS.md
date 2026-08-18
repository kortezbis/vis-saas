# Viszmo — universal educational-agent prompting

The active path is `agent_engine.py`: screenshot → one structured-JSON call → PyAutoGUI actions → repeat until the assignment is complete. The sidebar does not ask the user to select a subject; the model infers it from the workspace screenshot.

## System prompt

`SYSTEM_PROMPT` is subject-agnostic and covers:

- quantitative work across algebra, calculus, statistics, physics, chemistry, and other STEM;
- reading comprehension, literature, history, government, civics, and contextual questions;
- vocabulary, matching, true/false, dropdowns, radio buttons, checkboxes, short answers, and mixed assignments;
- state inspection, filled/graded/locked-field protection, submission, advancement, and completion detection;
- per-field action sequencing and platform-specific math/text formatting.

Every plan must classify `question_state` and `state_confidence` before proposing actions. The executor rejects answer edits for answered, graded, or locked states and turns low-confidence/unknown states into a short re-check wait.

The prompt requires internal reasoning only. The model returns no markdown, derivation, or conversational explanation.

## User turn

Each lossless PNG capture is accompanied by:

```text
Analyze the current workspace screenshot and produce the next executable plan.
Infer the subject and UI state from the screenshot itself; do not use a subject mode.
Skip filled, graded, locked, or checkmarked fields. If the active question is solved, advance immediately.
Image size: {api_w} x {api_h} (native PNG, sidebar cropped).
Prior execution guard: {guard}
Goal: {goal}
```

## JSON schema

Gemini receives `response_schema=ExecutionPlan` and `temperature=0.0`.

```text
ExecutionPlan
  plan_summary: string       short operational label
  question_state: string      unsolved | answered | graded_correct | graded_incorrect | locked | loading | complete | blocked | unknown
  state_confidence: number    0–1 confidence in the first-screenshot state classification
  is_complete: bool           true only when the assignment is visibly finished
  actions: list of Action
    action: "click" | "type" | "press_key" | "wait" | "done"
    target: string             answer | choice | dropdown | option | submit | advance | navigation | template | wait | done
    box_2d: [ymin, xmin, ymax, xmax] on 0–1000 (required for click)
    text: string              required for type; entered exactly as returned
    key: string               required for press_key
    seconds: number           optional for wait; defaults to 1 second
    reason: string            optional completion reason for done
    label: string             optional short sidebar label
```

Pydantic rejects unknown fields, malformed boxes, out-of-range coordinates, inverted boxes, clicks without boxes, types without text, key presses without keys, missing semantic targets, and unsafe edits for answered/graded/locked states. Boxes are normalized to integer coordinates in the inclusive 0–1000 range.

## Execution loop

1. Capture a lossless PNG with the copilot sidebar cropped out.
2. Ask Gemini for one structured plan.
3. Execute actions in order. Each independent input/control gets its own click box and action sequence.
4. Use explicit `press_key` actions for math-editor template navigation; ordinary text fields do not receive automatic right-arrow presses.
5. For dropdowns, click the closed control, then click the exact visible option or type the exact native-select label and press Enter.
6. After Submit/Check, remember the pre-action screenshot. If the next capture is unchanged, block answer edits until a new question or visible state transition appears.
7. Ignore invalid direct click targets safely and detect repeated input plans before clicking again.
8. Wait for transitions, recapture, and continue until `is_complete`, `done`, abort, or the bounded safety limit.

The legacy `mode` parameters remain accepted by Python entry points for compatibility, but they normalize to `auto` and do not influence prompting or execution.
