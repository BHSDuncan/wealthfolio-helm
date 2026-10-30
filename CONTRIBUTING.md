# Contributing

Keep the chart small and downstream of Wealthfolio. Update values, schema, templates, examples and operator documentation together. Preserve credentials, claim identity and the single-writer model. Add meaningful checks for behavior changes, especially storage, authentication, networking and encryption.

## Local checks

Use Python 3.10+, OpenSSL and the locked development dependencies. Tool setup installs checksum-verified binaries under ignored `.tools/` without changing system packages.

```bash
python3 -m venv .venv
.venv/bin/pip install --require-hashes -r requirements.lock
python3 scripts/setup-tools.py
scripts/check.sh
python3 -m unittest discover -s .github/tests
scripts/package.sh
```

Checks cover Helm 3 and 4, valid and invalid values, rendered security/storage settings, installer behavior, documentation links and Kubernetes schemas. Packages and checksums go to ignored `dist/`.

## Integration tests

With Docker available, run an isolated Kind smoke test:

```bash
scripts/integration.sh --environment kind --kubernetes-version 1.35.8 \
  --helm-major 3 --suite smoke --report dist/reports/kind-smoke.json
```

The full suite also needs Node.js 22+, `npm ci --ignore-scripts` and `npx playwright install chromium`; Linux may require `--with-deps`. Select `--suite full` to exercise synthetic profiles, credentials, lifecycle, backup/recovery, encryption and MCP.

For an existing Kubernetes cluster, use a scoped kubeconfig, explicit context and empty dedicated test namespace. Set `WF_CLUSTER_UID` to the `kube-system` namespace UID obtained from that verified context:

```bash
scripts/integration.sh --environment existing \
  --kubeconfig "$WF_KUBECONFIG" --context "$WF_CONTEXT" \
  --namespace wealthfolio-chart-test --release wealthfolio-test \
  --confirm-cluster-uid "$WF_CLUSTER_UID" \
  --report dist/reports/existing-cluster.json
```

Integration tests create resources and synthetic data. Never use valuable storage or an ambient current context. Keep TLS and SSH host verification enabled, use existing cluster infrastructure, and clean up only resources belonging to the test run. Test reports must exclude credentials, real financial data, kubeconfigs and private endpoints. See [compatibility](docs/compatibility.md) for coverage limits.

## Submitting changes

Record checks actually run and any remaining failures or omissions. Recheck upstream behavior when changing authentication, environment variables, data paths, probes or MCP. A healthy pod does not establish working login, persistent data or recovery.

Include user-visible changes and upgrade implications in [CHANGELOG](CHANGELOG.md). Chart and application versions are independent; publication is described in the [maintainer release guide](docs/releases.md).
