#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH="$PWD/.tools/bin:$PATH"
if [[ -x .venv/bin/python ]]; then export PATH="$PWD/.venv/bin:$PATH"; fi
python3 scripts/package-check.py
mkdir -p dist
helm package charts/wealthfolio --destination dist
archive_name=$(python3 - <<'PYTHON'
import pathlib
import yaml
chart = yaml.safe_load(pathlib.Path('charts/wealthfolio/Chart.yaml').read_text())
print(f"{chart['name']}-{chart['version']}.tgz")
PYTHON
)
python3 scripts/package-check.py --archive "dist/$archive_name"
