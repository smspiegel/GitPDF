# GitPDF

Local PDF comparison tool. Two PDFs in (text-embedded or scanned), side-by-side diff highlights out. Fully offline, permissive-licensed dependencies only.

Three streams of using the same code:

| Stream | Use case | Entry point |
|---|---|---|
| Dev | Editing the code, running tests | `python -m gitpdf` from the venv |
| Portable | Handing the app to a non-technical user | `dist\gitpdf\gitpdf.exe` |
| CLI | Feeding diffs into the agent pipeline | `<binary> diff old.pdf new.pdf` |

The CLI commands are identical across streams; only the binary path changes. Where this README writes `<binary>`, substitute one of:

- Dev: `.\.venv\Scripts\python.exe -m gitpdf`
- Portable: `.\dist\gitpdf\gitpdf-console.exe` (use the console build for CLI work so output is visible)

## Stream 1: Dev

One-time setup:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe scripts\fetch_pdfjs.py
```

Run:

```powershell
.\.venv\Scripts\python.exe -m gitpdf        # opens GUI in default browser
.\.venv\Scripts\python.exe -m pytest tests/  # runs 14 diff-engine tests
```

The GUI prints `gitpdf serving at http://127.0.0.1:<port>`. Ctrl+C to stop.

### Optional OCR backends

Born-digital PDFs work without OCR. Scanned PDFs need one of:

- **Tesseract** for typed scans: install the binary, ensure `tesseract.exe` is on `PATH`. Windows installer: <https://github.com/UB-Mannheim/tesseract/wiki>.
- **PaddleOCR** for handwriting or poor scans (~500 MB): `pip install -e ".[ocr-paddle]"`. Auto-detected when present.

## Stream 2: Portable

Build the distributable folder:

```powershell
.\scripts\build.ps1
```

After 1 to 2 minutes, [dist/gitpdf/](dist/gitpdf/) (~190 MB) contains two binaries:

- `gitpdf.exe`: windowed, no console window. Auto-opens the default browser, auto-exits when the tab closes. Send this one to end users.
- `gitpdf-console.exe`: same code with a console attached for logs and CLI work.

End-user workflow:

1. Double-click `gitpdf.exe`. Browser opens after ~10 second cold start.
2. Use the GUI (see below).
3. Close the browser tab. The exe shuts down within ~10 seconds.
4. To move it: copy the whole `dist\gitpdf\` folder anywhere (desktop, USB, network share).
5. To uninstall: delete the folder.

All runtime data lives in `dist\gitpdf\data\` (sessions, JSON exports, startup log at `data\gitpdf.log`).

## Stream 3: CLI / pipeline

Headless diff for downstream pipelines.

```powershell
<binary> diff old.pdf new.pdf --mode git-style --pretty
```

Output: `data\exports\<old>__vs__<new>.diff.json` (or pass `--out path`). Each entry in `summary[]`:

| Field | Meaning |
|---|---|
| `kind` | `removed`, `added`, or `moved` |
| `page_a`, `page_b` | Page numbers on each side |
| `text_a`, `text_b` | The changed text |
| `context_a`, `context_b` | The change wrapped in `⟦ ⟧` plus 12 words of surrounding context. This is the field the agent reads. |

Modes:

- `auto` (default): git-style if similarity >= 70%, else diff-only.
- `git-style`: red removed, green added, yellow moved.
- `diff-only`: neutral marker on both sides. Better for short reordered docs.

Print the JSON Schema:

```powershell
<binary> schema --out diff.schema.json
```

Use the diff engine as a Python library, skipping the CLI entirely:

```python
from gitpdf.diff_engine import compute_diff
from gitpdf.extract import extract_document

ea = extract_document("old.pdf")
eb = extract_document("new.pdf")
result = compute_diff(ea.tokens, eb.tokens, ea.page_count, eb.page_count, mode="git-style")
print(result.model_dump_json(indent=2))
```

`diff_engine.py` is I/O-free, so it imports cleanly into another pipeline without dragging FastAPI, OCR, or PDF rendering with it.

## Using the GUI

Applies to streams 1 and 2.

1. Click **Open Document A**, pick the old PDF. The pane title shows the filename.
2. Click **Open Document B**, pick the new PDF.
3. Pick a **Mode**.
4. Click **Compare**. Status bar shows similarity %, mode used, and change count.
5. **↑ / ↓** or **Ctrl+↑ / Ctrl+↓** jump between changes. Click any highlight to focus it.
6. **Summary** opens a right rail with every change.
7. **Sync scroll** keeps both panes aligned by ratio. Auto-disabled when page counts differ or the engine picked diff-only mode (since ratio-syncing drifts away from the diff you clicked).

Highlights: red = removed (only in A), green = added (only in B), yellow = moved (both sides).

## Project layout

```
gitpdf/
├── pyproject.toml                deps + entry points
├── gitpdf.spec                  PyInstaller config (produces both exes)
├── gitpdf_launch.py             frozen-build entry script (stdio fixup)
├── scripts/
│   ├── build.ps1                 builds dist/gitpdf/
│   └── fetch_pdfjs.py            downloads PDF.js into web/vendor/
├── src/gitpdf/
│   ├── cli.py                    argparse: gui / diff / schema
│   ├── server.py                 FastAPI routes + heartbeat watchdog
│   ├── extract.py                PDF to tokens (pdfplumber + pypdfium2 + OCR)
│   ├── ocr.py                    PaddleOCR + Tesseract dispatch
│   ├── diff_engine.py            pure-Python: blocks, alignment, word diff
│   ├── models.py                 Pydantic types (Token, Block, Overlay, DiffResult)
│   ├── paths.py                  portable file layout (frozen-aware)
│   └── web/                      PDF.js + index.html + app.js + styles.css
└── tests/test_diff_engine.py     14 tests, no PDF or OCR dependency
```

## Licensing

All runtime dependencies are MIT, BSD, or Apache 2.0. No AGPL, no LGPL, no paid frameworks. PyInstaller's runtime exception permits proprietary distribution of the bootloader bundled into the exe.
