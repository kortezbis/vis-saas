"""Build the Python agent sidecar consumed by Electron Builder."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "electron" / "backend.spec"
WORK_PATH = ROOT / "build-release" / "backend"
DIST_PATH = ROOT / "dist-release"


def main() -> int:
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--clean",
        "--noconfirm",
        "--workpath",
        str(WORK_PATH),
        "--distpath",
        str(DIST_PATH),
        str(SPEC),
    ]
    subprocess.run(command, cwd=ROOT, check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
