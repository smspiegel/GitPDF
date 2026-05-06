"""Filesystem layout for gitpdf.

The app is portable: when frozen with PyInstaller, every writable file lives
under the directory containing the executable (so the whole app folder can be
moved or deleted as one unit). In dev mode, the same paths anchor on the
project root.
"""
from __future__ import annotations

import sys
from pathlib import Path


def app_base_dir() -> Path:
    """Directory the app considers its own root (writable, portable)."""
    if getattr(sys, "frozen", False):
        # PyInstaller --onedir: sys.executable is dist/gitpdf/gitpdf.exe
        return Path(sys.executable).resolve().parent
    # Dev mode: repo root, two levels up from src/gitpdf/paths.py
    return Path(__file__).resolve().parents[2]


def data_dir() -> Path:
    d = app_base_dir() / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d


def sessions_dir() -> Path:
    d = data_dir() / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def exports_dir() -> Path:
    d = data_dir() / "exports"
    d.mkdir(parents=True, exist_ok=True)
    return d


def resource_path(rel: str) -> Path:
    """Locate a read-only bundled resource (e.g. the web/ folder).

    PyInstaller --onefile extracts to sys._MEIPASS at runtime; --onedir leaves
    bundled data next to the executable under _internal/. In dev, the resource
    sits next to the importing module.
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass) / rel
    return Path(__file__).resolve().parent / rel


def web_dir() -> Path:
    return resource_path("web")
