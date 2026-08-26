#!/usr/bin/env python3
"""FastAPI WebSocket server for the one-shot Viszmo copilot."""

from __future__ import annotations

import asyncio
import logging
import multiprocessing as mp
import os
import sys
from queue import Empty
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

import agent_engine
import chrome_session

ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
INDEX_FILE = ROOT / "index.html"
DASHBOARD_FILE = ROOT / "dashboard.html"
PEEK_FILE = ROOT / "peek.html"
WIDGET_FILE = ROOT / "widget.html"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
DEFAULT_SIDEBAR_WIDTH = 380

agent_engine.load_env()
agent_engine.setup_logging()
log = logging.getLogger("server")


def _agent_worker_entry(
    goal: str,
    sidebar_width: int,
    mode: str,
    autonomy: str,
    target_ws_url: str,
    usage_token: str,
    stop_event: Any,
    event_queue: Any,
) -> None:
    """Run one agent task in a killable child process.

    A thread cannot be force-stopped while a model/network call is blocked.
    Keeping the agent in its own process lets Stop terminate stale work before
    another task is launched.
    """

    def emit(event: dict[str, Any]) -> None:
        try:
            event_queue.put(event)
        except (BrokenPipeError, EOFError, OSError):
            pass

    try:
        import virtual_mouse

        virtual_mouse.configure_cdp_target(target_ws_url)
        status = agent_engine.run_oneshot(
            goal=goal,
            sidebar_width=sidebar_width,
            mode=mode,
            autonomy=autonomy,
            usage_token=usage_token,
            should_abort=stop_event.is_set,
            on_event=emit,
        )
        emit({"type": "_worker_finished", "status": status})
    except BaseException as exc:
        emit({"type": "error", "text": str(exc)})
        emit({"type": "_worker_finished", "status": "error"})
    finally:
        try:
            import virtual_mouse

            virtual_mouse.clear_cdp_target()
        except (ImportError, RuntimeError):
            pass
        try:
            event_queue.cancel_join_thread()
        except (AttributeError, OSError):
            pass

app = FastAPI(title="Viszmo")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["*"],
)
app.mount("/assets", StaticFiles(directory=ROOT / "assets"), name="assets")

BRIDGE_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Viszmo controller</title></head>
<body><script>
(() => {
  let socket = null;
  let reconnectTimer = null;

  function relay(payload) {
    window.parent.postMessage({ viszmoBridge: "from-bridge", ...payload }, "*");
  }

  function connect() {
    const protocol = location.protocol === "https:" ? "wss" : "ws";
    socket = new WebSocket(`${protocol}://${location.host}/ws`);
    socket.onopen = () => relay({ event: "open" });
    socket.onmessage = (event) => relay({ event: "message", message: event.data });
    socket.onerror = () => relay({ event: "error" });
    socket.onclose = () => {
      relay({ event: "close" });
      if (reconnectTimer) clearTimeout(reconnectTimer);
      reconnectTimer = setTimeout(connect, 1200);
    };
  }

  window.addEventListener("message", async (event) => {
    const data = event.data || {};
    if (data.viszmoBridge !== "to-bridge") return;
    if (data.kind === "socket" && socket && socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify(data.message || {}));
      return;
    }
    if (data.kind === "fetch" && (data.path === "/tab" || data.path === "/config")) {
      try {
        const response = await fetch(data.path, { cache: "no-store" });
        relay({ event: "response", requestId: data.requestId, payload: await response.json() });
      } catch (error) {
        relay({ event: "response", requestId: data.requestId, payload: null });
      }
    }
  });

  relay({ event: "ready" });
  connect();
})();
</script></body></html>"""


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


@app.get("/dashboard")
async def dashboard() -> Response:
    if not DASHBOARD_FILE.exists():
        return JSONResponse({"error": "dashboard.html is missing"}, status_code=500)
    return FileResponse(DASHBOARD_FILE)


@app.get("/bridge")
async def bridge() -> Response:
    return Response(content=BRIDGE_HTML, media_type="text/html")


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
    return chrome_session.connected_tab()


@app.get("/config")
async def config() -> dict[str, Any]:
    """Small runtime config surface for the desktop dashboard."""
    return {
        "start_url": os.getenv("VISZMO_START_URL", chrome_session.DEFAULT_START_URL),
        "browser": os.getenv("VISZMO_BROWSER", "auto"),
        "initial_task": os.getenv("VISZMO_INITIAL_TASK", ""),
    }


_UI_SETTING_KEYS = ("os_notifications", "completion_chime", "desktop_widget")
_UI_SETTINGS_DEFAULTS: dict[str, bool] = {
    "os_notifications": True,
    "completion_chime": True,
    "desktop_widget": True,
}


def _migrate_legacy_settings(data: dict[str, Any]) -> dict[str, Any]:
    """Old builds stored an in-tab pill toggle; carry it over."""
    if "desktop_widget" not in data and isinstance(data.get("status_pill"), bool):
        data["desktop_widget"] = data["status_pill"]
    return data


def _ui_settings_path() -> Path:
    base = os.getenv("VISZMO_DATA_DIR") or (
        Path(os.getenv("LOCALAPPDATA", str(Path.home()))) / "Viszmo"
    )
    path = Path(base) / "ui-settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _load_ui_settings() -> dict[str, bool]:
    settings = dict(_UI_SETTINGS_DEFAULTS)
    try:
        import json

        data = json.loads(_ui_settings_path().read_text(encoding="utf-8"))
        data = _migrate_legacy_settings(data if isinstance(data, dict) else {})
        for key in _UI_SETTING_KEYS:
            if isinstance(data.get(key), bool):
                settings[key] = data[key]
    except Exception:
        pass
    return settings


def _apply_ui_settings(settings: dict[str, bool]) -> None:
    import notifications

    notifications.set_runtime_enabled(bool(settings["os_notifications"]))


@app.get("/ui-settings")
async def get_ui_settings() -> dict[str, bool]:
    return _load_ui_settings()


@app.post("/ui-settings")
async def update_ui_settings(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Persist dashboard toggles and push panel-affecting ones live."""
    import json

    settings = _load_ui_settings()
    body = payload or {}
    for key in _UI_SETTING_KEYS:
        if isinstance(body.get(key), bool):
            settings[key] = body[key]
    try:
        _ui_settings_path().write_text(json.dumps(settings, indent=1), encoding="utf-8")
    except Exception as exc:
        log.warning("Could not save UI settings: %s", exc)
    _apply_ui_settings(settings)
    return settings


