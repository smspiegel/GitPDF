"""Command-line interface for gitpdf.

  gitpdf                                # opens the GUI (default)
  gitpdf gui                            # explicit GUI
  gitpdf diff A.pdf B.pdf [options]     # headless diff -> JSON

The `diff` subcommand is the integration point for downstream pipelines:
it produces a stable JSON document (the DiffResult Pydantic model) that
downstream tools can consume.
"""
from __future__ import annotations

import argparse
import json
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

from .diff_engine import compute_diff
from .extract import extract_document
from .models import DiffResult, Mode
from .paths import exports_dir


def _run_gui() -> int:
    import uvicorn

    from .server import create_app

    def free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    port = free_port()
    shutdown_event = threading.Event()
    app = create_app(shutdown_event=shutdown_event)
    # log_config=None skips uvicorn's dictConfig (which probes sys.stdout
    # for color support); critical for windowed PyInstaller builds where
    # stdout is a redirected log file with no isatty truthiness.
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning", log_config=None
    )
    server = uvicorn.Server(config)

    def _serve() -> None:
        # Daemon thread: an unhandled exception would die silently and
        # leave the main thread waiting on shutdown_event forever.
        try:
            server.run()
        except BaseException:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            shutdown_event.set()

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()

    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.05)
    else:
        print("Server failed to start", file=sys.stderr, flush=True)
        return 1

    url = f"http://127.0.0.1:{port}"
    try:
        import webview  # pywebview, optional
    except ImportError:
        # No native window available -- open the user's default browser so
        # double-clicking the exe behaves like launching any other app.
        # The server self-shuts-down via the heartbeat watchdog when the
        # browser tab is closed.
        try:
            webbrowser.open(url, new=1, autoraise=True)
        except Exception:  # noqa: BLE001 -- best-effort, server still works
            pass
        print(f"gitpdf serving at {url}  (closes when browser tab closes)", flush=True)
        try:
            shutdown_event.wait()
        except KeyboardInterrupt:
            pass
        server.should_exit = True
        thread.join(timeout=5)
        return 0

    webview.create_window("PDF Diff", url, width=1400, height=900)
    webview.start()
    server.should_exit = True
    thread.join(timeout=5)
    return 0


def _run_diff(args: argparse.Namespace) -> int:
    pdf_a = Path(args.pdf_a).resolve()
    pdf_b = Path(args.pdf_b).resolve()
    for p in (pdf_a, pdf_b):
        if not p.is_file():
            print(f"not a file: {p}", file=sys.stderr)
            return 2

    print(f"Extracting {pdf_a.name}...", file=sys.stderr, flush=True)
    ea = extract_document(pdf_a)
    print(f"Extracting {pdf_b.name}...", file=sys.stderr, flush=True)
    eb = extract_document(pdf_b)
    print("Computing diff...", file=sys.stderr, flush=True)
    result: DiffResult = compute_diff(
        ea.tokens, eb.tokens, ea.page_count, eb.page_count, mode=args.mode
    )

    payload = {
        "schema_version": 1,
        "source_a": str(pdf_a),
        "source_b": str(pdf_b),
        "mode_used": result.mode_used,
        "similarity": result.similarity,
        "page_count_a": result.page_count_a,
        "page_count_b": result.page_count_b,
        "summary": [s.model_dump() for s in result.summary],
        # `overlays` are page-coordinate rects, mainly useful for UI rendering.
        # Included so a downstream pipeline can re-render highlights if needed.
        "overlays": [o.model_dump() for o in result.overlays],
    }

    if args.out:
        out_path = Path(args.out)
    else:
        out_path = exports_dir() / f"{pdf_a.stem}__vs__{pdf_b.stem}.diff.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    indent = 2 if args.pretty else None
    out_path.write_text(json.dumps(payload, indent=indent, ensure_ascii=False), encoding="utf-8")
    print(str(out_path), flush=True)
    return 0


def _run_schema(args: argparse.Namespace) -> int:
    schema = DiffResult.model_json_schema()
    text = json.dumps(schema, indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(args.out)
    else:
        print(text)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gitpdf", description=__doc__)
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("gui", help="Open the GUI (default).")

    p_diff = sub.add_parser("diff", help="Diff two PDFs and emit JSON.")
    p_diff.add_argument("pdf_a", help="First PDF (the 'old' / left side).")
    p_diff.add_argument("pdf_b", help="Second PDF (the 'new' / right side).")
    p_diff.add_argument(
        "--mode",
        choices=("auto", "git-style", "diff-only"),
        default="auto",
        help="Comparison mode (default: auto).",
    )
    p_diff.add_argument("--out", help="Output JSON path. Default: data/exports/<name>.diff.json")
    p_diff.add_argument("--pretty", action="store_true", help="Indent JSON output.")

    p_schema = sub.add_parser("schema", help="Print the DiffResult JSON schema.")
    p_schema.add_argument("--out", help="Write schema to a file instead of stdout.")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd in (None, "gui"):
        return _run_gui()
    if args.cmd == "diff":
        return _run_diff(args)
    if args.cmd == "schema":
        return _run_schema(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
