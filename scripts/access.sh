#!/usr/bin/env bash
# Reconnect private localhost access to an already installed release.
set -euo pipefail
script_dir=$(cd -- "$(dirname -- "$0")" && pwd)
for task_arg in "$@"; do
  if [[ "$task_arg" == "--ssh" || "$task_arg" == --ssh=* ]]; then
    exec python3 "$script_dir/install-remote.py" --local-access --access-only "$@"
  fi
done
exec "$script_dir/install.sh" --local-access --access-only "$@"
