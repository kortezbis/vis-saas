"""Compatibility entry point for the browser-scoped Viszmo agent.

The previous implementation controlled the desktop with OS-level input. This
entry point now uses the same managed assignment browser as the desktop app so
running it cannot move the user's mouse or type into another application.
"""

from __future__ import annotations

import argparse

import agent_engine
import chrome_session


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Viszmo in its managed assignment browser")
    parser.add_argument("task", nargs="+", help="Natural-language task for the agent")
    parser.add_argument("--url", default="about:blank", help="Assignment URL")
    parser.add_argument("--browser", choices=("auto", "chrome", "edge", "brave"), default="auto")
    parser.add_argument("--mode", choices=("math", "general"), default="math")
    return parser.parse_args()


def main() -> int:
    agent_engine.load_env()
    agent_engine.setup_logging()
    args = parse_args()
    task = " ".join(args.task).strip()
    if not task:
        return 1
    try:
        chrome_session.launch_viszmo_chrome(args.url, browser=args.browser)
        result = agent_engine.run_oneshot(task, mode=args.mode)
    except Exception as exc:
        print(f"Viszmo could not start: {exc}")
        return 1
    return 0 if result == "done" else 1


if __name__ == "__main__":
    raise SystemExit(main())
