# Wealthfolio Helm chart

Unofficial community Helm chart for [Wealthfolio](https://wealthfolio.app/). Deploys one non-root instance with persistent storage, password authentication, and optional HTTPS Ingress and MCP access. Kubernetes **1.34+**; chart **0.1.0**, application **3.11.0**.

Standard Helm installation is the primary workflow. Chart 0.1.0 is unreleased; OCI publication is pending, so use the local chart below. The intended GHCR distribution and publication prerequisites are in the [release guide](docs/releases.md). Before public exposure, review the pinned image’s [recorded security limitations](docs/security.md#upstream-image).

## Quick start with Helm

You need Helm 3 or 4, kubectl, and a trusted kubeconfig for an existing Kubernetes **1.34+** cluster. The cluster needs durable filesystem storage writable by UID/GID 1000, a default StorageClass (or an explicit `persistence.storageClass`), permissions to create the release resources, and node access to the official application image on GHCR. No ingress controller, TLS certificate, Python, SSH or cert-manager is required by the chart.

Clone the source and select your verified context:

```bash
git clone https://github.com/BHSDuncan/wealthfolio-helm.git
cd wealthfolio-helm
kubectl config get-contexts
export WF_CONTEXT=your-context
```

First [prepare the namespace and authentication Secret](docs/install.md#prepare-credentials). That complete procedure prompts for a password and uses a small isolated Python environment; you can instead supply the same Secret through your existing secret manager. Create `wealthfolio-auth` in namespace `wealthfolio` with both entries:

| Secret key | Required content |
|---|---|
| `WF_SECRET_KEY` | Stable base64 text decoding to exactly 32 random bytes. Keep the original key in an independent private backup. |
| `WF_AUTH_PASSWORD_HASH` | Literal Argon2id PHC password hash with single `$` characters. |

Credentials stay in the existing Kubernetes Secret, outside Helm values and release history. Never put a password, hash or master key in Helm arguments or values. Kubernetes Secret base64 encoding is not encryption; restrict access to the Secret.

Save the minimum nonsecret configuration as `values.yaml`:

```yaml
auth:
  existingSecret: wealthfolio-auth
```

Only `auth.existingSecret` is required in values. Shell environment variables do **not** automatically become Helm values: exporting `WF_SECRET_KEY`, for example, neither creates a Secret nor configures the chart. Helm uses its defaults and the values files/explicit overrides you pass. Do not use credential overrides.

Lint, review the rendered resources, and install. The offline preview uses the Kubernetes 1.34 support floor; the installation checks your actual cluster version:

```bash
helm lint ./charts/wealthfolio --strict --values values.yaml
helm template wealthfolio ./charts/wealthfolio \
  --namespace wealthfolio --kube-version 1.34.0 --values values.yaml
helm install wealthfolio ./charts/wealthfolio \
  --kube-context "$WF_CONTEXT" --namespace wealthfolio \
  --values values.yaml --wait --timeout 10m
kubectl --context "$WF_CONTEXT" --namespace wealthfolio \
  port-forward service/wealthfolio 8088:8088 --address 127.0.0.1
```

Open **http://localhost:8088** and keep port-forward running while using it. The default Service is private ClusterIP; the shipped origins support this loopback workflow. Confirm login and persistent record readback before storing valuable data. The [installation guide](docs/install.md) covers credential preparation, customization and verification in detail.

## Optional access and configuration

Ingress, DNS and TLS are optional, separate choices. For a browser hostname, use an existing ingress controller, point DNS at it, and configure the exact browser origin and cookie policy in values. The [HTTPS Ingress example](examples/ingress-tls.yaml) uses an existing namespace-local TLS Secret; replace its hostname, ingress class and Secret name before use. Pass the reviewed file with `--values`. Certificates, shared controllers and cluster provisioning are operator-managed; **cert-manager is optional and installed separately**, never by this chart. See [access and TLS configuration](docs/install.md#optional-ingress-and-tls).

Other optional settings include storage class/size, fresh database encryption, IPv6 and MCP. See the [chart values reference](charts/wealthfolio/README.md) and [examples](examples/). Database encryption is off by default, while the credential vault remains encrypted. Choose `encryption.required: true` before creating data if wanted; changing the flag does not convert existing databases.

## Optional guided installer

For homelab users and first-time Kubernetes operators, the repository includes an interactive convenience installer:

```bash
./scripts/install.sh --hostname wealthfolio.example.lan
```

It uses an existing cluster and ingress controller, prompts for access settings and a password, prepares Helm if missing, and saves private recovery state on the installation host. Python 3.10+ and kubectl are required; `--local-access` offers loopback access without Ingress, and `--ssh user@host` can run setup on a remote Linux host. Read the [guided installer prerequisites, host changes, saved files and management workflow](docs/install.md#optional-guided-installer) before running it. Use its access configuration and renewal commands while it manages an installation; ordinary reruns preserve saved settings.

## Data and lifecycle

The chart retains the complete `/data` claim when uninstalled. **Back up the original master key separately**; losing it makes encrypted data unrecoverable. One replica and Recreate updates support the single-writer workload; updates cause downtime. Stop all writers before maintenance or recovery. Retained storage and healthy probes do not establish working backups.

- [Backup and restore](docs/backup-restore.md) · [Upgrades and retained storage](docs/upgrading.md)
- [MCP](docs/mcp.md) · [Troubleshooting](docs/troubleshooting.md)
- [Security](docs/security.md) · [Compatibility](docs/compatibility.md)
- [Contributing](CONTRIBUTING.md) · [Changelog](CHANGELOG.md)

The chart uses the official versioned Wealthfolio image pinned by digest. It does not rebuild or modify the application image. Independently authored chart and tooling files are Apache-2.0; Wealthfolio remains AGPL-3.0. See [NOTICE](NOTICE) and [image security](docs/security.md). Wealthfolio is a trademark of Teymz Inc.; no endorsement is implied.
