#!/bin/zsh
# One-time setup: creates .venv, installs dependencies, builds CrateDigger.app.
set -e
cd "$(dirname "$0")/.."
PY=$(command -v python3.14 || command -v python3.13 || command -v python3.12 || command -v python3.11 || command -v python3)
echo "Using $PY ($($PY --version))"
command -v ffmpeg >/dev/null || { echo "FFmpeg not found. Install it with: brew install ffmpeg"; exit 1; }
command -v clamscan >/dev/null || echo "WARNING: ClamAV not installed — files cannot be approved until it is: brew install clamav (see README)"
[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/pip install --upgrade pip -q
.venv/bin/pip install -r requirements.txt -q
./scripts/build_app.sh
echo "\nDone. Launch with: open dist/CrateDigger.app   (or ./scripts/run.sh)"
