"""Compile the frontend and vendor it into the Python package.

Run before building a wheel so the distribution serves the UI at /ui:

    python scripts/build_ui.py

Requires Node.js and npm. The output directory is gitignored; the bundle only ever
exists in a release build or after running this script locally.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"
DIST_DIR = WEB_DIR / "dist"
TARGET_DIR = ROOT / "src" / "bce" / "api" / "rest" / "static"


def run(*args: str) -> None:
    print(f"$ {' '.join(args)}", flush=True)
    # shell=True on Windows: npm ships as npm.cmd, which is not directly executable.
    subprocess.run(args, cwd=WEB_DIR, check=True, shell=sys.platform == "win32")


def main() -> int:
    if not WEB_DIR.is_dir():
        print(f"error: {WEB_DIR} not found", file=sys.stderr)
        return 1

    run("npm", "ci")
    run("npm", "run", "build")

    if not (DIST_DIR / "index.html").is_file():
        print(f"error: build produced no index.html in {DIST_DIR}", file=sys.stderr)
        return 1

    if TARGET_DIR.exists():
        shutil.rmtree(TARGET_DIR)
    shutil.copytree(DIST_DIR, TARGET_DIR)

    files = sum(1 for p in TARGET_DIR.rglob("*") if p.is_file())
    print(f"\nvendored {files} files into {TARGET_DIR.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
