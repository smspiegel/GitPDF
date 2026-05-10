"""FastAPI server that drives the local UI.

Single-process, single-user. Uploaded PDFs are kept in a temp dir keyed
by the session and served back to the frontend so PDF.js can render them.
"""
from __future__ import annotations

import asyncio
import atexit
import shutil
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Lock

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from .diff_engine import compute_diff
from .extract import extract_document
from .models import DiffResult, ExtractedDoc, Mode
from .paths import sessions_dir, web_dir

# Window-close detection: the page holds a WebSocket open. We shut down
# when no sockets are connected. Polling timers (setInterval) get throttled
# in backgrounded Chrome tabs -- often to once per minute -- which used to
# kill the server while the user was on another tab. WebSockets are not
# subject to that throttling and the OS tears the socket down immediately
# when the tab actually closes.
#
# STARTUP_GRACE: time to allow first connection after launch.
# DISCONNECT_GRACE: small buffer after last disconnect, so a tab refresh
# (which momentarily drops to zero connections) doesn't shut us down.
STARTUP_GRACE = 60.0
DISCONNECT_GRACE = 5.0
WATCHDOG_INTERVAL = 2.0


def _sweep_stale_sessions() -> None:
    # Single-user, single-process app: any session-* dir we find at startup
    # is a leftover from a previous run that crashed, was force-killed, or
    # missed the shutdown hook. Safe to wipe.
    root = sessions_dir()
    for child in root.iterdir():
        if child.is_dir() and child.name.startswith("session-"):
            shutil.rmtree(child, ignore_errors=True)


class _SessionState:
    def __init__(self) -> None:
        _sweep_stale_sessions()
        self.workdir = Path(
            tempfile.mkdtemp(prefix="session-", dir=str(sessions_dir()))
        )
        self.path_a: Path | None = None
        self.path_b: Path | None = None
        self.extracted_a: ExtractedDoc | None = None
        self.extracted_b: ExtractedDoc | None = None
        self.lock = Lock()
        # atexit fires on normal interpreter exit even when FastAPI's
        # shutdown hook is bypassed (daemon-thread cut-off, KeyboardInterrupt
        # racing with the watchdog, the 5s join timeout in cli._run_gui).
        atexit.register(self.cleanup)

    def cleanup(self) -> None:
        shutil.rmtree(self.workdir, ignore_errors=True)


def create_app(shutdown_event: threading.Event | None = None) -> FastAPI:
    state = _SessionState()
    startup_time = time.monotonic()
    conn_state: dict[str, object] = {
        "count": 0,           # currently open keepalive sockets
        "ever_connected": False,
        "last_zero_at": None,  # monotonic time count last dropped to 0
    }

    def _signal_shutdown() -> None:
        if shutdown_event is not None:
            shutdown_event.set()

    async def _watchdog() -> None:
        while True:
            await asyncio.sleep(WATCHDOG_INTERVAL)
            now = time.monotonic()
            if not conn_state["ever_connected"]:
                # Nobody has connected yet. Exit if the user never
                # opened the page within the grace window.
                if now - startup_time > STARTUP_GRACE:
                    _signal_shutdown()
                    return
            elif conn_state["count"] == 0:
                last_zero = conn_state["last_zero_at"]
                if last_zero is not None and now - last_zero > DISCONNECT_GRACE:
                    # All tabs gone for long enough that a refresh
                    # would have reconnected by now.
                    _signal_shutdown()
                    return

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # Startup: kick off the connection-count watchdog.
        watchdog_task = asyncio.create_task(_watchdog())
        try:
            yield
        finally:
            # Shutdown: cancel watchdog (no-op if already returned) and
            # wipe the per-session temp dir. atexit also covers this for
            # hard exits, but the lifespan handler runs on clean exits.
            watchdog_task.cancel()
            try:
                await watchdog_task
            except (asyncio.CancelledError, Exception):
                pass
            state.cleanup()

    app = FastAPI(title="gitpdf", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.session = state

    @app.websocket("/ws/keepalive")
    async def keepalive(ws: WebSocket) -> None:
        await ws.accept()
        conn_state["count"] = int(conn_state["count"]) + 1  # type: ignore[arg-type]
        conn_state["ever_connected"] = True
        conn_state["last_zero_at"] = None
        try:
            # Hold the socket open. We don't expect any messages -- if the
            # tab is alive, the TCP connection is alive. We only receive
            # so we notice client-initiated close immediately.
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            conn_state["count"] = int(conn_state["count"]) - 1  # type: ignore[arg-type]
            if conn_state["count"] == 0:
                conn_state["last_zero_at"] = time.monotonic()

    @app.post("/api/shutdown")
    def shutdown_now() -> dict[str, bool]:
        _signal_shutdown()
        return {"ok": True}

    @app.post("/api/upload")
    async def upload(
        side: str = Form(...),
        file: UploadFile = File(...),
    ) -> JSONResponse:
        if side not in ("A", "B"):
            raise HTTPException(400, "side must be 'A' or 'B'")
        if not (file.filename or "").lower().endswith(".pdf"):
            raise HTTPException(400, "file must be a .pdf")
        dest = state.workdir / f"{side}.pdf"
        with dest.open("wb") as out:
            shutil.copyfileobj(file.file, out)
        with state.lock:
            if side == "A":
                state.path_a = dest
                state.extracted_a = None
            else:
                state.path_b = dest
                state.extracted_b = None
        return JSONResponse({"ok": True, "side": side})

    @app.get("/api/pdf/{side}")
    def get_pdf(side: str) -> FileResponse:
        if side not in ("A", "B"):
            raise HTTPException(400, "bad side")
        path = state.path_a if side == "A" else state.path_b
        if path is None or not path.exists():
            raise HTTPException(404, "no pdf uploaded for this side")
        return FileResponse(path, media_type="application/pdf")

    @app.post("/api/diff")
    async def diff(mode: Mode = Form("auto")) -> DiffResult:
        if state.path_a is None or state.path_b is None:
            raise HTTPException(400, "upload both PDFs first")

        async def ensure_extracted() -> tuple[ExtractedDoc, ExtractedDoc]:
            with state.lock:
                a = state.extracted_a
                b = state.extracted_b
                pa, pb = state.path_a, state.path_b
            if a is None and pa is not None:
                a = await run_in_threadpool(extract_document, pa)
            if b is None and pb is not None:
                b = await run_in_threadpool(extract_document, pb)
            with state.lock:
                state.extracted_a = a
                state.extracted_b = b
            assert a is not None and b is not None
            return a, b

        ea, eb = await ensure_extracted()
        result = await run_in_threadpool(
            compute_diff,
            ea.tokens, eb.tokens, ea.page_count, eb.page_count, mode,
        )
        return result

    @app.get("/api/page-sizes")
    def page_sizes() -> JSONResponse:
        a = state.extracted_a
        b = state.extracted_b
        return JSONResponse(
            {
                "A": a.page_sizes if a else [],
                "B": b.page_sizes if b else [],
            }
        )

    # Static frontend last so /api routes win.
    app.mount("/", StaticFiles(directory=web_dir(), html=True), name="web")
    return app
