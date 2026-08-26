# PyInstaller spec for the Python sidecar used by the Electron app.

from pathlib import Path

from PyInstaller.building.build_main import Analysis, COLLECT, EXE, PYZ


ROOT = Path(SPECPATH).resolve().parent
DATA_FILES = [
    (ROOT / "index.html", "."),
    (ROOT / "dashboard.html", "."),
    (ROOT / "peek.html", "."),
    (ROOT / "widget.html", "."),
]
for asset in (ROOT / "assets").rglob("*"):
    if asset.is_file():
        DATA_FILES.append((asset, Path("assets") / asset.relative_to(ROOT / "assets")))

a = Analysis(
    [str(ROOT / "backend_server.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[(str(source), str(destination)) for source, destination in DATA_FILES],
    hiddenimports=[
        "websockets.sync.client",
        "websocket",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pywebview", "pyautogui"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="viszmo-backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="backend",
)
