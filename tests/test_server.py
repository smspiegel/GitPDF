"""Server tests covering the keepalive lifecycle.

The server shuts down via a `threading.Event` passed into `create_app`.
We drive the watchdog through the WebSocket connection count instead of
the old polling heartbeat -- these tests pin that contract so a future
refactor can't silently regress the background-tab fix.
"""
from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from gitpdf import server as server_mod
from gitpdf.server import create_app


@pytest.fixture
def fast_grace(monkeypatch):
    """Shrink the production grace windows so tests run in seconds."""
    monkeypatch.setattr(server_mod, "STARTUP_GRACE", 0.3)
    monkeypatch.setattr(server_mod, "DISCONNECT_GRACE", 0.3)
    monkeypatch.setattr(server_mod, "WATCHDOG_INTERVAL", 0.1)


def _wait_for(predicate, timeout: float = 3.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def test_keepalive_route_registered():
    app = create_app(threading.Event())
    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/ws/keepalive" in paths
    # Old polling heartbeat must stay gone -- it broke under Chrome throttling.
    assert "/api/heartbeat" not in paths
    assert "/api/shutdown" in paths


def test_shutdown_endpoint_signals_event():
    ev = threading.Event()
    app = create_app(ev)
    with TestClient(app) as client:
        r = client.post("/api/shutdown")
        assert r.status_code == 200
    assert ev.is_set(), "/api/shutdown must set the shutdown event"


def test_no_shutdown_before_startup_grace(fast_grace):
    ev = threading.Event()
    app = create_app(ev)
    with TestClient(app) as client:
        # Connect a keepalive socket immediately so "ever_connected" flips.
        with client.websocket_connect("/ws/keepalive"):
            # While the socket is open, the watchdog must not fire even after
            # the startup grace expires -- a live connection keeps us alive.
            time.sleep(server_mod.STARTUP_GRACE + 0.3)
            assert not ev.is_set()


def test_shutdown_after_no_initial_connection(fast_grace):
    ev = threading.Event()
    app = create_app(ev)
    with TestClient(app):
        # Don't connect the WS at all. Watchdog should fire after STARTUP_GRACE.
        assert _wait_for(ev.is_set, timeout=2.0), (
            "watchdog must shut down when no client ever connects"
        )


def test_shutdown_after_last_socket_disconnects(fast_grace):
    ev = threading.Event()
    app = create_app(ev)
    with TestClient(app) as client:
        with client.websocket_connect("/ws/keepalive"):
            time.sleep(0.1)
            assert not ev.is_set()
        # Socket closed -- shutdown should fire after DISCONNECT_GRACE.
        assert _wait_for(ev.is_set, timeout=2.0), (
            "watchdog must shut down once all keepalive sockets are gone"
        )


def test_refresh_within_disconnect_grace_keeps_server_alive(fast_grace):
    ev = threading.Event()
    app = create_app(ev)
    with TestClient(app) as client:
        with client.websocket_connect("/ws/keepalive"):
            time.sleep(0.05)
        # Simulate a tab refresh: reconnect *before* DISCONNECT_GRACE elapses.
        time.sleep(server_mod.DISCONNECT_GRACE / 2)
        with client.websocket_connect("/ws/keepalive"):
            # Past the original disconnect window, but a new socket is open --
            # we must NOT have shut down.
            time.sleep(server_mod.DISCONNECT_GRACE + 0.2)
            assert not ev.is_set(), (
                "reconnecting within the grace window must keep the server alive"
            )
