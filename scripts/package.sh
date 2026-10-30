#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH="$PWD/.tools/bin:$PATH"
if [[ -x .venv/bin/python ]]; then export PATH="$PWD/.venv/bin:$PATH"; fi
python3 scripts/package-check.py
mkdir -p dist
helm package charts/wealthfolio --destination dist
python3 scripts/package-check.py --archive dist/wealthfolio-0.1.0.tgz
