"""Lightweight cross-task progress notes for assignments.

The vision loop re-reads the page every turn, so this store never gates
actions — it only gives a resumed run context about what previous runs
already handled, and gives completion a place to close the book. Everything
is local JSON, keyed by assignment URL with query/session noise stripped.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

log = logging.getLogger("task_memory")

_LOCK = threading.Lock()

MAX_ANSWERS_PER_ASSIGNMENT = 80


def _store_path() -> Path:
    base = os.getenv("VISZMO_DATA_DIR") or (
        Path(os.getenv("LOCALAPPDATA", str(Path.home()))) / "Viszmo"
    )
    path = Path(base) / "progress.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def url_key(url: str) -> str:
    """Normalize an assignment URL to its stable identity."""
    try:
        parts = urlsplit(str(url or "").strip())
    except Exception:
        return ""
    if not parts.netloc:
        return ""
    path = parts.path.rstrip("/")
    return f"{parts.scheme}://{parts.netloc}{path}"


def _read_all() -> dict:
    try:
        data = json.loads(_store_path().read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _write_all(data: dict) -> None:
    try:
        _store_path().write_text(
            json.dumps(data, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
    except Exception as exc:
        log.debug("Could not write progress store: %s", exc)


def load(url: str) -> dict:
    """Return the stored entry for an assignment URL ({} when unknown)."""
    key = url_key(url)
    if not key:
        return {}
    with _LOCK:
        entry = _read_all().get(key)
    return entry if isinstance(entry, dict) else {}


def prior_answer_count(url: str) -> int:
    entry = load(url)
    answers = entry.get("answers")
    return len(answers) if isinstance(answers, list) else 0


def record_answer(url: str, question_label: str, answer_text: str) -> None:
    """Append one answered item; bounded so long assignments stay small."""
    key = url_key(url)
    if not key:
        return
    label = " ".join(str(question_label or "").split())[:120]
    answer = " ".join(str(answer_text or "").split())[:200]
    with _LOCK:
        data = _read_all()
        entry = data.setdefault(key, {})
        answers = entry.get("answers")
        if not isinstance(answers, list):
            answers = []
        answers.append({"q": label, "a": answer, "ts": int(time.time())})
        entry["answers"] = answers[-MAX_ANSWERS_PER_ASSIGNMENT:]
        entry["updated"] = int(time.time())
        data[key] = entry
        _write_all(data)


def mark_complete(url: str) -> None:
    """Close the book on a finished assignment."""
    key = url_key(url)
    if not key:
        return
    with _LOCK:
        data = _read_all()
        if key in data:
            data[key]["completed"] = True
            data[key]["completed_at"] = int(time.time())
            data[key].pop("answers", None)
            _write_all(data)
