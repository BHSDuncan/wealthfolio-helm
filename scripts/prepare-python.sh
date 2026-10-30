#!/usr/bin/env bash
# Called only by an owner-run local K3s installation when ensurepip is absent.
set -euo pipefail
set +x
if python3 -c 'import ensurepip' >/dev/null 2>&1; then
  exit 0
fi
if [[ "$(id -u)" != 0 ]] || ! command -v apt-get >/dev/null 2>&1; then
  printf 'Python venv support is required. Install your OS python3-venv package, then rerun the installer.\n' >&2
  exit 1
fi
printf 'Installing the Python venv dependency (python3-venv); no system upgrade is requested.\n'
apt-get update
apt-get install --yes --no-install-recommends python3-venv
python3 -c 'import ensurepip'
