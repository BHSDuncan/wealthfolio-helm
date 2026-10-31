# Contributing

Contributions should keep the chart focused on deploying the official Wealthfolio image. When changing configuration, update values, schema, templates, examples and operator documentation together. Preserve the existing authentication Secret, persistent claim identity and single-writer model. Behavior changes need meaningful checks, especially for storage, authentication, networking and encryption.

## Local checks

Run these commands from the repository root with Python 3.10+, venv support and OpenSSL available. Dependency installation needs access to PyPI; tool setup downloads checksum-verified binaries into ignored `.tools/` without changing system packages.

```bash
python3 -m venv .venv
.venv/bin/pip install --require-hashes -r requirements.lock
python3 scripts/setup-tools.py
scripts/check.sh
python3 -m unittest discover -s .github/tests
scripts/package.sh
```

The checks cover Helm 3 and 4, valid and invalid values, rendered security and storage settings, installer behavior, documentation links and Kubernetes schemas. Publication workflow tests run separately through the `unittest` command above. Packaging writes the chart archive and checksum to ignored `dist/`.

## Integration tests

With Docker available, run an isolated Kind smoke test:

```bash
scripts/integration.sh --environment kind --kubernetes-version 1.35.8 \
  --helm-major 3 --suite smoke --report dist/reports/kind-smoke.json
```

The full suite exercises synthetic profiles, credentials, lifecycle, backup/recovery, encryption and MCP. It also needs Node.js 22+ and these dependencies:

```bash
npm ci --ignore-scripts
npx playwright install chromium
```

On Linux, Playwright may also need `--with-deps`. Select `--suite full` in the integration command to run the full suite.

For an existing Kubernetes cluster, use a scoped kubeconfig, an explicit context and an empty dedicated test namespace. Set `WF_KUBECONFIG` to the kubeconfig path and `WF_CONTEXT` to the verified context, then read that cluster's identity:

```bash
export WF_CLUSTER_UID="$(kubectl --kubeconfig "$WF_KUBECONFIG" --context "$WF_CONTEXT" \
  get namespace kube-system -o jsonpath='{.metadata.uid}')"
```

The following command runs the full suite by default and therefore also needs the Node.js and browser dependencies above:

```bash
scripts/integration.sh --environment existing \
  --kubeconfig "$WF_KUBECONFIG" --context "$WF_CONTEXT" \
  --namespace wealthfolio-chart-test --release wealthfolio-test \
  --confirm-cluster-uid "$WF_CLUSTER_UID" \
  --report dist/reports/existing-cluster.json
```

Integration tests create resources and synthetic data. Never use valuable storage or rely on an unverified current context. Keep TLS and SSH host verification enabled, use existing cluster infrastructure, and clean up only resources belonging to the test run. Test reports must exclude credentials, real financial data, kubeconfigs and private endpoints. See [compatibility](docs/compatibility.md) for coverage limits.

## Submitting changes

Record checks actually run and any remaining failures or omissions. Recheck upstream behavior when changing authentication, environment variables, data paths, probes or MCP. A healthy pod does not establish working login, persistent data or recovery.

Include user-visible changes and upgrade implications in [CHANGELOG](CHANGELOG.md). Chart and application versions are independent; publication is described in the [maintainer release guide](docs/releases.md).
