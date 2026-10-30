#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH="$PWD/.tools/bin:$PATH"
if [[ -x .venv/bin/python ]]; then export PATH="$PWD/.venv/bin:$PATH"; fi
export PYTHONDONTWRITEBYTECODE=1
render_dir=$(mktemp -d)
trap 'rm -rf "$render_dir"' EXIT
for binary in "${HELM_BIN:-helm}" "${HELM4_BIN:-helm4}"; do
  command -v "$binary" >/dev/null || { echo "Missing $binary: run scripts/setup-tools.py" >&2; exit 1; }
  "$binary" version --short
  "$binary" lint charts/wealthfolio --strict -f tests/values/default-valid.yaml
  HELM_BIN="$binary" python3 -m pytest -q tests/test_render.py tests/test_scripts.py tests/test_chart_adversarial.py tests/test_installer.py tests/test_install_remote.py tests/test_tls_bootstrap.py
  "$binary" template wealthfolio charts/wealthfolio --namespace wealthfolio-chart-test \
    --kube-version 1.35.8 -f tests/values/default-valid.yaml > "$render_dir/default.yaml"
  for fixture in examples/*.yaml; do
    "$binary" template wealthfolio charts/wealthfolio --namespace wealthfolio-chart-test \
      --kube-version 1.35.8 -f tests/values/default-valid.yaml -f "$fixture" > "$render_dir/$(basename "$fixture")"
  done
done
command -v kubeconform >/dev/null || { echo 'Missing kubeconform: run scripts/setup-tools.py' >&2; exit 1; }
mkdir -p .tools/schemas
for version in 1.34.9 1.34.11 1.35.8 1.37.0; do
  kubeconform -strict -summary -cache "$PWD/.tools/schemas" -kubernetes-version "$version" "$render_dir/"*.yaml
done
python3 scripts/check-docs.py
git diff --check
