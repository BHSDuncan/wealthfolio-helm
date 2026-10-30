#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH="$PWD/.tools/bin:$PATH"
if [[ -x .venv/bin/python ]]; then export PATH="$PWD/.venv/bin:$PATH"; fi
export PYTHONDONTWRITEBYTECODE=1
exec python3 tests/integration/run.py "$@"
