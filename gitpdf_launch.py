"""Top-level entry script for the PyInstaller-frozen build.

Lives outside the `gitpdf` package so PyInstaller can run it without the
relative-import errors that bite when `__main__.py` is invoked as a script.
"""
import os
import sys
from pathlib import Path

# In a windowed build there is no console attached, so sys.stdout / sys.stderr
# are None. Several deps (notably uvicorn's default log config) call
# `sys.stdout.isatty()` and crash with AttributeError. We also lose all visibility
# into errors. Route stdio to a log file under the app's data dir so problems
# are debuggable without losing the windowed UX.
def _setup_stdio() -> None:
    if sys.stdout is not None and sys.stderr is not None:
        return
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).resolve().parent
    else:
        base = Path(__file__).resolve().parent
    log_dir = base / "data"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "gitpdf.log"
        log_file = open(log_path, "a", encoding="utf-8", buffering=1)
    except OSError:
        log_file = open(os.devnull, "w", encoding="utf-8")
    if sys.stdout is None:
        sys.stdout = log_file
    if sys.stderr is None:
        sys.stderr = log_file
    if sys.stdin is None:
        sys.stdin = open(os.devnull, "r", encoding="utf-8")


_setup_stdio()

from gitpdf.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
