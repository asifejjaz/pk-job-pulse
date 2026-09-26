#!/usr/bin/env sh
# Daily run: scan, then rebuild the public stats file.
set -e
cd "$(dirname "$0")/.."
python -m pulse.scanner scan
python -m pulse.stats
