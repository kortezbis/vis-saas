"""Run Viszmo's local agent service without opening a Python UI window."""

from __future__ import annotations

import argparse
import multiprocessing
import os

import uvicorn

from server import app, DEFAULT_HOST, DEFAULT_PORT


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Viszmo local agent service")
    parser.add_argument("--host", default=os.getenv("VISZMO_HOST", DEFAULT_HOST))
    parser.add_argument("--port", type=int, default=int(os.getenv("VISZMO_PORT", DEFAULT_PORT)))
    args = parser.parse_args(argv)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
