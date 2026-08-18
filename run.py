#!/usr/bin/env python3
"""Start the Viszmo server and open the 380px right-edge sidebar."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import urllib.request

import uvicorn

import agent_engine
import layout
import overlay
from server import DEFAULT_HOST, DEFAULT_PORT, DEFAULT_SIDEBAR_WIDTH, app

agent_engine.load_env()
agent_engine.setup_logging()


def _pids_listening_on(port: int) -> list[int]:
    if sys.platform != "win32":
        return []
    try:
        output = subprocess.check_output(["netstat", "-ano"], text=True, errors="ignore")
    except Exception:
        return []
    pids: list[int] = []
    marker = f"127.0.0.1:{port}"
    for line in output.splitlines():
        if marker not in line or "LISTENING" not in line:
            continue
        parts = line.split()
        try:
            pid = int(parts[-1])
        except (IndexError, ValueError):
            continue
        if pid not in pids and pid != os.getpid():
            pids.append(pid)
    return pids


def _free_port(port: int) -> None:
    """Stop a leftover Viszmo server so this launch can bind 8000."""
    for pid in _pids_listening_on(port):
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/F"],
                check=False,
                capture_output=True,
                text=True,
            )
        except Exception:
            pass
    deadline = time.time() + 3.0
    while time.time() < deadline and _pids_listening_on(port):
        time.sleep(0.15)


def _wait_for_server(url: str, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(0.15)
    raise RuntimeError(f"Server did not start at {url}")


class SidebarController:
    """JS API: only hide/show. Window objects stay private so pywebview
    does not recurse into WinForms AccessibilityObject.Bounds.Empty."""

    def __init__(self) -> None:
        self._main = None
        self._peek = None

    def hide(self) -> None:
        if self._main is not None:
            self._main.hide()
        if self._peek is not None:
            self._peek.show()

    def show(self) -> None:
        if self._peek is not None:
            self._peek.hide()
        if self._main is not None:
            self._main.show()


def _open_sidebar(host: str, port: int) -> None:
    try:
        import webview
    except ImportError as exc:
        raise SystemExit(
            "pywebview is required. Install with:\n  pip install -r requirements.txt"
        ) from exc

    area = layout.work_area()
    width = DEFAULT_SIDEBAR_WIDTH
    height = min(700, max(area.height - 48, 540))
    peek_size = 44
    controller = SidebarController()
    controller._main = webview.create_window(
        title="Viszmo",
        url=f"http://{host}:{port}/",
        js_api=controller,
        width=width,
        height=height,
        x=area.left + area.width - width - 12,
        y=area.top + 24,
        frameless=True,
        on_top=True,
        easy_drag=True,
        resizable=True,
        background_color="#131314",
    )
    controller._peek = webview.create_window(
        title="Viszmo",
        url=f"http://{host}:{port}/peek",
        js_api=controller,
        width=peek_size,
        height=peek_size,
        x=area.left + area.width - peek_size - 8,
        y=area.top + 8,
        frameless=True,
        on_top=True,
        easy_drag=True,
        resizable=False,
        hidden=True,
        background_color="#131314",
    )

    def after_start() -> None:
        overlay.ensure_started()
        if controller._peek is not None:
            controller._peek.hide()

    webview.start(after_start)


def main() -> int:
    _free_port(DEFAULT_PORT)
    config = uvicorn.Config(app, host=DEFAULT_HOST, port=DEFAULT_PORT, log_level="info")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    _wait_for_server(f"http://{DEFAULT_HOST}:{DEFAULT_PORT}/health")
    if not getattr(server, "started", False):
        raise SystemExit(
            f"Could not start Viszmo on {DEFAULT_HOST}:{DEFAULT_PORT}. "
            "Close the other instance and try again."
        )
    _open_sidebar(DEFAULT_HOST, DEFAULT_PORT)
    server.should_exit = True
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