@app.post("/widget/visibility")
async def widget_visibility(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """The dashboard relays widget toggles here; the backend mirrors the
    setting while Electron owns the actual window."""
    return {"ok": True, "settings": _load_ui_settings()}


# Persisted preferences (e.g. toasts off) must apply even if the dashboard
# is never opened this session.
_apply_ui_settings(_load_ui_settings())


@app.get("/live-status")
async def live_status() -> dict[str, Any]:
    """Compact task snapshot for the desktop status widget."""
    return chrome_session.get_live_status()


@app.get("/widget")
async def widget_page() -> Response:
    if not WIDGET_FILE.exists():
        return JSONResponse({"error": "widget.html is missing"}, status_code=500)
    return FileResponse(WIDGET_FILE)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    mp_context = mp.get_context("spawn")
    worker: mp.Process | None = None
    worker_stop_event: Any | None = None
    worker_queue: Any | None = None
    worker_pump: asyncio.Task[None] | None = None
    controller_auth_token = ""

    async def send(event: dict[str, Any]) -> None:
        await websocket.send_json(event)

    async def pump_worker_events(process: mp.Process, event_queue: Any) -> None:
        finished = False
        while True:
            try:
                event = event_queue.get_nowait()
            except Empty:
                if finished and not process.is_alive():
                    return
                if not process.is_alive():
                    # Give the queue feeder a moment to flush the final event.
                    await asyncio.sleep(0.1)
                    try:
                        event = event_queue.get_nowait()
                    except Empty:
                        return
                else:
                    await asyncio.sleep(0.05)
                    continue

            if not isinstance(event, dict):
                continue
            if event.get("type") == "_worker_finished":
                finished = True
                continue
            try:
                import notifications

                notifications.notify_task_event(str(event.get("type") or ""), str(event.get("text") or ""))
            except Exception:
                pass
            try:
                await send(event)
            except Exception:
                return

    async def cancel_worker() -> bool:
        nonlocal worker, worker_stop_event, worker_queue, worker_pump
        process = worker
        if process is not None:
            if worker_stop_event is not None:
                worker_stop_event.set()
            deadline = asyncio.get_running_loop().time() + 2.0
            while process.is_alive() and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.05)
            if process.is_alive():
                log.warning("Force-terminating the Viszmo worker process %s.", process.pid)
                process.terminate()
                process.join(timeout=2)
            if process.is_alive() and hasattr(process, "kill"):
                log.error("Worker process %s ignored terminate; killing it.", process.pid)
                process.kill()
                process.join(timeout=2)

            if process.is_alive():
                log.error("Worker process %s could not be stopped.", process.pid)
                return False

        if worker_pump is not None and not worker_pump.done():
            worker_pump.cancel()
            await asyncio.gather(worker_pump, return_exceptions=True)
        if worker_queue is not None:
            try:
                worker_queue.close()
                worker_queue.cancel_join_thread()
            except (AttributeError, OSError):
                pass
        if process is not None:
            process.close()
        worker = None
        worker_stop_event = None
        worker_queue = None
        worker_pump = None
        return True

    async def start_task(
        goal: str,
        sidebar_width: int,
        mode: str,
        autonomy: str,
        target_ws_url: str,
        usage_token: str,
    ) -> None:
        nonlocal worker, worker_stop_event, worker_queue, worker_pump
        if not await cancel_worker():
            await send({"type": "error", "text": "The previous Viszmo task could not be stopped. Restart Viszmo before launching again."})
            return
        worker_stop_event = mp_context.Event()
        worker_queue = mp_context.Queue()
        worker = mp_context.Process(
            target=_agent_worker_entry,
            args=(goal, sidebar_width, mode, autonomy, target_ws_url, usage_token, worker_stop_event, worker_queue),
            name="viszmo-agent-worker",
        )
        worker.start()
        worker_pump = asyncio.create_task(pump_worker_events(worker, worker_queue))

    try:
        await send({
            "type": "status",
            "text": "Ready. Viszmo controls only its assignment browser; your mouse and other apps stay independent.",
        })
        while True:
            data = await websocket.receive_json()
            msg_type = str(data.get("type") or "").lower()

            if msg_type == "auth":
                controller_auth_token = str(data.get("access_token") or "").strip()
                chrome_session.set_desktop_access_token(controller_auth_token)
                continue

            if msg_type == "toggle_lock":
                # Kept as a compatibility response for older sidebars. The
                # desktop workflow intentionally has no global or page lock.
                await send({
                    "type": "status",
                    "text": "Input locking is disabled. Viszmo only sends events to its assigned browser tab.",
                })
                continue

            if msg_type == "attach_browser":
                try:
                    target = chrome_session.attach_electron_target(
                        str(data.get("target_id") or ""),
                        int(data.get("cdp_port") or 0),
                    )
                    chrome_session.install_browser_panel()
                    await send({
                        "type": "status",
                        "text": f"Connected to the assignment browser: {target.get('title') or 'Google'}.",
                    })
                    await send({
                        "type": "browser_ready",
                        "text": "Chrome ready. The Viszmo panel is inside the browser page.",
                    })
                except Exception as exc:
                    log.exception("Electron browser attach failed")
                    await send({"type": "error", "text": str(exc)})
                continue

            if msg_type in {"close_browser", "close_chrome", "stop_browser"}:
                if not await cancel_worker():
                    await send({"type": "error", "text": "The current task is still stopping. Try closing Chrome again in a moment."})
                    continue
                try:
                    message = await asyncio.to_thread(chrome_session.close_pinned_browser)
                    await send({"type": "browser_closed", "text": message})
                except Exception as exc:
                    log.exception("Managed browser close failed")
                    await send({"type": "error", "text": str(exc)})
                continue

            if msg_type in {"launch_browser", "open_browser"}:
                selected_browser = str(data.get("browser") or os.getenv("VISZMO_BROWSER", "auto")).strip().lower() or "auto"
                start_url = str(
                    data.get("url")
                    or os.getenv("VISZMO_START_URL", "")
                    or chrome_session.DEFAULT_START_URL
                ).strip() or chrome_session.DEFAULT_START_URL
                await send({"type": "status", "text": "Opening the managed browser on Google…"})
                try:
                    await asyncio.to_thread(
                        chrome_session.launch_viszmo_chrome,
                        start_url,
                        selected_browser,
                    )
                    await asyncio.to_thread(chrome_session.install_browser_panel)
                    await send({
                        "type": "browser_ready",
                        "text": "Chrome ready. The Viszmo panel is inside the browser page.",
                    })
                except Exception as exc:
                    log.exception("Managed browser launch failed")
                    await send({"type": "error", "text": str(exc)})
                continue

            if msg_type == "abort":
                if await cancel_worker():
                    await send({"type": "stopped", "text": "Stopped by user."})
                else:
                    await send({"type": "error", "text": "The Viszmo task could not be stopped. Restart Viszmo before launching again."})
                continue

            if msg_type in {"launch", "auto", "start", "task"}:
                if not controller_auth_token:
                    await send({"type": "error", "text": "Sign in to Viszmo before answering questions."})
                    continue
                mode = agent_engine.normalize_mode(data.get("mode"))
                notes = str(data.get("notes") or data.get("task") or data.get("goal") or "").strip()
                goal = notes or agent_engine.goal_for(mode)
                target_id = str(data.get("target_id") or "").strip()
                if target_id:
                    try:
                        chrome_session.select_target(target_id)
                        await asyncio.to_thread(chrome_session.install_browser_panel)
                    except Exception as exc:
                        await send({"type": "error", "text": str(exc)})
                        continue
                label = "Math" if mode == "math" else "General"
                autonomy = agent_engine.normalize_autonomy(data.get("autonomy"))
                await send({"type": "status", "text": f"Evaluating workspace ({label})..."})
                target = chrome_session.assigned_target()
                target_ws_url = str((target or {}).get("webSocketDebuggerUrl") or "")
                if not target_ws_url:
                    await send({
                        "type": "error",
                        "text": "No managed assignment browser is connected. Launch the assignment browser first.",
                    })
                    continue
                await start_task(goal, _sidebar_width(data), mode, autonomy, target_ws_url, controller_auth_token)
    except WebSocketDisconnect:
        log.info("UI disconnected; aborting any running task.")
    except Exception as exc:
        log.exception("WebSocket error")
        try:
            await send({"type": "error", "text": str(exc)})
        except Exception:
            pass
    finally:
        await cancel_worker()
