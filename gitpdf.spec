# PyInstaller spec for gitpdf (portable --onedir build).
#
# Build with:
#   .\scripts\build.ps1
# or directly:
#   .\.venv\Scripts\pyinstaller.exe gitpdf.spec --noconfirm
#
# Output: dist/gitpdf/  (drop this folder anywhere; double-click gitpdf.exe)

# ruff: noqa
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

block_cipher = None
ROOT = Path(SPECPATH)
SRC = ROOT / "src" / "gitpdf"

# Bundle the entire web/ tree (PDF.js + index.html + app.js + styles.css).
# Destination is "web" (without "gitpdf/" prefix) because paths.web_dir()
# resolves resources relative to the gitpdf package, which PyInstaller
# places at the bundle root for the entry script.
datas = [(str(SRC / "web"), "web")]

# pdfplumber/pdfminer ship data files (CIDFont maps); collect them.
datas += collect_data_files("pdfminer")
datas += collect_data_files("pdfplumber")
datas += collect_data_files("pypdfium2")

hidden = [
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
]

a = Analysis(
    [str(ROOT / "gitpdf_launch.py")],
    pathex=[str(SRC.parent)],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Big optional deps we don't bundle into the portable build.
        "paddleocr",
        "paddle",
        "paddlepaddle",
        "torch",
        "tensorflow",
        "matplotlib",
        "tkinter",
        "IPython",
        "notebook",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

_common_exe_kwargs = dict(
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

# Primary exe: windowed (no console pops up when the user double-clicks).
# Auto-opens the browser via webbrowser.open() in cli._run_gui().
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="gitpdf",
    console=False,
    **_common_exe_kwargs,
)

# Companion exe: same code, console attached. For developers / IT to see
# server logs and stack traces when something goes wrong.
exe_console = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="gitpdf-console",
    console=True,
    **_common_exe_kwargs,
)

coll = COLLECT(
    exe,
    exe_console,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="gitpdf",
)
