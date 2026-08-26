#!/usr/bin/env python3
"""Start the Viszmo server and open the 380px right-edge sidebar."""

from __future__ import annotations

import os
import argparse
import socket
import threading
import time
import urllib.request

import uvicorn

import agent_engine
import chrome_session
import layout
from server import DEFAULT_HOST, DEFAULT_PORT, DEFAULT_SIDEBAR_WIDTH, app

agent_engine.load_env()
agent_engine.setup_logging()


def _choose_server_port(preferred: int) -> int:
    """Use 8000 when free; otherwise choose an unused loopback port.

    The launcher must never terminate an unrelated process that happens to
    use the preferred development port.
    """
    for candidate in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((DEFAULT_HOST, candidate))
            except OSError:
                continue
            return int(sock.getsockname()[1])
    raise RuntimeError("Could not find a local port for the Viszmo desktop server.")


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


def _maybe_launch_assignment_browser(
    *,
    url: str | None = None,
    browser: str = "auto",
    disabled: bool = False,
) -> None:
    if disabled:
        return
    enabled = os.getenv("VISZMO_AUTO_LAUNCH_BROWSER", "0").strip().lower()
    if enabled not in {"1", "true", "yes", "on"}:
        return
    assignment_url = (
        url
        or os.getenv("VISZMO_START_URL", "")
        or os.getenv("VISZMO_ASSIGNMENT_URL", "")
        or chrome_session.DEFAULT_START_URL
    ).strip() or chrome_session.DEFAULT_START_URL
    selected_browser = (
        browser if browser and browser != "auto" else os.getenv("VISZMO_BROWSER", "auto")
    ).strip().lower() or "auto"
    try:
        chrome_session.launch_viszmo_chrome(assignment_url, browser=selected_browser)
    except Exception as exc:
        raise SystemExit(f"Could not start the managed assignment browser: {exc}") from exc


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


def _open_sidebar(host: str, port: int, start_path: str = "/") -> None:
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
        url=f"http://{host}:{port}{start_path}",
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
        if controller._peek is not None:
            controller._peek.hide()

    webview.start(after_start)


def main(argv: list[str] | None = None, *, dashboard: bool = False) -> int:
    parser = argparse.ArgumentParser(description="Start the Viszmo desktop app")
    parser.add_argument(
        "--url",
        default=None,
        help="Optional browser start URL (default: Google)",
    )
    parser.add_argument(
        "--browser",
        choices=("auto", "chrome", "edge", "brave"),
        default="auto",
        help="Chromium browser Viszmo should own (default: auto)",
    )
    parser.add_argument(
        "--task",
        default=None,
        help="Optional task note to prefill in the sidebar",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not launch the managed browser; useful only for local diagnostics",
    )
    parser.add_argument(
        "--dashboard",
        action="store_true",
        help="Open the basic launch dashboard before the workspace",
    )
    args = parser.parse_args(argv)
    dashboard_mode = dashboard or args.dashboard
    if args.task is not None:
        os.environ["VISZMO_INITIAL_TASK"] = args.task
    if args.url is not None:
        os.environ["VISZMO_START_URL"] = args.url
    if args.browser != "auto":
        os.environ["VISZMO_BROWSER"] = args.browser

    server_port = _choose_server_port(DEFAULT_PORT)
    config = uvicorn.Config(app, host=DEFAULT_HOST, port=server_port, log_level="info")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    _wait_for_server(f"http://{DEFAULT_HOST}:{server_port}/health")
    if not getattr(server, "started", False):
        raise SystemExit(
            f"Could not start Viszmo on {DEFAULT_HOST}:{DEFAULT_PORT}. "
            "Close the other instance and try again."
        )
    if not dashboard_mode:
        _maybe_launch_assignment_browser(
            url=args.url,
            browser=args.browser,
            disabled=args.no_browser,
        )
    _open_sidebar(
        DEFAULT_HOST,
        server_port,
        start_path="/dashboard" if dashboard_mode else "/",
    )
    server.should_exit = True
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
