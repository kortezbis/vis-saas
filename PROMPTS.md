# Viszmo — universal educational-agent prompting

The active path is `agent_engine.py`: pinned CDP page screenshot → one structured-JSON call → CDP input actions → repeat until the assignment is complete. The assignment browser remains the target even when the user works in another browser. The in-browser Viszmo panel exposes Math and General subject profiles plus Copilot and Autopilot run modes. Copilot now performs batch answer review: it evaluates each visible question, records the exact answer in the chat, and uses only safe non-submit navigation to continue; it does not enter or submit answers. Autopilot executes the plan continuously. General keeps the low-cost fast route; Math uses a stronger reasoning model with low thinking by default, while risky or uncertain typed answers receive a verification turn and plain unformatted fields can take the fast path.

## System prompt

The active prompts are mode-specific and cover:

- quantitative work across algebra, calculus, statistics, physics, chemistry, and other STEM;
- reading comprehension, literature, history, government, civics, and contextual questions;
- vocabulary, matching, true/false, dropdowns, radio buttons, checkboxes, short answers, and mixed assignments;
- state inspection, filled/graded/locked-field protection, submission, advancement, and completion detection;
- per-field action sequencing and platform-specific math/text formatting.

Every plan must classify `question_state` and `state_confidence` before proposing actions. The executor rejects answer edits for answered, graded, or locked states and turns low-confidence/unknown states into a short re-check wait.

The prompt requires internal reasoning only. The model returns no markdown, derivation, or conversational explanation.
- General mode prioritizes reading the complete stem and choices, evidence matching, and fast same-question navigation when Submit or Next is visible.

## User turn

Each lossless PNG page capture is accompanied by:

```text
Analyze the current workspace screenshot and produce the next executable plan.
Use the selected subject mode and classify the UI state from the screenshot itself.
Skip filled, graded, locked, or checkmarked fields. If the active question is solved, advance immediately.
Image size: {api_w} x {api_h} (native PNG from the assignment page viewport).
Prior execution guard: {guard}
Goal: {goal}
```

## JSON schema

Gemini uses its structured response schema; OpenAI uses the Responses API's
JSON-schema output format. Both providers are parsed into the same
`ExecutionPlan` model before any browser action runs.

```text
ExecutionPlan
  plan_summary: string       short operational label
  answer_text: string|null    concise exact answer for the current visible question; null when no answer is available
  question_state: string      unsolved | answered | graded_correct | graded_incorrect | locked | loading | complete | blocked | unknown
  state_confidence: number    0–1 confidence in the first-screenshot state classification
  requires_verification: bool  Math only: true for risky/uncertain typed entries; false only for plain fields or safe selections that can submit in the same plan
  is_complete: bool           true only when the assignment is visibly finished
  actions: list of Action
    action: "click" | "type" | "press_key" | "scroll" | "wait" | "done"
    target: string             answer | choice | dropdown | option | cancel | submit | advance | navigation | template | wait | done
    box_2d: [ymin, xmin, ymax, xmax] on 0–1000 (required for click)
    text: string              required for type; entered exactly as returned
    key: string               required for press_key
    direction: string         up or down for scroll
    clicks: number            1–10 scroll clicks
    seconds: number           optional for wait; defaults to 1 second
    reason: string            optional completion reason for done
    label: string             optional short sidebar label
```

Pydantic rejects unknown fields, malformed boxes, out-of-range coordinates, inverted boxes, clicks without boxes, types without text, key presses without keys, missing semantic targets, and unsafe edits for answered/graded/locked states. Boxes are normalized to integer coordinates in the inclusive 0–1000 range.

## Execution loop

1. Capture a lossless PNG from the pinned CDP page with the in-browser Viszmo panel hidden and excluded.
2. Ask the configured vision provider for one structured plan.
3. Execute actions in order. Each independent input/control gets its own click box and action sequence.
   In Copilot, record `answer_text` in the chat and execute only safe non-submit navigation.
4. Use explicit `press_key` actions for math-editor template navigation; ordinary text fields do not receive automatic right-arrow presses. Math answer fields are replaced before a new answer is typed, and CDP modifiers are encoded explicitly for math punctuation and hotkeys such as Ctrl+A.
5. Use a scroll action by itself when content is not visible; never mix scrolling with a fill or click.
6. For dropdowns, click the closed control, then click the exact visible option or type the exact native-select label and press Enter.
7. After Submit/Check, remember the pre-action screenshot. If the next capture is unchanged, block answer edits until a new question or visible state transition appears.
8. Ignore invalid direct click targets safely and detect repeated input plans before clicking again.
9. Wait for transitions, recapture, and continue until `is_complete`, `done`, abort, or the bounded safety limit.
10. Every successful provider response is recorded from its usage metadata.

## Provider configuration

Set `LLM_PROVIDER=gemini` or `LLM_PROVIDER=openai` in the local, gitignored
`.env` file. OpenAI uses `OPENAI_MODEL`, `OPENAI_MATH_MODEL`, and
`OPENAI_GENERAL_MODEL`; the default is `gpt-5-mini` for Math and the lower-cost
`gpt-5-nano` route for General. Math falls back only to nano during a temporary
service failure, so it never silently escalates to an expensive model. The
checked-in `.env.example` contains placeholders only. Never commit or reuse an
API key that has been pasted into chat or logs.

The legacy `agent.py` path remains available for compatibility. The desktop path
launches its own isolated Chrome/Edge profile and uses a private CDP target; it
does not use OS-level mouse or keyboard injection and never requires the user
to start a browser with a debugging flag.
