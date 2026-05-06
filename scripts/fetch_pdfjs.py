"""Download the PDF.js distribution into src/gitpdf/web/vendor/pdfjs/.

Run once after install. Pulls the prebuilt ESM bundle from the official
GitHub release and unpacks only the files we need (pdf.mjs, pdf.worker.mjs,
plus the cmaps and standard fonts directories).
"""
from __future__ import annotations

import io
import sys
import urllib.request
import zipfile
from pathlib import Path

PDFJS_VERSION = "4.7.76"
ARCHIVE_URL = (
    f"https://github.com/mozilla/pdf.js/releases/download/v{PDFJS_VERSION}/"
    f"pdfjs-{PDFJS_VERSION}-dist.zip"
)

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "src" / "gitpdf" / "web" / "vendor" / "pdfjs"


def main() -> int:
    DEST.mkdir(parents=True, exist_ok=True)
    print(f"Fetching {ARCHIVE_URL} ...")
    with urllib.request.urlopen(ARCHIVE_URL) as resp:  # noqa: S310 -- known URL
        data = resp.read()
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        wanted_prefixes = ("build/", "web/cmaps/", "web/standard_fonts/")
        for name in zf.namelist():
            if name.endswith("/"):
                continue
            if not any(name.startswith(p) for p in wanted_prefixes):
                continue
            target = DEST / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(name) as src, target.open("wb") as out:
                out.write(src.read())
    print(f"Installed PDF.js {PDFJS_VERSION} to {DEST}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
