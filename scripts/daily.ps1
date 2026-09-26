# Daily run: scan, then rebuild the public stats file.
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
$env:PYTHONIOENCODING = "utf-8"
python -m pulse.scanner scan
python -m pulse.stats
