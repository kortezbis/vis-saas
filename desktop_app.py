"""Small development wrapper for the Electron desktop app.

Use ``npm install`` once, then ``python desktop_app.py`` or ``npm start``.
The Electron shell opens the launch dashboard; its controls start a dedicated
Google Chrome profile and keep the Viszmo panel inside that browser page.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    npm = "npm.cmd" if os.name == "nt" else "npm"
    command = [npm, "start", "--", *sys.argv[1:]]
    return subprocess.call(command, cwd=Path(__file__).resolve().parent)


if __name__ == "__main__":
    raise SystemExit(main())
