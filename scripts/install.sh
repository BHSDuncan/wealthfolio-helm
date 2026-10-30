#!/usr/bin/env bash
# The installer keeps credentials out of Helm values and release history.
set -euo pipefail
set +x
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
if ! command -v python3 >/dev/null 2>&1; then
  printf 'Python 3.10 or newer is required.\n' >&2
  exit 1
fi
for argument in "$@"; do
  if [[ "$argument" == --ssh || "$argument" == --ssh=* ]]; then
    exec python3 "$root/scripts/install-remote.py" "$@"
  fi
done
exec python3 "$root/scripts/installer.py" "$@"
