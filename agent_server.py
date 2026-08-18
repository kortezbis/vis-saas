#!/usr/bin/env python3
"""Back-compat launcher. Prefer: python run.py"""

from __future__ import annotations

import argparse

from run import main


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Viszmo desktop copilot")
    parser.add_argument("--desktop", action="store_true")
    parser.add_argument("--attach", action="store_true")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.parse_args()
    raise SystemExit(main())
