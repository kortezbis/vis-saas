#!/usr/bin/env python3
"""FastAPI WebSocket server for the one-shot Viszmo copilot."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

import agent_engine
import layout

ROOT = Path(__file__).resolve().parent
INDEX_FILE = ROOT / "index.html"
PEEK_FILE = ROOT / "peek.html"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
DEFAULT_SIDEBAR_WIDTH = 380

agent_engine.load_env()
agent_engine.setup_logging()
log = logging.getLogger("server")

app = FastAPI(title="Viszmo")
app.mount("/assets", StaticFiles(directory=ROOT / "assets"), name="assets")


def _sidebar_width(payload: dict[str, Any] | None = None) -> int:
    if payload:
        raw = payload.get("sidebar_width")
        if raw is not None:
            try:
                return max(0, min(int(raw), 800))
            except (TypeError, ValueError):
                pass
    return int(os.getenv("SIDEBAR_WIDTH", DEFAULT_SIDEBAR_WIDTH))


@app.get("/")
async def index() -> Response:
    if not INDEX_FILE.exists():
        return JSONResponse({"error": "index.html is missing"}, status_code=500)
    return FileResponse(INDEX_FILE)


@app.get("/peek")
async def peek() -> Response:
    if not PEEK_FILE.exists():
        return JSONResponse({"error": "peek.html is missing"}, status_code=500)
    return FileResponse(PEEK_FILE)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": True}


@app.get("/tab")
async def tab() -> dict[str, Any]:
    return layout.connected_tab()


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    loop = asyncio.get_running_loop()
    abort_event = threading.Event()
    worker: asyncio.Task[None] | None = None

    async def send(event: dict[str, Any]) -> None:
        await websocket.send_json(event)

    def emit_from_thread(event: dict[str, Any]) -> None:
        future = asyncio.run_coroutine_threadsafe(send(event), loop)
        try:
            future.result(timeout=10)
        except Exception:
            abort_event.set()

    async def cancel_worker() -> bool:
        nonlocal worker
        abort_event.set()
        if worker is not None and not worker.done():
            try:
                await asyncio.wait_for(asyncio.shield(worker), timeout=30)
            except asyncio.TimeoutError:
                log.warning("Previous task is still stopping.")
                return False
        worker = None
        return True

    async def start_task(goal: str, sidebar_width: int) -> None:
        nonlocal worker
        if not await cancel_worker():
            await send({"type": "error", "text": "Still stopping the previous task. Try again in a moment."})
            return
        abort_event.clear()

        def run() -> None:
            agent_engine.run_oneshot(
                goal=goal,
                sidebar_width=sidebar_width,
                should_abort=abort_event.is_set,
                on_event=emit_from_thread,
            )

        worker = asyncio.create_task(asyncio.to_thread(run))

    try:
        await send({"type": "status", "text": "Ready. Open the page on the left, then press Launch."})
        while True:
            data = await websocket.receive_json()
            msg_type = str(data.get("type") or "").lower()

            if msg_type == "abort":
                abort_event.set()
                if worker is None or worker.done():
                    await send({"type": "aborted", "text": "Stopped by user."})
                continue

            if msg_type in {"launch", "auto", "start", "task"}:
                notes = str(data.get("notes") or data.get("task") or data.get("goal") or "").strip()
                goal = notes or agent_engine.goal_for()
                await send({"type": "status", "text": "Evaluating workspace..."})
                await start_task(goal, _sidebar_width(data))
    except WebSocketDisconnect:
        abort_event.set()
        log.info("UI disconnected; aborting any running task.")
    except Exception as exc:
        abort_event.set()
        log.exception("WebSocket error")
        try:
            await send({"type": "error", "text": str(exc)})
        except Exception:
            pass
    finally:
        abort_event.set()
